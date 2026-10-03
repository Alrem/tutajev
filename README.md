# tutajev

Spam classifier for [Tuta](https://tuta.com) mail.

* **Mailbox access:** the `tuta` package from
  [tutaproxy](https://github.com/peix2/tutaproxy-public) (AGPL-3.0), used as a library.
  No local IMAP server is started.
* **Classification:** [Jev](https://jev-ai.dev/), TypeSafe's calibrated decision model, called through `typesafe-sdk`.

## Milestone 1: read-only inspection

The tool logs in to Tuta and fetches all unread messages of the checked folders, or the newest
messages with `--all`. Reading does not change the unread state.
For each message it extracts the sender, Reply-To, subject, text body, URLs and attachment
metadata, and prints the result as JSON or text.

**Nothing in the mailbox is modified** unless `--move-spam --apply` is used (see milestone 3). The adapter calls tutaproxy's `TutaClient` through
`ReadOnlyTutaClient`, which only allows the read methods listed in
`src/tutajev/mailbox/_readonly.py`. Attachment contents are never downloaded, only their metadata.

## Milestone 2: classification (still read-only)

`--classify` sends each message to Jev and attaches a verdict to it: `legitimate`, `promotional`,
`spam` or `phishing`. Each verdict includes a confidence value, per-label probabilities and a `spam_score`
(P(spam) + P(phishing)). Verdicts are only printed. Nothing is moved or flagged in the mailbox.

The request to TypeSafe's API contains the sender, Reply-To, subject, the first 4000 characters of the body,
the links and attachment metadata. It does not contain attachment contents.

## Milestone 3: moving spam to the Spam folder

```sh
uv run tutajev --move-spam --format table          # dry run: marks candidates with "~>"
uv run tutajev --move-spam --apply --format table  # really moves them; marked with ">>"
uv run tutajev --move-spam --threshold 0.95        # stricter threshold (default 0.9)
```

A message is moved when its `spam_score` reaches the threshold. Moving works from any checked folder
except Spam and Trash.
Messages whose classification failed are never moved. Every decision is logged with the message id,
sender and subject.

Safety:
* `--move-spam` without `--apply` is a dry run. The adapter is then created without move permission.
* With `--apply`, the guard unlocks exactly one method, `simple_move_mails`, and the adapter always
  passes the Spam folder type. Deleting, other moves and flag changes stay blocked.
* Only messages listed in the same session can be moved.

## Setup

```sh
./scripts/fetch_tutaproxy.sh      # clones tutaproxy at a pinned commit into vendor/tutaproxy
uv sync
cp .env.example .env              # fill in TUTA_EMAIL / TUTA_PASSWORD (and TUTA_TOTP_SECRET for 2FA)
```

## Usage

```sh
uv run tutajev                          # all unread messages of the checked folders as JSON
uv run tutajev --all --limit 50         # the 50 newest messages regardless of read state
uv run tutajev --format text --limit 5  # at most 5 unread messages per folder
uv run tutajev --folder spam --log-level DEBUG
uv run tutajev --list-folders                       # system folders and decrypted custom folder names
uv run tutajev --folder inbox --folder Newsletters    # several folders; default: TUTAJEV_FOLDERS
uv run tutajev --format table           # one line per message: folder, verdict, spam score, sender, subject
uv run tutajev --classify               # JSON with a "classification" object per message
```

Logs go to stderr and results go to stdout, so you can run `uv run tutajev > inbox.json`.

## Architecture

```
cli.py ──> mailbox.port.MailboxReader (Protocol) ──> parsing.parse_message ──> output.render_*
                    ▲
                    └── mailbox.tuta_lib.TutaLibReader   (the only module that imports tutaproxy)

cli.py ──> classifier.port.SpamClassifier (Protocol)
                    ▲
                    └── classifier.jev.JevClassifier      (the only module that imports typesafe_sdk)
```

Adapters return `RawMessage` objects that hold RFC 822 bytes, plus optional attachment metadata.
To replace tutaproxy, write another class that implements `MailboxReader`, for example an IMAP
or `.eml` reader. Nothing else needs to change.

### tutaproxy notes

* Custom folder names are encrypted. Older entities, such as folders, may use the legacy
  unauthenticated key wrap and keys from before a group key rotation. The adapter handles both
  cases (`decrypt_key`, `_group_key`). tutaproxy itself does not handle them.

* Its internal API is undocumented, so it is pinned to a specific commit in `scripts/fetch_tutaproxy.sh`.
* `tuta.telemetry` contacts a third-party server. It is never imported, and `TUTAPROXY_TELEMETRY=false` is set as an extra safeguard.
* 2FA works with TOTP only. U2F and WebAuthn are not supported.

## Development

```sh
uv run pytest
uv run ruff check .
uv run mypy
```

Tests that use tutaproxy's real crypto are skipped when `vendor/tutaproxy` is missing.

## Disclaimer

Tuta has no public API. tutaproxy talks to Tuta's internal API, which was reverse-engineered from
the official client and can change without notice. This project is not affiliated with Tuta or
TypeSafe. With `--move-spam --apply` it moves messages in your mailbox; run it without `--apply` first.

## License

[AGPL-3.0](LICENSE). tutaproxy is licensed under AGPL-3.0. This project uses tutaproxy as a library,
and parts of `src/tutajev/mailbox/tuta_lib.py` are adapted from tutaproxy.
