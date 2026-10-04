"""Turn RFC 822 bytes into a :class:`MessageSummary`. Pure functions, no I/O."""

from __future__ import annotations

import email
import email.policy
import email.utils
import logging
import re
from datetime import datetime
from email.message import EmailMessage
from html.parser import HTMLParser

from tutajev.models import Address, AttachmentMeta, MessageSummary, RawMessage

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"""(?:https?://|www\.)[^\s<>"'()\[\]{}]+""", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?'\""
_URL_ATTRS = frozenset({"href", "src"})
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})
_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table"})


class _HtmlExtractor(HTMLParser):
    """Collects visible text and link targets from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.urls: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")
        for name, value in attrs:
            if name in _URL_ATTRS and value and _looks_like_url(value):
                self.urls.append(value.strip())

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.text_parts.append(data)

    def text(self) -> str:
        joined = "".join(self.text_parts)
        lines = (" ".join(line.split()) for line in joined.splitlines())
        return "\n".join(line for line in lines if line)


def _looks_like_url(value: str) -> bool:
    return value.strip().lower().startswith(("http://", "https://", "www."))


def html_to_text(html: str) -> tuple[str, list[str]]:
    """Return visible text and ``href``/``src`` URLs of an HTML document."""
    extractor = _HtmlExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text(), extractor.urls


def extract_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(_TRAILING_PUNCT) for m in _URL_RE.finditer(text)]


def _dedupe(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


def _parse_addresses(header_values: list[str]) -> tuple[Address, ...]:
    return tuple(
        Address(name=name, email=addr)
        for name, addr in email.utils.getaddresses(header_values)
        if addr
    )


def _part_text(part: EmailMessage) -> str:
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError):  # unknown or wrong charset
        payload = part.get_payload(decode=True)
        content = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""
    return content if isinstance(content, str) else ""


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _attachments_from_mime(msg: EmailMessage) -> tuple[AttachmentMeta, ...]:
    result = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not (part.is_attachment() or filename):
            continue
        payload = part.get_payload(decode=True)
        content_id = part.get("Content-ID")
        result.append(
            AttachmentMeta(
                filename=filename or "attachment",
                content_type=part.get_content_type(),
                size_bytes=len(payload) if isinstance(payload, bytes) else 0,
                disposition=part.get_content_disposition() or "attachment",
                content_id=content_id.strip("<> ") if content_id else None,
            )
        )
    return tuple(result)


def parse_message(raw: RawMessage) -> MessageSummary:
    msg = email.message_from_bytes(raw.raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    senders = _parse_addresses([str(v) for v in msg.get_all("From", [])])
    reply_to = _parse_addresses([str(v) for v in msg.get_all("Reply-To", [])])
    sender = senders[0] if senders else None

    plain_parts: list[str] = []
    html_parts: list[str] = []
    for part in msg.walk():
        if part.is_multipart() or part.is_attachment():
            continue
        content_type = part.get_content_type()
        if content_type == "text/plain":
            plain_parts.append(_part_text(part))
        elif content_type == "text/html":
            html_parts.append(_part_text(part))

    urls: list[str] = []
    html_texts = []
    for html in html_parts:
        text, html_urls = html_to_text(html)
        html_texts.append(text)
        urls.extend(html_urls)
    text_body = "\n\n".join(p.strip() for p in plain_parts if p.strip())
    if not text_body:
        text_body = "\n\n".join(t for t in html_texts if t)
    urls = extract_urls(text_body) + urls

    sender_email = sender.email.lower() if sender else ""
    reply_to_differs = bool(reply_to) and any(a.email.lower() != sender_email for a in reply_to)

    return MessageSummary(
        id=raw.id,
        received_at=raw.received_at,
        date=_parse_date(msg.get("Date")),
        sender=sender,
        reply_to=reply_to,
        subject=str(msg.get("Subject", "") or ""),
        text_body=text_body,
        urls=_dedupe(urls),
        attachments=raw.attachments if raw.attachments is not None else _attachments_from_mime(msg),
        has_html=bool(html_parts),
        reply_to_differs=reply_to_differs,
        folder=raw.folder,
        authentication_results=tuple(
            " ".join(str(v).split()) for v in msg.get_all("Authentication-Results") or []
        ),
    )
