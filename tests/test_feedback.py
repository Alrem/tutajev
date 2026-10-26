from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import load_eml
from tests.test_cli import FakeClassifier, FakeReader
from tutajev.cli import main

# FakeClassifier scores html_only.eml (Reply-To differs) as spam 0.9 and plain.eml as legitimate.
SPAM, HAM = "html_only.eml", "plain.eml"


@pytest.fixture
def cache_dir(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    clean_env.setenv("TUTA_EMAIL", "me@tuta.com")
    clean_env.setenv("TUTA_PASSWORD", "secret")
    clean_env.setenv("TUTAJEV_VERDICT_CACHE", str(tmp_path / "verdicts.json"))
    return tmp_path


def _run(reader: FakeReader, *argv: str) -> int:
    return main(
        ["--move-spam", *argv, "--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: reader,
        classifier_factory=lambda s: FakeClassifier(),
    )


def _mistakes(cache_dir: Path) -> list[dict[str, object]]:
    path = cache_dir / "mistakes.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _first_run(cache_dir: Path) -> None:
    reader = FakeReader([load_eml(SPAM), load_eml(HAM)])
    assert _run(reader, "--apply") == 0
    assert reader.moved == [SPAM]
    assert _mistakes(cache_dir) == []


def test_both_kinds_of_mistakes_are_found_once(cache_dir: Path) -> None:
    _first_run(cache_dir)

    # The user moves the kept message to Spam and rescues the moved one, leaving it unread.
    reader = FakeReader([load_eml(SPAM)], folders={"spam": {HAM}, "inbox": {SPAM}})
    assert _run(reader, "--apply") == 0

    mistakes = {m["kind"]: m for m in _mistakes(cache_dir)}
    assert set(mistakes) == {"missed", "rescued"}
    assert mistakes["missed"]["message_id"] == HAM
    assert mistakes["missed"]["label"] == "legitimate"
    assert mistakes["missed"]["spam_score"] == 0.0
    assert mistakes["rescued"]["message_id"] == SPAM
    assert mistakes["rescued"]["folder"] == "inbox"
    assert mistakes["rescued"]["reason"] == "score"
    assert mistakes["rescued"]["spam_score"] == 0.9
    # A rescued message is not moved again, even though its cached score is above the threshold.
    assert reader.moved == []

    reader = FakeReader([load_eml(SPAM)], folders={"spam": {HAM}, "inbox": {SPAM}})
    assert _run(reader, "--apply") == 0
    assert len(_mistakes(cache_dir)) == 2
    assert reader.moved == []


def test_message_staying_where_it_was_put_is_no_mistake(cache_dir: Path) -> None:
    _first_run(cache_dir)

    reader = FakeReader([], folders={"spam": {SPAM}, "inbox": {HAM}})
    assert _run(reader, "--apply") == 0
    assert _mistakes(cache_dir) == []


def test_dry_run_records_no_decisions(cache_dir: Path) -> None:
    reader = FakeReader([load_eml(SPAM), load_eml(HAM)])
    assert _run(reader) == 0

    reader = FakeReader([], folders={"spam": {HAM}, "inbox": {SPAM}})
    assert _run(reader, "--apply") == 0
    assert _mistakes(cache_dir) == []
