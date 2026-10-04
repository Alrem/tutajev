"""The question every classifier answers, and the message state it is asked about.

Shared by all backends so that their verdicts are comparable.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from tutajev.classifier.port import LABELS, ClassifierError, Label
from tutajev.models import Address, MessageSummary

# Bodies of marketing mail are long and repetitive; the opening part carries the signal.
MAX_BODY_CHARS = 4000
MAX_URLS = 30

QUESTION_NAME = "category"
INSTRUCTIONS = (
    "Classify this email received in a personal mailbox. Judge only from the sender, "
    "reply-to, subject, body, links and attachments provided."
)
CRITERIA: dict[str, str] = {
    "legitimate": (
        "Personal or transactional mail the recipient expects: conversations, receipts, "
        "account notifications, security alerts from a real service."
    ),
    "promotional": (
        "Marketing or newsletters from a real company the recipient has likely subscribed to; "
        "bulk but not deceptive."
    ),
    "spam": (
        "Unsolicited bulk mail the recipient never asked for: unknown senders, adult content, "
        "get-rich-quick offers, scams that do not ask for credentials."
    ),
    "phishing": (
        "Tries to steal credentials, payment data or money by impersonating a trusted party, "
        "often with urgent language, mismatched reply-to or lookalike links."
    ),
}


def _address(address: Address | None) -> str | None:
    if address is None:
        return None
    return f"{address.name} <{address.email}>" if address.name else address.email


def build_state(message: MessageSummary) -> dict[str, Any]:
    """Build the JSON state the classifier evaluates from the extracted message fields."""
    body = message.text_body
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + " [truncated]"
    return {
        "from": _address(message.sender),
        "reply_to": [_address(a) for a in message.reply_to],
        "reply_to_differs_from_sender": message.reply_to_differs,
        "subject": message.subject,
        "body": body,
        "link_domains": sorted(
            {urlsplit(_with_scheme(u)).hostname or "" for u in message.urls} - {""}
        ),
        "links": list(message.urls[:MAX_URLS]),
        "attachments": [
            {"filename": a.filename, "content_type": a.content_type, "size_bytes": a.size_bytes}
            for a in message.attachments
        ],
    }


def _with_scheme(url: str) -> str:
    return url if "://" in url else f"http://{url}"


def as_label(value: str, source: str) -> Label:
    for label in LABELS:
        if label == value:
            return label
    raise ClassifierError(f"{source} returned unknown label {value!r}")
