"""Application settings loaded from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_TUTAPROXY_PATH = "./vendor/tutaproxy"


class ConfigError(Exception):
    """Raised when required settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    tuta_email: str
    tuta_password: str = field(repr=False)
    tuta_totp_secret: str = field(default="", repr=False)
    tutaproxy_path: Path = Path(DEFAULT_TUTAPROXY_PATH)
    typesafe_api_key: str = field(default="", repr=False)
    folders: tuple[str, ...] = ("inbox",)
    log_level: str = "INFO"


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """Read settings from the process environment, after loading ``env_file`` if it exists.

    Variables already present in the environment take precedence over the file.
    """
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)

    email = os.environ.get("TUTA_EMAIL", "").strip()
    password = os.environ.get("TUTA_PASSWORD", "")
    missing = [
        name for name, value in (("TUTA_EMAIL", email), ("TUTA_PASSWORD", password)) if not value
    ]
    if missing:
        raise ConfigError(f"Missing required settings: {', '.join(missing)}")

    log_level = os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"Invalid LOG_LEVEL: {log_level}")

    return Settings(
        tuta_email=email,
        tuta_password=password,
        tuta_totp_secret=os.environ.get("TUTA_TOTP_SECRET", "").strip(),
        tutaproxy_path=Path(os.environ.get("TUTAPROXY_PATH", DEFAULT_TUTAPROXY_PATH)).expanduser(),
        typesafe_api_key=os.environ.get("TYPESAFE_API_KEY", "").strip(),
        folders=_parse_folders(os.environ.get("TUTAJEV_FOLDERS", "")),
        log_level=log_level,
    )


def _parse_folders(value: str) -> tuple[str, ...]:
    """Parse a comma-separated folder list; empty means just the Inbox."""
    folders = tuple(dict.fromkeys(f.strip() for f in value.split(",") if f.strip()))
    return folders or ("inbox",)
