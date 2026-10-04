from __future__ import annotations

import pytest

from tutajev.models import MessageSummary
from tutajev.rules import dmarc_quarantined

TUTA_DEFAULT_POLICY = (
    "w10.tutanota.de (dis=spam; info=dmarc default policy); dmarc=fail (dis=spam p=quarantine; "
    "aspf=r; adkim=r; pSrc=config) header.from=manilashaker.com"
)
TUTA_SIGNED_NEWSLETTER = (
    "w10.tutanota.de (dis=spam; info=dmarc domain policy); dmarc=fail (dis=spam p=quarantine; "
    "aspf=r; adkim=r; pSrc=domain) header.from=intercity.pl; dkim=pass header.d=send24.pl"
)


def _message(*results: str) -> MessageSummary:
    return MessageSummary(
        id="m",
        received_at=None,
        date=None,
        sender=None,
        reply_to=(),
        subject="",
        text_body="",
        urls=(),
        attachments=(),
        has_html=False,
        authentication_results=results,
    )


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ((TUTA_DEFAULT_POLICY,), True),
        (("mail.w11.tutanota.de; dmarc=fail (p=reject dis=reject) header.from=x.example",), True),
        ((TUTA_SIGNED_NEWSLETTER,), False),
        (("w10.tutanota.de; dmarc=pass (p=quarantine dis=none) header.from=x.example",), False),
        (("w10.tutanota.de; dmarc=fail (p=none dis=none) header.from=x.example",), False),
        # Results from other servers, e.g. inside a forwarded message, are not trusted.
        (("mx.google.com; dmarc=fail (p=QUARANTINE dis=QUARANTINE) header.from=x.example",), False),
        (("eviltutanota.de; dmarc=fail (dis=spam p=quarantine)",), False),
        ((), False),
    ],
)
def test_dmarc_quarantined(results: tuple[str, ...], expected: bool) -> None:
    assert dmarc_quarantined(_message(*results)) is expected
