"""Find tutajev's mistakes by comparing its decisions with where the messages are now.

Message ids stay the same when mail moves between folders, so no content is needed:

- ``missed``: tutajev kept a message, and it is now in Spam (moved there by the user or Tuta).
- ``rescued``: tutajev moved a message to Spam, and it is now back in a watched folder.

Each mistake is reported once and appended to a JSON Lines file next to the verdict cache. The
decisions only live as long as the cache entries (30 days), which matches Tuta's Spam retention.
Mistakes are only collected for now; nothing here changes what gets moved.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from tutajev.mailbox import MailLocator
from tutajev.output import Result
from tutajev.verdict_cache import Outcome, VerdictCache

logger = logging.getLogger(__name__)

MISTAKES_FILE = "mistakes.jsonl"
# Mail moved out of these folders is not a signal: Spam is what we compare against, and a
# message in Trash may have been either.
_IGNORED_FOLDERS = frozenset({"spam", "trash"})


@dataclasses.dataclass(frozen=True)
class Mistake:
    kind: Outcome
    message_id: str
    folder: str
    sender: str
    subject: str
    label: str | None
    spam_score: float | None
    reason: str | None
    decided_at: str
    found_at: str


def record_decisions(cache: VerdictCache, results: Sequence[Result]) -> None:
    """Store what was done with each result; failed classifications that stayed are skipped."""
    for r in results:
        moved = r.action == "moved_to_spam"
        if r.verdict is None and not moved:
            continue
        cache.record_decision(
            r.message.id,
            "moved" if moved else "kept",
            r.reason,
            r.message.sender.email if r.message.sender else "",
            r.message.subject,
        )


async def find_mistakes(
    locator: MailLocator, cache: VerdictCache, folders: Iterable[str]
) -> list[Mistake]:
    """Compare earlier decisions in ``cache`` with the current contents of Spam and ``folders``.

    Call it before this run moves anything or records its own decisions. Marks each mistake's
    cache entry with its outcome so it is reported only once.
    """
    spam_ids = await locator.list_ids("spam")
    folder_of: dict[str, str] = {}
    for folder in folders:
        if folder.lower() not in _IGNORED_FOLDERS:
            folder_of.update(dict.fromkeys(await locator.list_ids(folder), folder))

    found_at = datetime.now(UTC).isoformat()
    mistakes = []
    for message_id, entry in cache.decided():
        if entry.get("outcome"):
            continue
        kind: Outcome
        if entry["decision"] == "kept" and message_id in spam_ids:
            kind, folder = "missed", "spam"
        elif entry["decision"] == "moved" and message_id in folder_of:
            kind, folder = "rescued", folder_of[message_id]
        else:
            continue
        cache.set_outcome(message_id, kind)
        probabilities = entry.get("probabilities")
        mistakes.append(
            Mistake(
                kind=kind,
                message_id=message_id,
                folder=folder,
                sender=entry.get("sender", ""),
                subject=entry.get("subject", ""),
                label=entry.get("label"),
                spam_score=(
                    round(probabilities.get("spam", 0.0) + probabilities.get("phishing", 0.0), 4)
                    if isinstance(probabilities, dict)
                    else None
                ),
                reason=entry.get("reason"),
                decided_at=entry.get("decided_at", ""),
                found_at=found_at,
            )
        )
    return mistakes


def log_mistakes(mistakes: Sequence[Mistake], path: Path) -> None:
    """Log each mistake and append it to ``path``; write errors are logged, not raised."""
    for m in mistakes:
        logger.info(
            "[%s] %s: was %s %s (%s), now in %s, from %s, subject %r",
            m.kind,
            m.message_id,
            m.label or "unclassified",
            f"{m.spam_score:.2f}" if m.spam_score is not None else "-",
            "moved" if m.kind == "rescued" else "kept",
            m.folder,
            m.sender,
            m.subject,
        )
    if not mistakes:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            for m in mistakes:
                f.write(json.dumps(dataclasses.asdict(m), ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Could not write mistakes to %s: %s", path, exc)
