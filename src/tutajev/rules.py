"""Deterministic spam rules that run alongside the classifier."""

from __future__ import annotations

import re

from tutajev.models import MessageSummary

# Only results added by Tuta's own mail servers are trusted. A sender can forge an
# Authentication-Results header, but a forged "fail" only gets their own message moved.
TRUSTED_AUTHSERV_IDS = ("tutanota.de",)

_DMARC_FAIL = re.compile(r"\bdmarc=fail\b\s*(?:\((?P<comment>[^)]*)\))?", re.IGNORECASE)
_SPAM_DISPOSITION = re.compile(r"\bdis=(?:spam|quarantine|reject)\b", re.IGNORECASE)
_DKIM_PASS = re.compile(r"\bdkim=pass\b", re.IGNORECASE)


def _authserv_id(value: str) -> str:
    return re.split(r"[\s;(]", value.strip(), maxsplit=1)[0].lower()


def _trusted(authserv_id: str) -> bool:
    return any(authserv_id == t or authserv_id.endswith("." + t) for t in TRUSTED_AUTHSERV_IDS)


def dmarc_quarantined(message: MessageSummary) -> bool:
    """True when Tuta reports an unsigned message failing DMARC with a quarantine or reject policy.

    Tuta records this as ``dmarc=fail (dis=spam p=quarantine ...)`` but may still deliver
    the message to the Inbox. Messages with any passing DKIM signature are left alone: real
    newsletters sent through a mailing service often fail DMARC alignment while being signed
    by the service's own domain.
    """
    for value in message.authentication_results:
        if not _trusted(_authserv_id(value)) or _DKIM_PASS.search(value):
            continue
        for match in _DMARC_FAIL.finditer(value):
            if _SPAM_DISPOSITION.search(match.group("comment") or ""):
                return True
    return False
