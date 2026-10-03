"""Plain data structures shared by the mailbox adapters, the parser and the output layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Address:
    name: str
    email: str


@dataclass(frozen=True)
class AttachmentMeta:
    filename: str
    content_type: str
    size_bytes: int
    disposition: str = "attachment"
    content_id: str | None = None


@dataclass(frozen=True)
class RawMessage:
    """A message as delivered by a mailbox adapter: RFC 822 bytes plus optional hints.

    ``attachments`` lets a backend supply attachment metadata it already knows without
    downloading attachment content. When it is ``None`` the parser reads attachments from
    the MIME structure of ``raw``.
    """

    id: str
    received_at: datetime | None
    raw: bytes
    attachments: tuple[AttachmentMeta, ...] | None = None
    folder: str = ""


@dataclass(frozen=True)
class MessageSummary:
    id: str
    received_at: datetime | None
    date: datetime | None
    sender: Address | None
    reply_to: tuple[Address, ...]
    subject: str
    text_body: str
    urls: tuple[str, ...]
    attachments: tuple[AttachmentMeta, ...]
    has_html: bool
    reply_to_differs: bool = field(default=False)
    folder: str = ""
