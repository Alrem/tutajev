"""A guard that exposes only an allow-listed subset of tutaproxy's ``TutaClient``."""

from __future__ import annotations

from typing import Any

# Every TutaClient method the adapter may call for reading. All of them only read mail
# data; login creates the API session, get_mail_details requests a blob read token.
# TutaClient.logout is deliberately not allowed: it deletes a wrong URL and swallows the
# error, leaving the session open. Use ReadOnlyTutaClient.close_session instead.
ALLOWED_METHODS = frozenset(
    {
        "login",
        "get_folders",
        "get_mails_in_folder",
        "get_mail_group_key",
        "get_mail_details",
        "get_file",
        # Generic sys GET helper and URL builder; used to load former (rotated) group keys.
        "_get",
        "_url",
    }
)

# The only mutating method, unlocked explicitly by adapters created with move permission.
# It moves mails to a system folder; the adapter always passes the Spam folder type.
MOVE_TO_SYSTEM_FOLDER = "simple_move_mails"


class ReadOnlyViolation(RuntimeError):
    """Raised when code tries to call a TutaClient method that is not on the allow-list."""


class ReadOnlyTutaClient:
    """Wraps a ``TutaClient`` (or a fake) and blocks every method not allow-listed.

    ``allow_move`` additionally unlocks :data:`MOVE_TO_SYSTEM_FOLDER`. Everything else
    (delete_mails, move_mails_to_folder, mark_mails_unread, apply_labels, send_*, ...)
    stays blocked.
    """

    def __init__(self, client: Any, allow_move: bool = False) -> None:
        self._client = client
        self._allowed = ALLOWED_METHODS | {MOVE_TO_SYSTEM_FOLDER} if allow_move else ALLOWED_METHODS

    async def close_session(self, list_id: str, element_id: str, access_token: str) -> None:
        """Delete our own API session (``DELETE /rest/sys/session/<listId>/<elementId>``).

        The generic ``_delete`` stays blocked; only this exact URL can be deleted.
        """
        url = self._client._url("sys", "session", list_id, element_id)
        await self._client._delete(url, access_token)

    def __getattr__(self, name: str) -> Any:
        if name not in self._allowed:
            raise ReadOnlyViolation(f"TutaClient.{name} is not allowed")
        return getattr(self._client, name)
