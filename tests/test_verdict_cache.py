from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tutajev.classifier import Verdict
from tutajev.verdict_cache import VerdictCache

SPAM = Verdict("spam", 0.8, {"spam": 0.8, "legitimate": 0.2}, "jev-1")


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state" / "verdicts.json"
    cache = VerdictCache.load(path, "jev")
    assert cache.get("m1") is None
    cache.put("m1", SPAM)
    cache.save()

    assert VerdictCache.load(path, "jev").get("m1") == SPAM


def test_keyed_by_classifier(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.json"
    cache = VerdictCache.load(path, "jev")
    cache.put("m1", SPAM)
    cache.save()

    assert VerdictCache.load(path, "clef").get("m1") is None


def test_unreadable_file_gives_empty_cache(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.json"
    path.write_text("not json")

    cache = VerdictCache.load(path, "jev")
    assert cache.get("m1") is None
    cache.put("m1", SPAM)
    cache.save()
    assert VerdictCache.load(path, "jev").get("m1") == SPAM


def test_bad_entry_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.json"
    path.write_text(json.dumps({"version": 1, "verdicts": {"jev:m1": {"label": "weird"}}}))

    assert VerdictCache.load(path, "jev").get("m1") is None


def test_save_drops_entries_not_seen_recently(tmp_path: Path) -> None:
    path = tmp_path / "verdicts.json"
    old = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    stale = {"label": "spam", "confidence": 1, "probabilities": {}, "last_seen": old}
    path.write_text(json.dumps({"version": 1, "verdicts": {"jev:old": stale, "jev:hit": stale}}))

    cache = VerdictCache.load(path, "jev")
    assert cache.get("hit") is not None  # a hit refreshes last_seen
    cache.save()

    assert set(json.loads(path.read_text())["verdicts"]) == {"jev:hit"}


def test_unwritable_path_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    cache = VerdictCache.load(blocker / "verdicts.json", "jev")
    cache.put("m1", SPAM)
    cache.save()
