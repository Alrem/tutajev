from __future__ import annotations

from pathlib import Path

import pytest

from tutajev.models import RawMessage

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).parent.parent
TUTAPROXY_PATH = PROJECT_ROOT / "vendor" / "tutaproxy"


def load_eml(name: str) -> RawMessage:
    return RawMessage(id=name, received_at=None, raw=(FIXTURES / name).read_bytes())


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for var in (
        "TUTA_EMAIL",
        "TUTA_PASSWORD",
        "TUTA_TOTP_SECRET",
        "TUTAPROXY_PATH",
        "LOG_LEVEL",
        "TYPESAFE_API_KEY",
        "TUTAJEV_CLASSIFIER",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_API_TOKEN",
        "TUTAJEV_FOLDERS",
    ):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch
