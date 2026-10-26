"""Mailbox access. Everything outside this package depends only on :class:`MailboxReader`."""

from tutajev.mailbox.port import MailboxError, MailboxReader, MailLocator, SpamMover

__all__ = ["MailLocator", "MailboxError", "MailboxReader", "SpamMover"]
