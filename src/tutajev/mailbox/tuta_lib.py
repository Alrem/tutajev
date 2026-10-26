"""Mailbox adapter that talks to Tuta's API through tutaproxy's ``tuta`` package used as a library.

This is the only module that knows about tutaproxy. Tuta entities arrive as dicts keyed by
numeric field ids (see tutao/tutanota ``tuta-sdk/rust/sdk/src/type_models/tutanota.json``).
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.parser
import email.policy
import email.utils
import importlib
import logging
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any, Self

from tutajev.mailbox._readonly import ReadOnlyTutaClient
from tutajev.mailbox.port import SYSTEM_FOLDERS, MailboxError
from tutajev.models import AttachmentMeta, RawMessage

logger = logging.getLogger(__name__)

# MailSetKind values (Mail set field 436).
FOLDER_TYPES: dict[str, str] = {
    "inbox": "1",
    "sent": "2",
    "trash": "3",
    "archive": "4",
    "spam": "5",
}

FOLDER_CUSTOM = "0"

# Field ids from the Tuta type model.
MAIL_ID = "99"
MAIL_OWNER_ENC_SESSION_KEY = "102"
MAIL_RECEIVED_DATE = "107"
MAIL_UNREAD = "109"
MAIL_ATTACHMENTS = "115"
MAIL_DETAILS_DRAFT = "1309"
MAIL_BUCKET_KEY = "1310"
BUCKET_KEY_PQ_MESSAGE = "2045"
BUCKET_KEY_INSTANCE_KEYS = "2048"
INSTANCE_KEY_ELEMENT_ID = "2041"
INSTANCE_KEY_ENC_SESSION_KEY = "2042"
MAIL_DETAILS_BLOB_DETAILS = "1305"
MAIL_DETAILS_REPLY_TOS = "1285"
MAIL_DETAILS_HEADERS = "1287"
HEADER_HEADERS = "1271"
HEADER_COMPRESSED_HEADERS = "1272"
ENC_MAIL_ADDRESS_NAME = "614"
ENC_MAIL_ADDRESS_ADDRESS = "615"
FILE_OWNER_ENC_SESSION_KEY = "18"
FILE_NAME = "21"
FILE_SIZE = "22"
FILE_MIME_TYPE = "23"
FILE_CID = "924"
MAIL_OWNER_KEY_VERSION = "1395"
FILE_OWNER_KEY_VERSION = "1391"
# sys model: Group.formerGroupKeys -> GroupKeysRef.list -> GroupKey.ownerEncGKey
GROUP_FORMER_GROUP_KEYS = "2273"
GROUP_KEYS_REF_LIST = "2269"
GROUP_KEY_ID = "2257"
GROUP_KEY_OWNER_ENC_G_KEY = "2261"

# Concurrent per-message API calls; keeps load on Tuta's API modest.
_CONCURRENCY = 4


@dataclass(frozen=True)
class _TutaModules:
    api: ModuleType
    crypto: ModuleType
    message_builder: ModuleType


def load_tuta(tutaproxy_path: Path) -> _TutaModules:
    """Import tutaproxy's ``tuta`` package from a checkout at ``tutaproxy_path``.

    Only ``tuta.api``, ``tuta.crypto`` and ``tuta.message_builder`` are imported;
    ``tuta.telemetry`` (which phones home) is never imported.
    """
    package_dir = tutaproxy_path / "tuta"
    if not (package_dir / "api.py").is_file():
        raise MailboxError(
            f"tutaproxy not found at {tutaproxy_path}; run scripts/fetch_tutaproxy.sh "
            "or set TUTAPROXY_PATH"
        )
    os.environ.setdefault("TUTAPROXY_TELEMETRY", "false")
    path_str = str(tutaproxy_path.resolve())
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
    return _TutaModules(
        api=importlib.import_module("tuta.api"),
        crypto=importlib.import_module("tuta.crypto"),
        message_builder=importlib.import_module("tuta.message_builder"),
    )


class TutaLibReader:
    """Read-only MailboxReader backed by tutaproxy's ``TutaClient``."""

    def __init__(
        self,
        email_address: str,
        password: str,
        tutaproxy_path: Path,
        totp_secret: str = "",
        client_factory: Callable[[], Any] | None = None,
        allow_move_to_spam: bool = False,
    ) -> None:
        self._email = email_address
        self._password = password
        self._totp_secret = totp_secret
        self._tuta = load_tuta(tutaproxy_path)
        self._client_factory = client_factory or self._tuta.api.TutaClient
        self._raw_client: Any = None
        self._client: Any = None
        self._session: Any = None
        self._mail_group_key: bytes | None = None
        # mail group key by version; former versions are loaded lazily
        self._group_keys: dict[int, bytes] = {}
        self._allow_move_to_spam = allow_move_to_spam
        # element id -> (mail bag list id, element id) of messages returned by list_recent
        self._listed_ids: dict[str, tuple[str, str]] = {}

    async def __aenter__(self) -> Self:
        if self._totp_secret:
            # tutaproxy reads the TOTP secret from the environment during login.
            os.environ["TUTA_TOTP_SECRET"] = self._totp_secret
        self._raw_client = self._client_factory()
        await self._raw_client.__aenter__()
        self._client = ReadOnlyTutaClient(self._raw_client, allow_move=self._allow_move_to_spam)
        try:
            logger.info("Logging in to Tuta as %s", self._email)
            self._session = await self._client.login(self._email, self._password)
        except Exception as exc:
            await self._raw_client.__aexit__(None, None, None)
            raise MailboxError(f"Tuta login failed: {exc}") from exc
        logger.info("Logged in")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._session is not None:
                token = self._session.access_token
                list_id, element_id = self._tuta.api._session_id_from_access_token(token)
                await self._client.close_session(list_id, element_id, token)
                logger.debug("Tuta session closed")
        except Exception as logout_exc:  # logout failures must not mask the original error
            logger.warning("Closing the Tuta session failed; it stays open: %s", logout_exc)
        finally:
            self._session = None
            await self._raw_client.__aexit__(exc_type, exc, tb)

    async def list_folders(self) -> list[str]:
        self._require_session()
        custom = [
            await self._folder_name(f)
            for f in await self._client.get_folders(self._session)
            if f.folder_type == FOLDER_CUSTOM
        ]
        return [*SYSTEM_FOLDERS, *sorted(custom, key=str.casefold)]

    async def list_recent(
        self, folder: str = "inbox", limit: int | None = 20, unread_only: bool = False
    ) -> list[RawMessage]:
        self._require_session()
        if limit is not None and limit <= 0:
            return []

        target = await self._find_folder(folder)
        mails: list[dict[str, Any]] = await self._client.get_mails_in_folder(
            self._session, target.mail_list_id
        )
        total = len(mails)
        if unread_only:
            mails = [m for m in mails if m.get(MAIL_UNREAD) == "1"]
        mails.sort(key=lambda m: int(m.get(MAIL_RECEIVED_DATE) or 0), reverse=True)
        newest = mails[:limit]
        for mail in newest:
            mail_id = mail.get(MAIL_ID)
            if isinstance(mail_id, list) and len(mail_id) == 2:
                self._listed_ids[str(mail_id[1])] = (str(mail_id[0]), str(mail_id[1]))
        logger.info(
            "Folder %s: %d messages, %s, fetching %d",
            folder,
            total,
            f"{len(mails)} unread" if unread_only else "any read state",
            len(newest),
        )

        await self._ensure_mail_group_key()
        semaphore = asyncio.Semaphore(_CONCURRENCY)

        async def fetch(mail: dict[str, Any]) -> RawMessage | None:
            async with semaphore:
                try:
                    return await self._to_raw_message(mail, folder)
                except Exception as exc:
                    logger.warning("Skipping message %s: %s", _element_id(mail), exc)
                    return None

        results = await asyncio.gather(*(fetch(m) for m in newest))
        return [r for r in results if r is not None]

    async def list_ids(self, folder: str) -> set[str]:
        """Return the element ids of all messages in ``folder`` (entities only, no details)."""
        self._require_session()
        target = await self._find_folder(folder)
        mails = await self._client.get_mails_in_folder(self._session, target.mail_list_id)
        return {_element_id(m) for m in mails}

    async def move_to_spam(self, message_ids: Sequence[str]) -> None:
        """Move messages previously returned by :meth:`list_recent` to the Spam folder."""
        self._require_session()
        if not message_ids:
            return
        unknown = [i for i in message_ids if i not in self._listed_ids]
        if unknown:
            raise MailboxError(f"Refusing to move messages not listed in this session: {unknown}")
        mail_ids = [self._listed_ids[i] for i in message_ids]
        logger.info("Moving %d message(s) to Spam", len(mail_ids))
        try:
            await self._client.simple_move_mails(self._session, mail_ids, FOLDER_TYPES["spam"])
        except Exception as exc:
            raise MailboxError(f"Moving to Spam failed: {exc}") from exc

    def _require_session(self) -> None:
        if self._session is None:
            raise MailboxError("Not logged in; use TutaLibReader as an async context manager")

    async def _ensure_mail_group_key(self) -> bytes:
        if self._mail_group_key is None:
            self._mail_group_key = await self._client.get_mail_group_key(self._session)
            self._group_keys[self._current_key_version()] = self._mail_group_key
        return self._mail_group_key

    def _current_key_version(self) -> int:
        return int(getattr(self._session, "mail_group_key_version", "0") or 0)

    async def _group_key(self, version: str | int | None) -> bytes:
        """Mail group key of ``version`` (``None`` means current); handles key rotation.

        Entities created before a rotation are encrypted with a former key. Each former key
        is stored encrypted with the next newer one, so the chain is decrypted downwards.
        """
        current = await self._ensure_mail_group_key()
        if version is None or version == "":
            return current
        wanted = int(version)
        if wanted in self._group_keys:
            return self._group_keys[wanted]
        current_version = self._current_key_version()
        if wanted > current_version:
            raise MailboxError(
                f"group key version {wanted} is newer than current {current_version}"
            )
        await self._load_former_group_keys(current, current_version)
        if wanted not in self._group_keys:
            raise MailboxError(f"former group key version {wanted} not found")
        return self._group_keys[wanted]

    async def _load_former_group_keys(self, current: bytes, current_version: int) -> None:
        group_id = self._session.mail_group_id
        group = await self._client._get(
            self._client._url("sys", "group", group_id), token=self._session.access_token
        )
        keys_ref = _first(group.get(GROUP_FORMER_GROUP_KEYS))
        list_id = _unwrap(keys_ref.get(GROUP_KEYS_REF_LIST))
        if not list_id:
            raise MailboxError("mail group has no former keys")
        former = await self._client._get(
            self._client._url("sys", "groupkey", list_id),
            token=self._session.access_token,
            params={"start": _custom_id(str(current_version)), "count": "100", "reverse": "true"},
        )
        newer_key, newer_version = current, current_version
        for entry in former or []:
            # _id of a list element is [listId, elementId]
            version = int(_decode_custom_id(str(_last(entry.get(GROUP_KEY_ID)))))
            if version != newer_version - 1:
                raise MailboxError(f"unexpected former key version {version} after {newer_version}")
            newer_key = decrypt_key(
                self._tuta.crypto,
                newer_key,
                base64.b64decode(entry.get(GROUP_KEY_OWNER_ENC_G_KEY, "")),
            )
            newer_version = version
            self._group_keys[version] = newer_key
        logger.debug("Loaded %d former mail group key(s)", current_version - newer_version)

    async def _folder_name(self, folder: Any) -> str:
        """Decrypt a custom folder's name; its key is encrypted with the mail group key."""
        try:
            folder_key = decrypt_key(
                self._tuta.crypto,
                await self._group_key(folder.owner_key_version),
                base64.b64decode(folder.owner_enc_session_key),
            )
            name = self._decrypt_str(folder_key, folder.name_encrypted)
        except Exception as exc:
            logger.debug("Cannot decrypt name of folder %s: %s", folder.id, exc)
            name = ""
        return name or f"Folder-{folder.id}"

    async def _find_folder(self, folder: str) -> Any:
        folders = await self._client.get_folders(self._session)
        folder_type = FOLDER_TYPES.get(folder.lower())
        if folder_type is not None:
            for candidate in folders:
                if candidate.folder_type == folder_type:
                    return candidate
            raise MailboxError(f"System folder {folder!r} not found in mailbox")

        wanted = folder.casefold()
        names = {}
        for candidate in folders:
            if candidate.folder_type == FOLDER_CUSTOM:
                names[candidate.id] = (await self._folder_name(candidate), candidate)
        matches = [c for name, c in names.values() if name.casefold() == wanted]
        if len(matches) > 1:
            raise MailboxError(f"Folder name {folder!r} is ambiguous ({len(matches)} folders)")
        if not matches:
            available = ", ".join(sorted((n for n, _ in names.values()), key=str.casefold))
            raise MailboxError(f"Folder {folder!r} not found; custom folders: {available or '-'}")
        return matches[0]

    async def _to_raw_message(self, mail: dict[str, Any], folder: str) -> RawMessage:
        if mail.get(MAIL_DETAILS_DRAFT):
            raise MailboxError("drafts are not supported")
        keys = _KeyResolver(self._tuta.crypto, self._session, mail)
        mail_key = keys.session_key(
            _element_id(mail),
            mail.get(MAIL_OWNER_ENC_SESSION_KEY),
            await self._group_key(mail.get(MAIL_OWNER_KEY_VERSION)),
        )

        details_blob = await self._client.get_mail_details(self._session, mail)
        raw = _drop_synthesized_plain_text(
            self._tuta.message_builder.build_rfc2822(mail, details_blob, mail_key, None)
        )
        # build_rfc2822 omits Reply-To and the original transport headers; header order is
        # irrelevant, so prepend the ones the parser needs.
        extra = [f"Authentication-Results: {v}" for v in self._auth_results(details_blob, mail_key)]
        reply_to = self._reply_to_header(details_blob, mail_key)
        if reply_to:
            extra.append(f"Reply-To: {reply_to}")
        if extra:
            raw = "".join(f"{line}\r\n" for line in extra).encode() + raw

        attachments = await self._attachment_metadata(mail, keys)
        received_ms = int(mail.get(MAIL_RECEIVED_DATE) or 0)
        return RawMessage(
            id=_element_id(mail),
            received_at=datetime.fromtimestamp(received_ms / 1000, tz=UTC) if received_ms else None,
            raw=raw,
            attachments=tuple(attachments),
            folder=folder,
        )

    def _auth_results(self, details_blob: dict[str, Any], mail_key: bytes) -> list[str]:
        """Authentication-Results values from the original headers (MailDetails.headers)."""
        details = _first(details_blob.get(MAIL_DETAILS_BLOB_DETAILS))
        header = _first(details.get(MAIL_DETAILS_HEADERS))
        try:
            if header.get(HEADER_COMPRESSED_HEADERS):
                crypto = self._tuta.crypto
                data = crypto.aes_decrypt_tuta(
                    mail_key, base64.b64decode(header[HEADER_COMPRESSED_HEADERS])
                )
                text = crypto.uncompress_lz4(data).decode("utf-8", "replace")
            else:
                text = self._decrypt_str(mail_key, header.get(HEADER_HEADERS) or "")
        except Exception as exc:
            logger.warning("Cannot read original headers: %s", exc)
            return []
        parsed = email.parser.HeaderParser(policy=email.policy.compat32).parsestr(text + "\n\n")
        return [_one_line(str(v)) for v in parsed.get_all("Authentication-Results") or []]

    def _reply_to_header(self, details_blob: dict[str, Any], mail_key: bytes) -> str:
        details = _first(details_blob.get(MAIL_DETAILS_BLOB_DETAILS))
        addresses = []
        for entry in details.get(MAIL_DETAILS_REPLY_TOS) or []:
            name = _one_line(self._decrypt_str(mail_key, entry.get(ENC_MAIL_ADDRESS_NAME, "")))
            address = _one_line(
                self._decrypt_str(mail_key, entry.get(ENC_MAIL_ADDRESS_ADDRESS, ""))
            )
            if address:
                addresses.append(email.utils.formataddr((name, address), charset="utf-8"))
        return ", ".join(addresses)

    async def _attachment_metadata(
        self, mail: dict[str, Any], keys: _KeyResolver
    ) -> list[AttachmentMeta]:
        """Read attachment metadata from File entities without downloading their content."""
        result = []
        for ref in mail.get(MAIL_ATTACHMENTS) or []:
            if not isinstance(ref, list) or len(ref) < 2:
                continue
            list_id, element_id = ref[0], ref[1]
            try:
                file_obj = await self._client.get_file(self._session, list_id, element_id)
                file_key = keys.session_key(
                    element_id,
                    file_obj.get(FILE_OWNER_ENC_SESSION_KEY),
                    await self._group_key(file_obj.get(FILE_OWNER_KEY_VERSION)),
                )
            except Exception as exc:
                logger.warning("Cannot read attachment %s: %s", element_id, exc)
                continue
            cid = self._decrypt_str(file_key, file_obj.get(FILE_CID) or "")
            result.append(
                AttachmentMeta(
                    filename=self._decrypt_str(file_key, file_obj.get(FILE_NAME, ""))
                    or "attachment",
                    content_type=self._decrypt_str(file_key, file_obj.get(FILE_MIME_TYPE) or "")
                    or "application/octet-stream",
                    size_bytes=int(file_obj.get(FILE_SIZE) or 0),
                    disposition="inline" if cid else "attachment",
                    content_id=cid or None,
                )
            )
        return result

    def _decrypt_str(self, key: bytes, value: str) -> str:
        decrypted: str = self._tuta.message_builder._decrypt_str(key, value)
        return decrypted


class _KeyResolver:
    """Resolves session keys of a mail and its attachments.

    Two paths, ported from tutaproxy's ``ImapSession._decrypt_mail_key`` and
    ``TutaClient.load_attachments``:
      * ``_ownerEncSessionKey`` encrypted with the mail group key (regular mail);
      * TutaCrypt PQ bucket key (field 1310) for Tuta-to-Tuta end-to-end mail.
    """

    def __init__(self, crypto: ModuleType, session: Any, mail: dict[str, Any]) -> None:
        self._crypto = crypto
        self._session = session
        self._mail = mail
        self._bucket: tuple[bytes, dict[str, str]] | None = None

    def session_key(
        self, element_id: str, owner_enc_session_key: str | None, group_key: bytes
    ) -> bytes:
        """``group_key`` must be the mail group key version the entity's owner key refers to."""
        if owner_enc_session_key:
            return decrypt_key(self._crypto, group_key, base64.b64decode(owner_enc_session_key))
        bucket_key, instance_keys = self._bucket_keys()
        enc_key = instance_keys.get(element_id)
        if not enc_key:
            raise MailboxError(f"no session key for {element_id} in bucket key")
        return decrypt_key(self._crypto, bucket_key, base64.b64decode(enc_key))

    def _bucket_keys(self) -> tuple[bytes, dict[str, str]]:
        if self._bucket is None:
            entry = _first(self._mail.get(MAIL_BUCKET_KEY))
            session = self._session
            pq_message = entry.get(BUCKET_KEY_PQ_MESSAGE) or ""
            if not pq_message or not session.priv_ecc or not session.kyber_sk:
                raise MailboxError(
                    "mail has no usable session key (no field 102, no PQ bucket key)"
                )
            bucket_key = self._crypto.pq_decapsulate_bucket_key(
                session.priv_ecc,
                session.pub_ecc,
                session.pub_kyber_tuta,
                session.kyber_sk,
                base64.b64decode(pq_message),
            )
            instance_keys = {
                e.get(INSTANCE_KEY_ELEMENT_ID): e.get(INSTANCE_KEY_ENC_SESSION_KEY, "")
                for e in entry.get(BUCKET_KEY_INSTANCE_KEYS) or []
            }
            self._bucket = (bucket_key, instance_keys)
        return self._bucket


def _drop_synthesized_plain_text(raw: bytes) -> bytes:
    """Remove the text/plain alternative that tutaproxy derives from the HTML body.

    Tuta stores message bodies as HTML only. ``build_rfc2822`` adds a text/plain part made
    with crude regexes that keep <style>/<script> contents, so the parser would prefer that
    lossy text over its own HTML extraction.
    """
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    changed = False
    for part in msg.walk():
        if part.get_content_type() != "multipart/alternative":
            continue
        subparts = part.get_payload()
        if not isinstance(subparts, list):
            continue
        kept = [
            p for p in subparts if not isinstance(p, str) and p.get_content_type() != "text/plain"
        ]
        if kept and len(kept) < len(subparts):
            part.set_payload(kept)
            changed = True
    return msg.as_bytes() if changed else raw


def decrypt_key(crypto: ModuleType, wrapping_key: bytes, encrypted_key: bytes) -> bytes:
    """Decrypt a key encrypted with another key, in either of Tuta's two formats.

    * 16/32 bytes: legacy unauthenticated key wrap, AES-CBC with a fixed IV and no padding
      (still used by older entities, e.g. custom folders);
    * otherwise: AesCbcThenHmac with a version byte, MAC and IV.
    """
    if len(encrypted_key) in (16, 32):
        if len(wrapping_key) == 32:
            key: bytes = crypto.aes256_decrypt_fixed_iv(wrapping_key, encrypted_key)
        else:
            key = crypto.aes128_decrypt_fixed_iv(wrapping_key, encrypted_key)
        return key
    key = crypto.aes_decrypt_tuta(wrapping_key, encrypted_key)
    return key


def _unwrap(value: Any) -> Any:
    """Single-valued associations sometimes arrive wrapped in a one-element list."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _last(value: Any) -> Any:
    if isinstance(value, list):
        return value[-1] if value else ""
    return value


def _custom_id(value: str) -> str:
    """Tuta CustomId: base64url of the UTF-8 string, without padding."""
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_custom_id(value: str) -> str:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()


def _one_line(value: str) -> str:
    """Drop CR/LF so decrypted values cannot inject extra header lines."""
    return " ".join(value.split()) if ("\r" in value or "\n" in value) else value.strip()


def _first(value: Any) -> dict[str, Any]:
    """Tuta aggregations arrive either as a dict or as a one-element list."""
    if isinstance(value, list):
        value = value[0] if value else {}
    return value if isinstance(value, dict) else {}


def _element_id(mail: dict[str, Any]) -> str:
    mail_id = mail.get(MAIL_ID, "")
    return str(mail_id[-1]) if isinstance(mail_id, list) and mail_id else str(mail_id)
