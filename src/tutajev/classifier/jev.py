"""Spam classifier backed by Jev, TypeSafe's System One model (``typesafe-sdk``).

Message content (sender, subject, body excerpt, URLs, attachment metadata) is sent to
TypeSafe's API. Nothing is sent back to the mailbox.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Self

from tutajev.classifier.port import ClassifierError, Verdict
from tutajev.classifier.prompt import CRITERIA, INSTRUCTIONS, QUESTION_NAME, as_label, build_state
from tutajev.models import MessageSummary

logger = logging.getLogger(__name__)


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
            label=as_label(answer.choice, source="Jev"),
            confidence=float(answer.confidence),
            probabilities={k: float(v) for k, v in answer.probabilities.items()},
            model=str(response.model),
        )
