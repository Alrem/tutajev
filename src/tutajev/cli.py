"""Command-line entry point: list, classify and optionally move spam out of a Tuta folder.

By default the mailbox is only read. Messages are moved to Spam only with
``--move-spam --apply``; ``--move-spam`` alone is a dry run.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from collections.abc import Callable, Sequence

from tutajev.classifier import ClassifierError, SpamClassifier
from tutajev.config import ConfigError, Settings, load_settings
from tutajev.logging_setup import configure_logging
from tutajev.mailbox import MailboxError, MailboxReader, SpamMover
from tutajev.models import MessageSummary, RawMessage
from tutajev.output import Result, render_json, render_summary_table, render_text
from tutajev.parsing import parse_message

logger = logging.getLogger(__name__)

# (settings, allow_move_to_spam) -> reader; move permission is granted only for --apply.
ReaderFactory = Callable[[Settings, bool], MailboxReader]
ClassifierFactory = Callable[[Settings], SpamClassifier]

DEFAULT_THRESHOLD = 0.9
DEFAULT_ALL_LIMIT = 20
# Moving "to Spam" from these folders makes no sense.
_NO_MOVE_FOLDERS = frozenset({"spam", "trash"})
# Concurrent classification requests.
_CLASSIFY_CONCURRENCY = 4


def default_reader_factory(settings: Settings, allow_move_to_spam: bool) -> MailboxReader:
    # Imported lazily so the rest of the app never depends on tutaproxy at import time.
    from tutajev.mailbox.tuta_lib import TutaLibReader

    return TutaLibReader(
        email_address=settings.tuta_email,
        password=settings.tuta_password,
        tutaproxy_path=settings.tutaproxy_path,
        totp_secret=settings.tuta_totp_secret,
        allow_move_to_spam=allow_move_to_spam,
    )


def default_classifier_factory(settings: Settings) -> SpamClassifier:
    from tutajev.classifier.jev import JevClassifier

    if not settings.typesafe_api_key:
        raise ConfigError("TYPESAFE_API_KEY is required for classification")
    return JevClassifier(api_key=settings.typesafe_api_key)


def parse_all(raw_messages: Sequence[RawMessage]) -> list[MessageSummary]:
    summaries = []
    for raw in raw_messages:
        try:
            summaries.append(parse_message(raw))
        except Exception:
            logger.exception("Failed to parse message %s", raw.id)
    return summaries


async def classify_all(
    classifier: SpamClassifier, summaries: Sequence[MessageSummary]
) -> list[Result]:
    semaphore = asyncio.Semaphore(_CLASSIFY_CONCURRENCY)

    async def one(message: MessageSummary) -> Result:
        async with semaphore:
            try:
                return Result(message, verdict=await classifier.classify(message))
            except ClassifierError:
                raise
            except Exception as exc:
                logger.warning("Classification failed for %s: %s", message.id, exc)
                return Result(message, error=str(exc))

    async with classifier:
        return list(await asyncio.gather(*(one(m) for m in summaries)))


async def move_spam(
    reader: MailboxReader, results: Sequence[Result], threshold: float, apply: bool
) -> list[Result]:
    """Move results whose spam score reaches ``threshold`` to Spam; only report unless ``apply``.

    Messages whose classification failed are never moved.
    """
    candidates = {
        r.message.id for r in results if r.verdict is not None and r.verdict.spam_score >= threshold
    }
    for r in results:
        if r.message.id in candidates:
            assert r.verdict is not None
            logger.info(
                "%s %s: %s %.2f, from %s, subject %r",
                "Moving" if apply else "Would move",
                r.message.id,
                r.verdict.label,
                r.verdict.spam_score,
                r.message.sender.email if r.message.sender else "-",
                r.message.subject,
            )
    if not candidates:
        logger.info("No messages reach spam score %.2f", threshold)
        return list(results)

    if apply:
        if not isinstance(reader, SpamMover):
            raise MailboxError("This mailbox adapter cannot move messages")
        await reader.move_to_spam([r.message.id for r in results if r.message.id in candidates])
        logger.info("Moved %d message(s) to Spam", len(candidates))
    else:
        logger.info("Dry run: %d message(s) would be moved to Spam; add --apply", len(candidates))

    action = "moved_to_spam" if apply else "would_move_to_spam"
    return [
        dataclasses.replace(r, action=action) if r.message.id in candidates else r for r in results
    ]


async def run(
    args: argparse.Namespace,
    settings: Settings,
    reader_factory: ReaderFactory,
    classifier_factory: ClassifierFactory,
) -> list[Result]:
    # Build the classifier first so a missing API key fails before logging in to Tuta.
    classifier = classifier_factory(settings) if args.classify else None
    apply = bool(args.move_spam and args.apply)
    reader = reader_factory(settings, apply)
    # Classification and moving happen inside one Tuta session: moves refer to listed messages.
    async with reader:
        summaries = []
        for folder in args.folder:
            raw_messages = await reader.list_recent(
                folder=folder, limit=args.limit, unread_only=not args.all
            )
            summaries += parse_all(raw_messages)
        if classifier is None:
            return [Result(m) for m in summaries]
        results = await classify_all(classifier, summaries)
        if args.move_spam:
            results = await move_spam(reader, results, args.threshold, apply)
    return results


def _threshold(value: str) -> float:
    number = float(value)
    if not 0.0 < number <= 1.0:
        raise argparse.ArgumentTypeError("threshold must be in (0, 1]")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tutajev", description="List, classify and move spam out of a Tuta folder."
    )
    parser.add_argument(
        "--folder",
        action="append",
        help="System folder (inbox, spam, trash, archive, sent) or custom folder name; "
        "repeatable. Default: TUTAJEV_FOLDERS from the environment, else inbox",
    )
    parser.add_argument("--list-folders", action="store_true", help="Print folder names and exit")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Check the newest messages regardless of read state instead of all unread ones",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help=f"Max messages per folder, newest first; default: no limit for unread, "
        f"{DEFAULT_ALL_LIMIT} with --all",
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help="Classify messages with Jev (sends content to TypeSafe)",
    )
    parser.add_argument(
        "--move-spam",
        action="store_true",
        help="Move messages whose spam score reaches --threshold to Spam "
        "(dry run without --apply); implies --classify",
    )
    parser.add_argument(
        "--threshold",
        type=_threshold,
        default=DEFAULT_THRESHOLD,
        help="Spam score (P(spam) + P(phishing)) needed to move a message; "
        f"default {DEFAULT_THRESHOLD}",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually move messages (only with --move-spam)"
    )
    parser.add_argument(
        "--format",
        choices=("json", "text", "table"),
        default="json",
        help="'table' prints one line per message and implies --classify",
    )
    parser.add_argument("--log-level", help="Overrides LOG_LEVEL")
    parser.add_argument("--env-file", default=".env")
    return parser


def _print_folders(reader: MailboxReader) -> int:
    async def fetch() -> list[str]:
        async with reader:
            return await reader.list_folders()

    try:
        folders = asyncio.run(fetch())
    except MailboxError as exc:
        logger.error("%s", exc)
        return 1
    sys.stdout.write("\n".join(folders) + "\n")
    return 0


def main(
    argv: Sequence[str] | None = None,
    reader_factory: ReaderFactory = default_reader_factory,
    classifier_factory: ClassifierFactory = default_classifier_factory,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.apply and not args.move_spam:
        parser.error("--apply requires --move-spam")
    if args.format == "table" or args.move_spam:
        args.classify = True

    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        configure_logging("INFO")
        logger.error("%s", exc)
        return 2
    configure_logging((args.log_level or settings.log_level).upper())
    args.folder = list(dict.fromkeys(args.folder or settings.folders))
    if args.limit is None and args.all:
        args.limit = DEFAULT_ALL_LIMIT
    if args.move_spam and _NO_MOVE_FOLDERS & {f.lower() for f in args.folder}:
        parser.error("--move-spam cannot be used with the spam or trash folder")

    if args.list_folders:
        return _print_folders(reader_factory(settings, False))

    try:
        results = asyncio.run(run(args, settings, reader_factory, classifier_factory))
    except ConfigError as exc:
        logger.error("%s", exc)
        return 2
    except (MailboxError, ClassifierError) as exc:
        logger.error("%s", exc)
        return 1

    if args.format == "table":
        output = render_summary_table(results)
    elif args.format == "text":
        output = render_text(results, include_classification=args.classify)
    else:
        output = render_json(results, include_classification=args.classify)
    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
