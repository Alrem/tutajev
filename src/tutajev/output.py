"""Render message summaries (and optional classification verdicts) as JSON or text."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from tutajev.classifier import Verdict
from tutajev.models import Address, MessageSummary

_TEXT_PREVIEW_CHARS = 500
_ACTION_MARKERS = {"moved_to_spam": ">>", "would_move_to_spam": "~>"}


@dataclasses.dataclass(frozen=True)
class Result:
    message: MessageSummary
    verdict: Verdict | None = None
    error: str | None = None
    # "moved_to_spam", "would_move_to_spam" (dry run) or None
    action: str | None = None
    # Why the message is moved: "score" (classifier) or "dmarc" (rules.dmarc_quarantined).
    reason: str | None = None


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def _verdict_dict(result: Result) -> dict[str, Any] | None:
    if result.verdict is None:
        return {"error": result.error} if result.error else None
    v = result.verdict
    return {
        "label": v.label,
        "confidence": v.confidence,
        "spam_score": v.spam_score,
        "probabilities": v.probabilities,
        "model": v.model,
    }


def render_json(results: Sequence[Result], include_classification: bool = False) -> str:
    items = []
    for r in results:
        item = dataclasses.asdict(r.message)
        if include_classification:
            item["classification"] = _verdict_dict(r)
            item["action"] = r.action
            item["reason"] = r.reason
        items.append(item)
    return json.dumps(items, default=_json_default, ensure_ascii=False, indent=2)


def _fmt_address(address: Address | None) -> str:
    if address is None:
        return "-"
    return f"{address.name} <{address.email}>" if address.name else address.email


def _fmt_verdict(result: Result) -> str:
    if result.verdict is None:
        return f"failed ({result.error})" if result.error else "-"
    v = result.verdict
    return f"{v.label.upper()} (confidence {v.confidence:.2f}, spam score {v.spam_score:.2f})"


def render_text(results: Sequence[Result], include_classification: bool = False) -> str:
    blocks = []
    for index, r in enumerate(results, start=1):
        m = r.message
        received = m.received_at.isoformat() if m.received_at else "-"
        body = m.text_body
        if len(body) > _TEXT_PREVIEW_CHARS:
            body = body[:_TEXT_PREVIEW_CHARS] + " [...]"
        lines = [
            f"=== [{index}] {m.subject or '(no subject)'}",
            f"id:          {m.id}",
            f"folder:      {m.folder or '-'}",
            f"received:    {received}",
            f"from:        {_fmt_address(m.sender)}",
            f"reply-to:    {', '.join(_fmt_address(a) for a in m.reply_to) or '-'}"
            + ("  (differs from sender)" if m.reply_to_differs else ""),
        ]
        if include_classification:
            lines.append(f"verdict:     {_fmt_verdict(r)}")
            if r.action:
                lines.append(f"action:      {r.action} ({r.reason})")
        lines += [
            f"has html:    {'yes' if m.has_html else 'no'}",
            f"urls ({len(m.urls)}):",
            *(f"  - {u}" for u in m.urls),
            f"attachments ({len(m.attachments)}):",
            *(
                f"  - {a.filename} ({a.content_type}, {a.size_bytes} B, {a.disposition})"
                for a in m.attachments
            ),
            "body:",
            *(f"  | {line}" for line in body.splitlines()),
        ]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_summary_table(results: Sequence[Result]) -> str:
    """One line per message: action marker, folder, verdict, spam score, sender and subject.

    Marker: ``>>`` moved to Spam, ``~>`` would be moved (dry run). Messages moved by the
    DMARC rule rather than by their score end with ``[dmarc]``.
    """
    lines = []
    for r in results:
        m = r.message
        verdict = r.verdict.label if r.verdict else "error"
        score = f"{r.verdict.spam_score:.2f}" if r.verdict else "  - "
        sender = m.sender.email if m.sender else "-"
        marker = _ACTION_MARKERS.get(r.action or "", "  ")
        folder = m.folder[:12]
        line = f"{marker} {folder:<12} {verdict:<12} {score}  {sender[:32]:<32}  {m.subject[:60]}"
        lines.append(line + ("  [dmarc]" if r.reason == "dmarc" else ""))
    return "\n".join(lines)
