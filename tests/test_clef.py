from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from tests.conftest import load_eml
from tutajev.classifier import ClassifierError
from tutajev.classifier.clef import ClefClassifier
from tutajev.classifier.prompt import CRITERIA, QUESTION_NAME
from tutajev.models import MessageSummary
from tutajev.parsing import parse_message

PROBABILITIES = {"legitimate": 0.05, "promotional": 0.05, "spam": 0.2, "phishing": 0.7}


class FakeWorkersAI:
    def __init__(self) -> None:
        self.status = 200
        self.body: dict[str, Any] = {
            "success": True,
            "errors": [],
            "result": {
                "model": "clef-flash",
                "answers": {
                    QUESTION_NAME: {
                        "type": "choice",
                        "choice": "phishing",
                        "probabilities": PROBABILITIES,
                        "confidence": 0.7,
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 1},
            },
        }
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append(
            (request.path, request.headers.get("Authorization", ""), await request.json())
        )
        return web.json_response(self.body, status=self.status)


@pytest.fixture
async def api() -> AsyncIterator[tuple[FakeWorkersAI, str]]:
    fake = FakeWorkersAI()
    app = web.Application()
    app.router.add_post("/accounts/{account}/ai/run/@cf/cloudflare/{model}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    yield fake, str(server.make_url("")).rstrip("/")
    await server.close()


def _message() -> MessageSummary:
    return parse_message(load_eml("html_only.eml"))


def _classifier(base: str, model: Any = "clef-flash") -> ClefClassifier:
    return ClefClassifier(account_id="acc", api_token="tok", model=model, api_base=base)


async def test_classify_sends_system_one_request(api: tuple[FakeWorkersAI, str]) -> None:
    fake, base = api
    async with _classifier(base) as classifier:
        verdict = await classifier.classify(_message())

    assert verdict.label == "phishing"
    assert verdict.confidence == 0.7
    assert verdict.spam_score == pytest.approx(0.9)
    assert verdict.model == "clef-flash"
    path, auth, payload = fake.requests[0]
    assert path == "/accounts/acc/ai/run/@cf/cloudflare/clef-flash"
    assert auth == "Bearer tok"
    assert payload["model"] == "clef-flash"
    assert payload["state"]["subject"] == "You won €1000!"
    question = payload["questions"][QUESTION_NAME]
    assert question["type"] == "choice"
    assert question["criteria"] == CRITERIA


async def test_unwrapped_response_is_accepted(api: tuple[FakeWorkersAI, str]) -> None:
    fake, base = api
    fake.body = fake.body["result"]
    async with _classifier(base) as classifier:
        assert (await classifier.classify(_message())).label == "phishing"


async def test_bad_token_is_fatal(api: tuple[FakeWorkersAI, str]) -> None:
    fake, base = api
    fake.status = 403
    fake.body = {"success": False, "errors": [{"code": 10000, "message": "Authentication error"}]}
    async with _classifier(base) as classifier:
        with pytest.raises(ClassifierError, match="Authentication error"):
            await classifier.classify(_message())


async def test_server_error_fails_only_this_message(api: tuple[FakeWorkersAI, str]) -> None:
    fake, base = api
    fake.status = 500
    fake.body = {"success": False, "errors": [{"message": "overloaded"}]}
    async with _classifier(base) as classifier:
        with pytest.raises(RuntimeError, match="overloaded"):
            await classifier.classify(_message())


async def test_unknown_label_is_rejected(api: tuple[FakeWorkersAI, str]) -> None:
    fake, base = api
    fake.body["result"]["answers"][QUESTION_NAME]["choice"] = "weird"
    async with _classifier(base) as classifier:
        with pytest.raises(ClassifierError, match="unknown label"):
            await classifier.classify(_message())


def test_unknown_model_is_rejected() -> None:
    with pytest.raises(ValueError, match="Clef model"):
        ClefClassifier(account_id="a", api_token="t", model="clef-xl")  # type: ignore[arg-type]
