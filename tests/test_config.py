from __future__ import annotations

from pathlib import Path

import pytest

from tutajev.config import ConfigError, load_settings


def test_loads_from_env_file(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TUTA_EMAIL=me@tuta.com\nTUTA_PASSWORD=secret\nTUTAPROXY_PATH=/opt/tp\nLOG_LEVEL=debug\n"
    )
    settings = load_settings(env_file)

    assert settings.tuta_email == "me@tuta.com"
    assert settings.tuta_password == "secret"
    assert settings.tutaproxy_path == Path("/opt/tp")
    assert settings.log_level == "DEBUG"


def test_environment_overrides_env_file(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TUTA_EMAIL=file@tuta.com\nTUTA_PASSWORD=x\n")
    clean_env.setenv("TUTA_EMAIL", "env@tuta.com")
    assert load_settings(env_file).tuta_email == "env@tuta.com"


def test_missing_credentials(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="TUTA_EMAIL, TUTA_PASSWORD"):
        load_settings(None)


def test_invalid_log_level(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TUTA_EMAIL", "a@b.c")
    clean_env.setenv("TUTA_PASSWORD", "x")
    clean_env.setenv("LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_settings(None)


def test_secrets_not_in_repr(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TUTA_EMAIL", "a@b.c")
    clean_env.setenv("TUTA_PASSWORD", "hunter2")
    clean_env.setenv("TUTA_TOTP_SECRET", "JBSWY3DPEHPK3PXP")
    clean_env.setenv("TYPESAFE_API_KEY", "ts-secret-key")
    settings = load_settings(None)
    assert settings.typesafe_api_key == "ts-secret-key"
    text = repr(settings)
    assert "ts-secret-key" not in text
    assert "hunter2" not in text
    assert "JBSWY3DPEHPK3PXP" not in text


def test_folders_parsing(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TUTA_EMAIL", "a@b.c")
    clean_env.setenv("TUTA_PASSWORD", "x")
    assert load_settings(None).folders == ("inbox",)
    clean_env.setenv("TUTAJEV_FOLDERS", " inbox , Newsletters,,inbox ")
    assert load_settings(None).folders == ("inbox", "Newsletters")


def test_classifier_defaults_to_jev(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TUTA_EMAIL", "a@b.c")
    clean_env.setenv("TUTA_PASSWORD", "x")
    assert load_settings(None).classifier == "jev"


def test_clef_settings(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TUTA_EMAIL", "a@b.c")
    clean_env.setenv("TUTA_PASSWORD", "x")
    clean_env.setenv("TUTAJEV_CLASSIFIER", "Clef-Flash")
    clean_env.setenv("CLOUDFLARE_ACCOUNT_ID", "acc")
    clean_env.setenv("CLOUDFLARE_API_TOKEN", "tok")
    settings = load_settings(None)

    assert settings.classifier == "clef-flash"
    assert settings.cloudflare_account_id == "acc"
    assert "tok" not in repr(settings)


def test_invalid_classifier(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("TUTA_EMAIL", "a@b.c")
    clean_env.setenv("TUTA_PASSWORD", "x")
    clean_env.setenv("TUTAJEV_CLASSIFIER", "gpt")
    with pytest.raises(ConfigError, match="TUTAJEV_CLASSIFIER"):
        load_settings(None)
