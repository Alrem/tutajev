"""A JSON file of verdicts keyed by classifier and message id, so each message is classified once.

Jev's scores vary between calls, and unread mail is checked again every run. Without the cache a
legitimate message that stays unread gets a new chance to cross the threshold each time.
Failed classifications are not stored. Entries not seen for ``max_age`` are dropped on save.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tutajev.classifier.port import LABELS, Verdict

logger = logging.getLogger(__name__)

_VERSION = 1
DEFAULT_MAX_AGE = timedelta(days=30)


class VerdictCache:
    def __init__(
        self,
        path: Path,
        classifier: str,
        entries: dict[str, dict[str, Any]] | None = None,
        max_age: timedelta = DEFAULT_MAX_AGE,
    ) -> None:
        self.path = path
        self.classifier = classifier
        self.max_age = max_age
        self._entries = entries or {}
        self._now = datetime.now(UTC)

    @classmethod
    def load(
        cls, path: Path, classifier: str, max_age: timedelta = DEFAULT_MAX_AGE
    ) -> VerdictCache:
        """Read ``path``; a missing or unreadable file gives an empty cache."""
        entries: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == _VERSION and isinstance(data.get("verdicts"), dict):
                entries = data["verdicts"]
            else:
                logger.warning("Ignoring verdict cache %s with unknown format", path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError) as exc:
            logger.warning("Ignoring unreadable verdict cache %s: %s", path, exc)
        return cls(path, classifier, entries, max_age)

    def _key(self, message_id: str) -> str:
        return f"{self.classifier}:{message_id}"

    def get(self, message_id: str) -> Verdict | None:
        entry = self._entries.get(self._key(message_id))
        if entry is None:
            return None
        try:
            label = entry["label"]
            if label not in LABELS:
                raise ValueError(f"unknown label {label!r}")
            verdict = Verdict(
                label=label,
                confidence=float(entry["confidence"]),
                probabilities={k: float(v) for k, v in entry["probabilities"].items()},
                model=str(entry.get("model", "")),
            )
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            logger.warning("Ignoring bad cached verdict for %s: %s", message_id, exc)
            return None
        entry["last_seen"] = self._now.isoformat()
        return verdict

    def put(self, message_id: str, verdict: Verdict) -> None:
        now = self._now.isoformat()
        self._entries[self._key(message_id)] = {
            "label": verdict.label,
            "confidence": verdict.confidence,
            "probabilities": verdict.probabilities,
            "model": verdict.model,
            "classified_at": now,
            "last_seen": now,
        }

    def save(self) -> None:
        """Drop stale entries and write the file atomically; errors are logged, not raised."""
        cutoff = self._now - self.max_age
        self._entries = {k: v for k, v in self._entries.items() if not _older(v, cutoff)}
        payload = json.dumps({"version": _VERSION, "verdicts": self._entries}, indent=1)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as exc:
            logger.warning("Could not write verdict cache %s: %s", self.path, exc)


def _older(entry: dict[str, Any], cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(entry["last_seen"]) < cutoff
    except (KeyError, TypeError, ValueError):
        return True
