# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```sh
./scripts/fetch_tutaproxy.sh   # required once: clones tutaproxy at a pinned SHA into vendor/ (gitignored)
uv sync
uv run pytest                                                     # all tests
uv run pytest tests/test_tuta_lib.py::test_unread_only_returns_all_unread_messages   # single test
uv run ruff check . && uv run ruff format src tests
uv run mypy                                                       # strict, src/ only
uv run tutajev --format table                  # list unread mail (read-only)
uv run tutajev --move-spam --format table      # dry run: candidates marked "~>"
uv run tutajev --move-spam --apply             # really moves spam (only mailbox mutation)
uv run tutajev --list-folders
```

`tests/test_tuta_lib.py` is skipped when `vendor/tutaproxy` is missing, so fetch it before trusting a green run. Line length is 100. Code, comments and docs are written in English.

## Architecture

The pipeline runs inside one Tuta session (`cli.run`): `MailboxReader.list_recent` → `parsing.parse_message` → `SpamClassifier.classify` → optionally `SpamMover.move_to_spam` → `output.render_*`. Moves must happen while the reader is still open, because the adapter only moves ids it listed in the same session.

**Ports and adapters.** `mailbox/port.py` and `classifier/port.py` define Protocols. Only `mailbox/tuta_lib.py` imports tutaproxy, only `classifier/jev.py` imports `typesafe_sdk`, and `classifier/clef.py` calls Cloudflare Workers AI over aiohttp. The question and message state live in `classifier/prompt.py` and are shared by both backends so their scores stay comparable. Adapters return `RawMessage` objects that hold RFC 822 bytes plus optional attachment metadata, so `parsing.py` does not depend on the backend. The CLI takes reader and classifier factories, and the tests inject fakes through them.

**tutaproxy used as a library, not a server.** `load_tuta()` puts `vendor/tutaproxy` on `sys.path` and imports only `tuta.api`, `tuta.crypto` and `tuta.message_builder`. Never import `tuta.telemetry`, which phones home; a test asserts this. Tuta entities are dicts keyed by numeric field ids. Look up ids in Tuta's type models, `tuta-sdk/rust/sdk/src/type_models/{tutanota,sys}.json` in tutao/tutanota, and keep them as named constants at the top of `tuta_lib.py`.

**Safety guard.** Every `TutaClient` call goes through `mailbox/_readonly.py::ReadOnlyTutaClient`, an allow-list. `simple_move_mails` is unlocked only when the reader is built with `allow_move_to_spam=True`, which the CLI does only for `--move-spam --apply`. Keep mutations out of the allow-list. New write paths should be narrow methods on the guard, like `close_session`, not generic helpers such as `_delete`.

**Workarounds for tutaproxy and the real Tuta API.** Keep these; each one was found against the live account.
- `TutaClient.logout` deletes the wrong URL and swallows the error, which leaves sessions open. The adapter closes sessions with `close_session` (`DELETE sys/session/<listId>/<elementId>`).
- Keys come in two formats. `decrypt_key()` handles both the legacy 16/32-byte key wrap (fixed IV, no MAC) and AesCbcThenHmac.
- Mail group keys are rotated. `_group_key(version)` walks the `formerGroupKeys` chain, where each former key is encrypted with the next newer one. Pass `_ownerKeyVersion` for mails (1395), files (1391) and folders.
- Real API responses wrap single ids in lists (`_unwrap`, `_last`).
- `build_rfc2822` omits Reply-To, which the adapter injects from MailDetails field 1285.
- `build_rfc2822` also adds a lossy text/plain part, because Tuta stores bodies as HTML only. That part leaks CSS, so `_drop_synthesized_plain_text` removes it.
- Attachment metadata comes from `get_file` (size is field 22) without downloading content.

**Selection and classification.**
- By default the tool checks all unread messages (Mail field 109) of every folder in `TUTAJEV_FOLDERS`. `--all` switches to the newest N messages instead.
- Custom folders are matched by their decrypted name.
- The classifier (`TUTAJEV_CLASSIFIER` / `--classifier`: jev, clef, clef-flash) answers a single `Choice` with the labels legitimate, promotional, spam and phishing. `Verdict.spam_score` is P(spam) + P(phishing).
- `TUTAJEV_VERDICT_CACHE` (`verdict_cache.py`) stores verdicts by classifier and message id, so each message is classified once; failures are not cached, `--no-cache` bypasses it. Jev scores vary between calls, so re-classifying unread mail every run would let legitimate mail drift over the threshold.
- A message is moved when `spam_score >= --threshold` (default 0.9). Messages whose classification failed are never moved by score.
- `--dmarc-rule` (`rules.dmarc_quarantined`) also moves messages whose Tuta-added `Authentication-Results` (authserv-id `*.tutanota.de`) say `dmarc=fail` with `dis=spam|quarantine|reject` and contain no `dkim=pass`. `build_rfc2822` drops the original headers, so the adapter prepends `Authentication-Results` from MailDetails field 1287 (Header 1271 plain / 1272 LZ4-compressed).

Mailbox mutations are run as a dry run first and applied only after the user confirms.
