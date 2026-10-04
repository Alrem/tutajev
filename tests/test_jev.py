from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from typesafe_sdk import Choice, TypeSafeAuthenticationError

from tests.conftest import load_eml
from tutajev.classifier import ClassifierError
from tutajev.classifier.jev import JevClassifier
from tutajev.classifier.prompt import CRITERIA, MAX_BODY_CHARS, QUESTION_NAME, build_state
from tutajev.models import MessageSummary
from tutajev.parsing import parse_message


class FakeTypeSafeClient:
    def __init__(self, choice: str = "phishing", error: Exception | None = None) -> None:
        self.choice = choice
        self.error = error
        self.requests: list[dict[str, Any]] = []

    async def system_one(self, state: Any, questions: Any) -> Any:
        self.requests.append({"state": state, "questions": questions})
        if self.error:
            raise self.error
        probabilities = {"legitimate": 0.05, "promotional": 0.05, "spam": 0.2, "phishing": 0.7}
        answer = SimpleNamespace(choice=self.choice, confidence=0.7, probabilities=probabilities)
        return SimpleNamespace(model="jev-1", choices={QUESTION_NAME: answer})


def _message() -> MessageSummary:
    return parse_message(load_eml("html_only.eml"))


def test_build_state_contains_signals() -> None:
    state = build_state(_message())

    assert state["from"] == "Призов <prize@lottery.example>"
    assert state["reply_to"] == ["Claims Desk <claims@other.example>"]
    assert state["reply_to_differs_from_sender"] is True
    assert state["subject"] == "You won €1000!"
    assert state["link_domains"] == ["claim.example", "tracker.example"]
    assert state["attachments"] == []


def test_build_state_truncates_long_body() -> None:
    message = _message()
    long = MessageSummary(**{**message.__dict__, "text_body": "x" * (MAX_BODY_CHARS + 100)})
    assert build_state(long)["body"].endswith(" [truncated]")
    assert len(build_state(long)["body"]) == MAX_BODY_CHARS + len(" [truncated]")


async def test_classify_returns_verdict() -> None:
    client = FakeTypeSafeClient()
    async with JevClassifier(api_key="k", client=client) as classifier:
        verdict = await classifier.classify(_message())

    assert verdict.label == "phishing"
    assert verdict.confidence == 0.7
    assert verdict.spam_score == pytest.approx(0.9)
    assert verdict.model == "jev-1"
    question = client.requests[0]["questions"][QUESTION_NAME]
    assert isinstance(question, Choice)
    assert set(question.criteria) == set(CRITERIA)


async def test_unknown_label_is_rejected() -> None:
    async with JevClassifier(api_key="k", client=FakeTypeSafeClient(choice="weird")) as c:
        with pytest.raises(ClassifierError, match="unknown label"):
            await c.classify(_message())


async def test_authentication_error_becomes_classifier_error() -> None:
    error = TypeSafeAuthenticationError(401, None, httpx2.Headers(), message="bad key")
    async with JevClassifier(api_key="k", client=FakeTypeSafeClient(error=error)) as c:
        with pytest.raises(ClassifierError, match="API key"):
            await c.classify(_message())
