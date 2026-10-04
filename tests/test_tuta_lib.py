"""TutaLibReader tests: a fake TutaClient serves data encrypted with tutaproxy's real crypto."""

from __future__ import annotations

import base64
import os
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import TUTAPROXY_PATH
from tutajev.mailbox import MailboxError
from tutajev.mailbox._readonly import ReadOnlyTutaClient, ReadOnlyViolation
from tutajev.parsing import parse_message

pytestmark = pytest.mark.skipif(
    not (TUTAPROXY_PATH / "tuta" / "api.py").is_file(),
    reason="vendor/tutaproxy missing; run scripts/fetch_tutaproxy.sh",
)

# 9 bytes of session list id + the secret part, as in real Tuta access tokens
ACCESS_TOKEN = base64.urlsafe_b64encode(bytes(range(25))).decode().rstrip("=")
GROUP_KEY = bytes(range(32))  # current mail group key, version 1
FORMER_GROUP_KEY = bytes(range(32, 64))  # rotated-out version 0


def _crypto() -> Any:
    from tutajev.mailbox.tuta_lib import load_tuta

    return load_tuta(TUTAPROXY_PATH).crypto


def _enc(key: bytes, data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.b64encode(_crypto().aes_encrypt_tuta(key, raw)).decode()


def _legacy_wrap(wrapping_key: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = _crypto().TUTA_FIXED_IV
    encryptor = Cipher(algorithms.AES(wrapping_key), modes.CBC(iv)).encryptor()
    return encryptor.update(key) + encryptor.finalize()


def test_decrypt_key_handles_both_formats() -> None:
    from tutajev.mailbox.tuta_lib import decrypt_key

    key = os.urandom(32)
    crypto = _crypto()
    assert decrypt_key(crypto, GROUP_KEY, _legacy_wrap(GROUP_KEY, key)) == key
    authenticated = crypto.aes_encrypt_tuta(GROUP_KEY, key, add_padding=False)
    assert decrypt_key(crypto, GROUP_KEY, authenticated) == key


@dataclass
class FakeMail:
    element_id: str
    received_ms: int
    subject: str
    sender: tuple[str, str]
    html: str
    reply_tos: list[tuple[str, str]] = field(default_factory=list)
    attachments: list[tuple[str, str, int, str]] = field(default_factory=list)
    session_key: bytes = field(default_factory=lambda: os.urandom(32))
    unread: bool = False
    # Original transport headers; stored compressed unless headers_plain is set.
    headers: str = ""
    headers_plain: bool = False


class FakeTutaClient:
    """Mimics the subset of tutaproxy's TutaClient used by the adapter, plus a mutating method."""

    def __init__(self, mails: list[FakeMail], fail_login: bool = False) -> None:
        self.mails = {m.element_id: m for m in mails}
        self.fail_login = fail_login
        self.fail_delete = False
        self.calls: list[str] = []
        self.files: dict[str, dict[str, Any]] = {}
        self.closed = False

    async def __aenter__(self) -> FakeTutaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True

    async def login(self, email: str, password: str) -> Any:
        self.calls.append("login")
        if self.fail_login:
            raise RuntimeError("invalid password")
        return SimpleNamespace(
            priv_ecc=None,
            kyber_sk=None,
            mail_group_key_version="1",
            mail_group_id="mail-group",
            access_token=ACCESS_TOKEN,
        )

    async def logout(self, session: Any) -> None:
        raise AssertionError("tutaproxy's logout is broken and must not be used")

    async def _delete(self, url: str, token: str) -> None:
        self.calls.append(f"_delete:{url}")
        if self.fail_delete:
            raise RuntimeError("server said no")

    async def get_folders(self, session: Any) -> list[Any]:
        self.calls.append("get_folders")
        folder_key = bytes(32)
        custom = {
            "id": "c1",
            "folder_type": "0",
            "mail_list_id": "custom-list",
            # Like real old folders: legacy key wrap with the former (version 0) group key.
            "owner_enc_session_key": base64.b64encode(
                _legacy_wrap(FORMER_GROUP_KEY, folder_key)
            ).decode(),
            "owner_key_version": "0",
            "name_encrypted": _enc(folder_key, "Newsletters"),
        }
        return [
            SimpleNamespace(
                id="s", folder_type="5", mail_list_id="spam-list", owner_key_version="1"
            ),
            SimpleNamespace(
                id="i", folder_type="1", mail_list_id="inbox-list", owner_key_version="1"
            ),
            SimpleNamespace(**custom),
        ]

    async def get_mails_in_folder(self, session: Any, list_id: str) -> list[dict[str, Any]]:
        self.calls.append(f"get_mails_in_folder:{list_id}")
        return [self._mail_entity(m) for m in self.mails.values()]

    async def get_mail_group_key(self, session: Any) -> bytes:
        self.calls.append("get_mail_group_key")
        return GROUP_KEY

    async def get_mail_details(self, session: Any, mail: dict[str, Any]) -> dict[str, Any]:
        m = self.mails[mail["99"][1]]
        crypto = _crypto()
        body = base64.b64encode(
            crypto.aes_encrypt_tuta(m.session_key, crypto.compress_lz4(m.html.encode()))
        ).decode()
        details = {
            "1284": str(m.received_ms),
            "1286": {"1279": [{"94": _enc(m.session_key, "Me"), "95": "me@tuta.com"}]},
            "1288": [{"1276": body}],
            "1285": [
                {"614": _enc(m.session_key, name), "615": _enc(m.session_key, addr)}
                for name, addr in m.reply_tos
            ],
        }
        if m.headers and m.headers_plain:
            details["1287"] = [{"1271": _enc(m.session_key, m.headers)}]
        elif m.headers:
            compressed = crypto.aes_encrypt_tuta(
                m.session_key, crypto.compress_lz4(m.headers.encode())
            )
            details["1287"] = [{"1272": base64.b64encode(compressed).decode()}]
        return {"1305": [details]}

    def _url(self, app: str, service: str, *ids: str) -> str:
        return "/".join((app, service, *ids))

    async def _get(self, url: str, token: str = "", params: Any = None) -> Any:
        self.calls.append(f"_get:{url}")
        if url == "sys/group/mail-group":
            return {"2273": [{"2269": ["former-keys"]}]}  # shapes as returned by the real API
        if url == "sys/groupkey/former-keys":
            assert params == {"start": "MQ", "count": "100", "reverse": "true"}
            wrapped = base64.b64encode(_legacy_wrap(GROUP_KEY, FORMER_GROUP_KEY)).decode()
            return [{"2257": ["former-keys", "MA"], "2261": wrapped}]  # "MA" = CustomId("0")
        raise AssertionError(f"unexpected GET {url}")

    async def get_file(self, session: Any, list_id: str, element_id: str) -> dict[str, Any]:
        self.calls.append(f"get_file:{element_id}")
        return self.files[element_id]

    async def simple_move_mails(
        self, session: Any, mail_ids: list[tuple[str, str]], destination: str
    ) -> None:
        self.calls.append(f"simple_move_mails:{destination}:{mail_ids}")

    async def delete_mails(self, *args: Any) -> None:
        raise AssertionError("delete_mails must never be reached")

    def _mail_entity(self, m: FakeMail) -> dict[str, Any]:
        file_refs = []
        for index, (name, mime, size, cid) in enumerate(m.attachments):
            file_id = f"{m.element_id}-f{index}"
            file_key = os.urandom(32)
            self.files[file_id] = {
                "18": _enc(GROUP_KEY, file_key),
                "21": _enc(file_key, name),
                "22": str(size),
                "23": _enc(file_key, mime),
                "924": _enc(file_key, cid) if cid else None,
            }
            file_refs.append(["file-list", file_id])
        return {
            "99": ["bag", m.element_id],
            "102": _enc(GROUP_KEY, m.session_key),
            "105": _enc(m.session_key, m.subject),
            "107": str(m.received_ms),
            "109": "1" if m.unread else "0",
            "111": {"94": _enc(m.session_key, m.sender[0]), "95": m.sender[1]},
            "115": file_refs,
        }


def _reader(client: FakeTutaClient, allow_move: bool = False) -> Any:
    from tutajev.mailbox.tuta_lib import TutaLibReader

    return TutaLibReader(
        "me@tuta.com",
        "pw",
        TUTAPROXY_PATH,
        client_factory=lambda: client,
        allow_move_to_spam=allow_move,
    )


def _mails() -> list[FakeMail]:
    return [
        FakeMail(
            "old",
            1_000_000,
            "Old",
            ("Old Sender", "old@example.com"),
            "<p>old</p>",
            headers="Authentication-Results: mx.example; spf=pass\r\n",
            headers_plain=True,
        ),
        FakeMail(
            "new",
            3_000_000,
            "Free money",
            ("Банк", "bank@spam.example"),
            '<p>Click <a href="https://phish.example/login">here</a></p>',
            reply_tos=[("Support", "collect@else.example")],
            headers=(
                "Received: from relay.example\r\n"
                "Authentication-Results: w10.tutanota.de (dis=spam);\r\n"
                " dmarc=fail (dis=spam p=quarantine) header.from=spam.example\r\n"
                "Subject: ignored\r\n"
            ),
            attachments=[
                ("invoice.zip", "application/zip", 4096, ""),
                ("i.png", "image/png", 12, "c1"),
            ],
        ),
        FakeMail("mid", 2_000_000, "Mid", ("Mid", "mid@example.com"), "<p>mid</p>"),
    ]


async def test_lists_newest_messages_and_extracts_fields() -> None:
    client = FakeTutaClient(_mails())
    async with _reader(client) as reader:
        raw_messages = await reader.list_recent(limit=2)

    assert [r.id for r in raw_messages] == ["new", "mid"]
    assert "get_mails_in_folder:inbox-list" in client.calls
    assert client.calls[-1] == _session_delete_call()
    assert client.closed

    summary = parse_message(raw_messages[0])
    assert summary.subject == "Free money"
    assert summary.sender is not None
    assert (summary.sender.name, summary.sender.email) == ("Банк", "bank@spam.example")
    assert [(a.name, a.email) for a in summary.reply_to] == [("Support", "collect@else.example")]
    assert summary.reply_to_differs is True
    assert summary.authentication_results == (
        "w10.tutanota.de (dis=spam); dmarc=fail (dis=spam p=quarantine) header.from=spam.example",
    )
    assert summary.subject == "Free money"
    assert "https://phish.example/login" in summary.urls
    assert "Click here" in summary.text_body
    assert [
        (a.filename, a.content_type, a.size_bytes, a.disposition, a.content_id)
        for a in summary.attachments
    ] == [
        ("invoice.zip", "application/zip", 4096, "attachment", None),
        ("i.png", "image/png", 12, "inline", "c1"),
    ]
    assert summary.received_at is not None and summary.received_at.timestamp() == 3000


async def test_body_text_comes_from_html_not_tutaproxy_plain_text() -> None:
    html = "<html><head><style>body,td { font-family: verdana; }</style></head><p>Hello</p></html>"
    client = FakeTutaClient([FakeMail("s", 1, "Styled", ("S", "s@example.com"), html)])
    async with _reader(client) as reader:
        (raw,) = await reader.list_recent(limit=1)

    summary = parse_message(raw)
    assert summary.text_body == "Hello"
    assert summary.has_html is True


async def test_unread_only_returns_all_unread_messages() -> None:
    mails = _mails()
    mails[0].unread = True  # "old"
    mails[2].unread = True  # "mid"
    client = FakeTutaClient(mails)
    async with _reader(client) as reader:
        raw_messages = await reader.list_recent(limit=None, unread_only=True)
    assert [r.id for r in raw_messages] == ["mid", "old"]


async def test_selects_spam_folder_by_type() -> None:
    client = FakeTutaClient(_mails())
    async with _reader(client) as reader:
        await reader.list_recent(folder="spam", limit=1)
    assert "get_mails_in_folder:spam-list" in client.calls


async def test_selects_custom_folder_by_decrypted_name() -> None:
    client = FakeTutaClient(_mails())
    async with _reader(client) as reader:
        assert await reader.list_folders() == [
            "inbox",
            "spam",
            "trash",
            "archive",
            "sent",
            "Newsletters",
        ]
        raw_messages = await reader.list_recent(folder="NEWSLETTERS", limit=1)
    assert "get_mails_in_folder:custom-list" in client.calls
    assert client.calls.count("_get:sys/groupkey/former-keys") == 1  # former keys cached
    assert raw_messages[0].folder == "NEWSLETTERS"


async def test_unknown_custom_folder_lists_available() -> None:
    client = FakeTutaClient(_mails())
    async with _reader(client) as reader:
        with pytest.raises(MailboxError, match="custom folders: Newsletters"):
            await reader.list_recent(folder="nope")


async def test_login_failure_raises_mailbox_error_and_closes_client() -> None:
    client = FakeTutaClient([], fail_login=True)
    with pytest.raises(MailboxError, match="login failed"):
        async with _reader(client):
            pass
    assert client.closed
    assert not any(c.startswith("_delete:") for c in client.calls)


async def test_logout_runs_when_listing_fails() -> None:
    client = FakeTutaClient(_mails())
    with pytest.raises(RuntimeError):
        async with _reader(client):
            raise RuntimeError("boom")
    assert client.calls[-1] == _session_delete_call()


async def test_failed_session_close_is_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeTutaClient(_mails())
    client.fail_delete = True
    async with _reader(client):
        pass
    assert "stays open" in caplog.text
    assert client.closed


def _session_delete_call() -> str:
    from tutajev.mailbox.tuta_lib import load_tuta

    list_id, element_id = load_tuta(TUTAPROXY_PATH).api._session_id_from_access_token(ACCESS_TOKEN)
    return f"_delete:sys/session/{list_id}/{element_id}"


async def test_undecryptable_message_is_skipped() -> None:
    client = FakeTutaClient(_mails())
    original = client._mail_entity

    def broken(m: FakeMail) -> dict[str, Any]:
        entity = original(m)
        if m.element_id == "mid":
            entity["102"] = None  # no owner key and no PQ bucket key
        return entity

    client._mail_entity = broken  # type: ignore[method-assign]
    async with _reader(client) as reader:
        raw_messages = await reader.list_recent(limit=3)
    assert [r.id for r in raw_messages] == ["new", "old"]


async def test_move_to_spam_uses_spam_system_folder() -> None:
    client = FakeTutaClient(_mails())
    async with _reader(client, allow_move=True) as reader:
        await reader.list_recent(limit=3)
        await reader.move_to_spam(["new"])
    assert "simple_move_mails:5:[('bag', 'new')]" in client.calls


async def test_move_to_spam_blocked_without_permission() -> None:
    client = FakeTutaClient(_mails())
    async with _reader(client) as reader:
        await reader.list_recent(limit=3)
        with pytest.raises(MailboxError, match="not allowed"):
            await reader.move_to_spam(["new"])
    assert not any(c.startswith("simple_move_mails") for c in client.calls)


async def test_move_to_spam_refuses_unlisted_messages() -> None:
    client = FakeTutaClient(_mails())
    async with _reader(client, allow_move=True) as reader:
        await reader.list_recent(limit=1)
        with pytest.raises(MailboxError, match="not listed"):
            await reader.move_to_spam(["old"])


def test_read_only_guard_blocks_mutations() -> None:
    guarded = ReadOnlyTutaClient(FakeTutaClient([]))
    for name in (
        "delete_mails",
        "move_mails_to_folder",
        "mark_mails_unread",
        "apply_labels",
        "logout",
        "_delete",
    ):
        with pytest.raises(ReadOnlyViolation):
            getattr(guarded, name)
    assert callable(guarded.get_folders)
    with pytest.raises(ReadOnlyViolation):
        guarded.simple_move_mails  # noqa: B018


def test_move_permission_unlocks_only_simple_move() -> None:
    guarded = ReadOnlyTutaClient(FakeTutaClient([]), allow_move=True)
    assert callable(guarded.simple_move_mails)
    for name in ("delete_mails", "move_mails_to_folder", "mark_mails_unread"):
        with pytest.raises(ReadOnlyViolation):
            getattr(guarded, name)


def test_telemetry_module_is_never_imported() -> None:
    _crypto()
    assert "tuta.api" in sys.modules
    assert "tuta.telemetry" not in sys.modules
    assert os.environ.get("TUTAPROXY_TELEMETRY") == "false"


async def test_reads_uncompressed_original_headers() -> None:
    async with _reader(FakeTutaClient(_mails())) as reader:
        raw_messages = await reader.list_recent(limit=None)

    old = parse_message(next(r for r in raw_messages if r.id == "old"))
    mid = parse_message(next(r for r in raw_messages if r.id == "mid"))
    assert old.authentication_results == ("mx.example; spf=pass",)
    assert mid.authentication_results == ()
