"""The mailbox port: the only interface the rest of the application uses to read mail."""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Literal, Protocol, Self, runtime_checkable

from tutajev.models import RawMessage

FolderKind = Literal["inbox", "spam", "trash", "archive", "sent"]
SYSTEM_FOLDERS: tuple[FolderKind, ...] = ("inbox", "spam", "trash", "archive", "sent")


class MailboxError(Exception):
    """Raised by adapters for authentication, network or decoding failures."""


class MailboxReader(Protocol):
    """Read-only access to a mailbox.

    Implementations must never modify mailbox state (flags, folders, messages).
    Use as an async context manager: entering authenticates, exiting logs out.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def list_folders(self) -> list[str]:
        """Return the names of all folders: system kinds first, then custom folder names."""
        ...

    async def list_recent(
        self, folder: str = "inbox", limit: int | None = 20, unread_only: bool = False
    ) -> list[RawMessage]:
        """Return up to ``limit`` messages from ``folder``, newest first.

        ``limit=None`` means no limit; ``unread_only`` keeps only unread messages. Reading
        never changes the unread state.

        ``folder`` is a system kind (see :data:`SYSTEM_FOLDERS`) or a custom folder name
        (matched case-insensitively).
        """
        ...


@runtime_checkable
class MailLocator(Protocol):
    """Lists which messages a folder holds, without fetching or decrypting them."""

    async def list_ids(self, folder: str) -> set[str]:
        """Return the ids of all messages in ``folder``, as used by ``RawMessage.id``."""
        ...


@runtime_checkable
class SpamMover(Protocol):
    """Moves messages to the Spam folder. The only mailbox mutation the application performs."""

    async def move_to_spam(self, message_ids: Sequence[str]) -> None:
        """Move messages previously returned by ``list_recent`` to Spam."""
        ...
