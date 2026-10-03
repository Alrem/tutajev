"""The classifier port: the interface the application uses to classify a message."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from typing import Literal, Protocol, Self

from tutajev.models import MessageSummary

Label = Literal["legitimate", "promotional", "spam", "phishing"]
LABELS: tuple[Label, ...] = ("legitimate", "promotional", "spam", "phishing")


class ClassifierError(Exception):
    """Raised for failures that make classification impossible (e.g. authentication)."""


@dataclass(frozen=True)
class Verdict:
    label: Label
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    model: str = ""

    @property
    def spam_score(self) -> float:
        """Probability that the message is unwanted: spam or phishing."""
        return self.probabilities.get("spam", 0.0) + self.probabilities.get("phishing", 0.0)


class SpamClassifier(Protocol):
    """Classifies a message. Use as an async context manager to manage connections."""

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def classify(self, message: MessageSummary) -> Verdict: ...
