"""Mailbox access. Everything outside this package depends only on :class:`MailboxReader`."""

from tutajev.mailbox.port import MailboxError, MailboxReader, SpamMover

__all__ = ["MailboxError", "MailboxReader", "SpamMover"]
