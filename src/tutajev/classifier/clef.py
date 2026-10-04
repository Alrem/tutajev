"""Spam classifier backed by Cloudflare's Clef decision models on Workers AI.

Clef speaks the same System One request format as Jev, so it gets the same question and
state. Message content (sender, subject, body excerpt, URLs, attachment metadata) is sent
to Cloudflare's API. Nothing is sent back to the mailbox.
"""

from __future__ import annotations

import logging
from types import TracebackType
from typing import Any, Literal, Self

import aiohttp

from tutajev.classifier.port import ClassifierError, Verdict
from tutajev.classifier.prompt import CRITERIA, INSTRUCTIONS, QUESTION_NAME, as_label, build_state
from tutajev.models import MessageSummary

logger = logging.getLogger(__name__)

ClefModel = Literal["clef", "clef-flash"]
CLEF_MODELS: tuple[ClefModel, ...] = ("clef", "clef-flash")
API_BASE = "https://api.cloudflare.com/client/v4"
REQUEST_TIMEOUT_SECONDS = 60


class ClefClassifier:
    """:class:`~tutajev.classifier.port.SpamClassifier` using the Workers AI REST API."""

    def __init__(
        self,
        account_id: str,
        api_token: str,
        model: ClefModel = "clef",
        session: aiohttp.ClientSession | None = None,
        api_base: str = API_BASE,
    ) -> None:
        if model not in CLEF_MODELS:
            raise ValueError(f"Unknown Clef model {model!r}")
        self._url = f"{api_base}/accounts/{account_id}/ai/run/@cf/cloudflare/{model}"
        self._api_token = api_token
        self._model = model
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> Self:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def classify(self, message: MessageSummary) -> Verdict:
        if self._session is None:
            raise ClassifierError("ClefClassifier must be used as an async context manager")
        payload = {
            "model": self._model,
            "state": build_state(message),
            "questions": {
                QUESTION_NAME: {
                    "type": "choice",
                    "instructions": INSTRUCTIONS,
                    "criteria": CRITERIA,
                }
            },
        }
        headers = {"Authorization": f"Bearer {self._api_token}"}
        async with self._session.post(self._url, json=payload, headers=headers) as response:
            if response.status in (401, 403):
                raise ClassifierError(
                    f"Cloudflare rejected the API token ({response.status}): "
                    f"{_errors(await _json(response))}"
                )
            body = await _json(response)
            if response.status >= 400 or body.get("success") is False:
                # Per-message failure: the CLI logs it and leaves the message alone.
                raise RuntimeError(f"Clef request failed ({response.status}): {_errors(body)}")

        # Workers AI wraps model output in {"result": ..., "success": true}.
        result = body.get("result", body)
        try:
            answer = result["answers"][QUESTION_NAME]
            probabilities = {k: float(v) for k, v in answer["probabilities"].items()}
            choice, confidence = answer["choice"], float(answer["confidence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Unexpected Clef response: {result!r:.300}") from exc
        logger.debug("Message %s: %s (%.2f)", message.id, choice, confidence)
        return Verdict(
            label=as_label(choice, source="Clef"),
            confidence=confidence,
            probabilities=probabilities,
            model=str(result.get("model") or self._model),
        )


async def _json(response: aiohttp.ClientResponse) -> dict[str, Any]:
    try:
        body = await response.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return {"errors": [{"message": (await response.text())[:300]}]}
    return body if isinstance(body, dict) else {"result": body}


def _errors(body: dict[str, Any]) -> str:
    errors = body.get("errors") or []
    messages = [str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors]
    return "; ".join(messages) or "no details"
