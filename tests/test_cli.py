from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from tests.conftest import load_eml
from tutajev.classifier import ClassifierError, Verdict
from tutajev.cli import default_classifier_factory, main
from tutajev.config import Settings, load_settings
from tutajev.mailbox import MailboxError
from tutajev.models import MessageSummary, RawMessage


class FakeReader:
    def __init__(
        self,
        messages: list[RawMessage],
        fail: bool = False,
        folders: dict[str, set[str]] | None = None,
    ) -> None:
        self.messages = messages
        self.fail = fail
        # Ids per folder for list_ids; by default the listed messages are in the Inbox.
        self.folders = folders if folders is not None else {"inbox": {m.id for m in messages}}
        self.calls: list[tuple[str, int | None, bool]] = []
        self.closed = False
        self.moved: list[str] = []

    async def __aenter__(self) -> Self:
        if self.fail:
            raise MailboxError("login failed")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.closed = True

    async def list_folders(self) -> list[str]:
        return ["inbox", "spam", "Newsletters"]

    async def list_recent(
        self, folder: str = "inbox", limit: int | None = 20, unread_only: bool = False
    ) -> list[RawMessage]:
        self.calls.append((folder, limit, unread_only))
        return [dataclasses.replace(m, folder=folder) for m in self.messages[:limit]]

    async def list_ids(self, folder: str) -> set[str]:
        return set(self.folders.get(folder, set()))

    async def move_to_spam(self, message_ids: Sequence[str]) -> None:
        assert not self.closed, "moves must happen inside the session"
        self.moved.extend(message_ids)
        for ids in self.folders.values():
            ids.difference_update(message_ids)
        self.folders.setdefault("spam", set()).update(message_ids)


@pytest.fixture
def creds(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TUTA_EMAIL", "me@tuta.com")
    clean_env.setenv("TUTA_PASSWORD", "secret")


def test_json_output(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    reader = FakeReader([load_eml("html_only.eml"), load_eml("plain.eml")])

    def factory(settings: Settings, allow_move: bool) -> FakeReader:
        return reader

    code = main(["--limit", "2", "--env-file", "/nonexistent"], reader_factory=factory)

    assert code == 0
    assert reader.calls == [("inbox", 2, True)]
    assert reader.closed
    data = json.loads(capsys.readouterr().out)
    assert [m["subject"] for m in data] == ["You won €1000!", "Plain hello"]
    assert data[0]["reply_to"] == [{"name": "Claims Desk", "email": "claims@other.example"}]
    assert data[0]["reply_to_differs"] is True


def test_text_output(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    reader = FakeReader([load_eml("multipart_attachment.eml")])
    code = main(
        ["--format", "text", "--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: reader,
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "=== [1] Your invoice" in out
    assert "invoice.pdf (application/pdf" in out


def test_mailbox_error_returns_1(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: FakeReader([], fail=True),
    )
    assert code == 1
    assert capsys.readouterr().out == ""


def test_missing_config_returns_2(clean_env: pytest.MonkeyPatch) -> None:
    assert (
        main(["--env-file", "/nonexistent"], reader_factory=lambda s, allow_move: FakeReader([]))
        == 2
    )


class FakeClassifier:
    def __init__(self, fail_on: str | None = None, fatal: bool = False) -> None:
        self.fail_on = fail_on
        self.fatal = fatal
        self.classified: list[str] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    async def classify(self, message: MessageSummary) -> Verdict:
        self.classified.append(message.id)
        if self.fatal:
            raise ClassifierError("bad key")
        if message.subject == self.fail_on:
            raise RuntimeError("timeout")
        label = "spam" if message.reply_to_differs else "legitimate"
        probabilities = {"spam": 0.9, "legitimate": 0.1} if label == "spam" else {"legitimate": 1.0}
        return Verdict(label, 0.9, probabilities, "jev-1")


def _two_messages() -> FakeReader:
    return FakeReader([load_eml("html_only.eml"), load_eml("plain.eml")])


def test_classify_json(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--classify", "--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: _two_messages(),
        classifier_factory=lambda s: FakeClassifier(fail_on="Plain hello"),
    )
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert data[0]["classification"]["label"] == "spam"
    assert data[0]["classification"]["spam_score"] == 0.9
    assert data[1]["classification"] == {"error": "timeout"}


def test_table_implies_classify(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--format", "table", "--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: _two_messages(),
        classifier_factory=lambda s: FakeClassifier(),
    )
    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[0].split()[:3] == ["inbox", "spam", "0.90"] and "You won" in lines[0]
    assert lines[1].split()[:2] == ["inbox", "legitimate"]


def test_fatal_classifier_error_returns_1(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--classify", "--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: _two_messages(),
        classifier_factory=lambda s: FakeClassifier(fatal=True),
    )
    assert code == 1
    assert capsys.readouterr().out == ""


def test_classify_without_api_key_fails_before_login(creds: None) -> None:
    reader = _two_messages()
    code = main(
        ["--classify", "--env-file", "/nonexistent"], reader_factory=lambda s, allow_move: reader
    )
    assert code == 2
    assert reader.calls == []


def _move_run(argv: list[str]) -> tuple[int, FakeReader, list[bool]]:
    reader = _two_messages()
    permissions: list[bool] = []

    def factory(settings: Settings, allow_move: bool) -> FakeReader:
        permissions.append(allow_move)
        return reader

    code = main(
        [*argv, "--env-file", "/nonexistent"],
        reader_factory=factory,
        classifier_factory=lambda s: FakeClassifier(),
    )
    return code, reader, permissions


def test_move_spam_dry_run_moves_nothing(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    code, reader, permissions = _move_run(["--move-spam", "--format", "table"])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert reader.moved == []
    assert permissions == [False]
    assert lines[0].split()[:3] == ["~>", "inbox", "spam"]
    assert lines[1].split()[:2] == ["inbox", "legitimate"]


def test_move_spam_apply_moves_only_above_threshold(
    creds: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code, reader, permissions = _move_run(["--move-spam", "--apply"])

    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert permissions == [True]
    assert reader.moved == ["html_only.eml"]
    assert [m["action"] for m in data] == ["moved_to_spam", None]


def test_threshold_above_score_moves_nothing(creds: None) -> None:
    code, reader, _ = _move_run(["--move-spam", "--apply", "--threshold", "0.95"])
    assert code == 0
    assert reader.moved == []


def test_failed_classification_is_never_moved(creds: None) -> None:
    reader = FakeReader([load_eml("html_only.eml")])
    code = main(
        ["--move-spam", "--apply", "--threshold", "0.01", "--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: reader,
        classifier_factory=lambda s: FakeClassifier(fail_on="You won €1000!"),
    )
    assert code == 0
    assert reader.moved == []


def _cached_run(argv: list[str], classifier: FakeClassifier) -> int:
    return main(
        ["--format", "table", *argv, "--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: _two_messages(),
        classifier_factory=lambda s: classifier,
    )


def test_verdict_cache_classifies_each_message_once(
    creds: None, clean_env: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean_env.setenv("TUTAJEV_VERDICT_CACHE", str(tmp_path / "verdicts.json"))
    first, second = FakeClassifier(fail_on="Plain hello"), FakeClassifier()

    assert _cached_run([], first) == 0
    first_out = capsys.readouterr().out
    assert _cached_run([], second) == 0

    assert first.classified == ["html_only.eml", "plain.eml"]
    # The failed classification is retried; the verdict for the first message is reused.
    assert second.classified == ["plain.eml"]
    assert capsys.readouterr().out.splitlines()[0] == first_out.splitlines()[0]


def test_no_cache_flag_ignores_cache(
    creds: None, clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "verdicts.json"
    clean_env.setenv("TUTAJEV_VERDICT_CACHE", str(path))
    assert _cached_run(["--no-cache"], FakeClassifier()) == 0
    assert not path.exists()

    assert _cached_run([], FakeClassifier()) == 0
    again = FakeClassifier()
    assert _cached_run(["--no-cache"], again) == 0
    assert again.classified == ["html_only.eml", "plain.eml"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--apply"],
        ["--move-spam", "--folder", "inbox", "--folder", "Spam"],
        ["--move-spam", "--threshold", "1.5"],
    ],
)
def test_invalid_move_arguments(creds: None, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv, reader_factory=lambda s, allow_move: _two_messages())
    assert excinfo.value.code == 2


def test_multiple_folders_including_custom(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    reader = FakeReader([load_eml("plain.eml")])
    code = main(
        ["--folder", "inbox", "--folder", "Newsletters", "--env-file", "/nonexistent"],
        reader_factory=lambda s, allow_move: reader,
    )
    data = json.loads(capsys.readouterr().out)
    assert code == 0
    assert reader.calls == [("inbox", None, True), ("Newsletters", None, True)]
    assert [m["folder"] for m in data] == ["inbox", "Newsletters"]


def test_folders_default_from_environment(creds: None) -> None:
    creds_env = pytest.MonkeyPatch()
    creds_env.setenv("TUTAJEV_FOLDERS", "inbox, Newsletters")
    try:
        reader = FakeReader([])
        assert main(["--env-file", "/nonexistent"], reader_factory=lambda s, a: reader) == 0
        assert reader.calls == [("inbox", None, True), ("Newsletters", None, True)]
    finally:
        creds_env.undo()


def test_list_folders(creds: None, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["--list-folders", "--env-file", "/nonexistent"], reader_factory=lambda s, a: FakeReader([])
    )
    assert code == 0
    assert capsys.readouterr().out.split() == ["inbox", "spam", "Newsletters"]


def test_all_mode_uses_newest_with_default_limit(creds: None) -> None:
    reader = FakeReader([])
    assert main(["--all", "--env-file", "/nonexistent"], reader_factory=lambda s, a: reader) == 0
    assert reader.calls == [("inbox", 20, False)]


def test_limit_caps_unread_mode(creds: None) -> None:
    reader = FakeReader([])
    code = main(["--limit", "5", "--env-file", "/nonexistent"], reader_factory=lambda s, a: reader)
    assert code == 0
    assert reader.calls == [("inbox", 5, True)]


def test_classifier_flag_overrides_settings(creds: None, clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TUTAJEV_CLASSIFIER", "jev")
    seen: list[str] = []

    def classifier_factory(settings: Settings) -> FakeClassifier:
        seen.append(settings.classifier)
        return FakeClassifier()

    assert (
        main(
            ["--classify", "--classifier", "clef-flash", "--env-file", "/nonexistent"],
            reader_factory=lambda s, a: FakeReader([]),
            classifier_factory=classifier_factory,
        )
        == 0
    )
    assert seen == ["clef-flash"]


def test_clef_requires_cloudflare_credentials(
    creds: None, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["--classify", "--classifier", "clef", "--env-file", "/nonexistent"],
        reader_factory=lambda s, a: FakeReader([]),
    )
    assert code == 2
    assert "CLOUDFLARE_ACCOUNT_ID" in capsys.readouterr().err


def test_default_factory_builds_clef(creds: None, clean_env: pytest.MonkeyPatch) -> None:
    from tutajev.classifier.clef import ClefClassifier

    clean_env.setenv("TUTAJEV_CLASSIFIER", "clef-flash")
    clean_env.setenv("CLOUDFLARE_ACCOUNT_ID", "acc")
    clean_env.setenv("CLOUDFLARE_API_TOKEN", "tok")
    classifier = default_classifier_factory(load_settings(None))
    assert isinstance(classifier, ClefClassifier)
    assert classifier._url.endswith("/accounts/acc/ai/run/@cf/cloudflare/clef-flash")


def _dmarc_failing_message() -> RawMessage:
    raw = load_eml("plain.eml")
    header = b"Authentication-Results: w10.tutanota.de; dmarc=fail (dis=spam p=quarantine)\r\n"
    return dataclasses.replace(raw, raw=header + raw.raw)


@pytest.mark.parametrize(("dmarc_rule", "moved"), [(False, []), (True, ["plain.eml"])])
def test_dmarc_rule_moves_unsigned_dmarc_failures(
    creds: None, capsys: pytest.CaptureFixture[str], dmarc_rule: bool, moved: list[str]
) -> None:
    reader = FakeReader([_dmarc_failing_message()])
    argv = ["--move-spam", "--apply", "--format", "table", "--env-file", "/nonexistent"]
    code = main(
        argv + (["--dmarc-rule"] if dmarc_rule else []),
        reader_factory=lambda s, a: reader,
        classifier_factory=lambda s: FakeClassifier(),
    )

    out = capsys.readouterr().out
    assert code == 0
    assert reader.moved == moved
    assert out.rstrip().endswith("[dmarc]") is dmarc_rule
    assert out.split()[0] == (">>" if dmarc_rule else "inbox")


def test_dmarc_rule_requires_move_spam(creds: None) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--dmarc-rule", "--env-file", "/nonexistent"])
    assert exc.value.code == 2
