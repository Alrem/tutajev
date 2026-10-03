"""Spam classifier backed by Jev, TypeSafe's System One model (``typesafe-sdk``).

Message content (sender, subject, body excerpt, URLs, attachment metadata) is sent to
TypeSafe's API. Nothing is sent back to the mailbox.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

from tutajev.classifier.port import LABELS, ClassifierError, Label, Verdict
from tutajev.models import Address, MessageSummary

logger = logging.getLogger(__name__)

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
    """Build the JSON state Jev evaluates from the extracted message fields."""
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


class JevClassifier:
    """:class:`~tutajev.classifier.port.SpamClassifier` using ``AsyncTypeSafeClient.system_one``."""

    def __init__(self, api_key: str, model: str | None = None, client: Any | None = None) -> None:
        self._api_key = api_key
        self._model = model
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        if self._client is None:
            from typesafe_sdk import AsyncTypeSafeClient

            self._client = AsyncTypeSafeClient(api_key=self._api_key, model=self._model)
        if self._owns_client:
            await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client is not None:
            await self._client.__aexit__(exc_type, exc, tb)

    async def classify(self, message: MessageSummary) -> Verdict:
        from typesafe_sdk import Choice, TypeSafeAuthenticationError, TypeSafePermissionDeniedError

        if self._client is None:
            raise ClassifierError("JevClassifier must be used as an async context manager")
        try:
            response = await self._client.system_one(
                state=build_state(message),
                questions={QUESTION_NAME: Choice(instructions=INSTRUCTIONS, criteria=CRITERIA)},
            )
        except (TypeSafeAuthenticationError, TypeSafePermissionDeniedError) as exc:
            raise ClassifierError(f"Jev rejected the API key: {exc}") from exc

        answer = response.choices[QUESTION_NAME]
        logger.debug("Message %s: %s (%.2f)", message.id, answer.choice, answer.confidence)
        return Verdict(
            label=_as_label(answer.choice),
            confidence=float(answer.confidence),
            probabilities={k: float(v) for k, v in answer.probabilities.items()},
            model=str(response.model),
        )


def _as_label(value: str) -> Label:
    for label in LABELS:
        if label == value:
            return label
    raise ClassifierError(f"Jev returned unknown label {value!r}")
