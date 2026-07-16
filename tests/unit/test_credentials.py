from __future__ import annotations

import json
import logging
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from pan.credentials import load_credentials, save_credentials
from pan.errors import CredentialsError
from pan.models import SlackCredentials

FAKE_BOT_TOKEN = "xoxb-fake-bot-000000-abcdef"
FAKE_APP_TOKEN = "xapp-fake-app-111111-uvwxyz"


def _fake_credentials() -> SlackCredentials:
    return SlackCredentials(
        bot_token=SecretStr(FAKE_BOT_TOKEN),
        app_token=SecretStr(FAKE_APP_TOKEN),
    )


@pytest.fixture
def credentials_logger_capture(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    logger = logging.getLogger("pan.credentials")
    logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        logger.removeHandler(caplog.handler)


def test_save_credentials_writes_mode_0600(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"

    save_credentials(_fake_credentials(), credentials_path)

    mode = stat.S_IMODE(credentials_path.stat().st_mode)
    assert mode == 0o600


def test_save_over_pre_existing_loose_file_ends_at_0600(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}")
    credentials_path.chmod(0o644)

    save_credentials(_fake_credentials(), credentials_path)

    assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600


def test_saved_credentials_round_trip_through_disk(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    save_credentials(_fake_credentials(), credentials_path)

    loaded = load_credentials(credentials_path)

    assert loaded.bot_token.get_secret_value() == FAKE_BOT_TOKEN
    assert loaded.app_token.get_secret_value() == FAKE_APP_TOKEN


def test_loaded_credentials_mask_tokens_in_repr_and_str(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    save_credentials(_fake_credentials(), credentials_path)

    loaded = load_credentials(credentials_path)

    for rendered in (repr(loaded), str(loaded)):
        assert FAKE_BOT_TOKEN not in rendered
        assert FAKE_APP_TOKEN not in rendered
        assert "**********" in rendered


def test_missing_file_raises_credentials_error(tmp_path: Path) -> None:
    with pytest.raises(CredentialsError):
        load_credentials(tmp_path / "absent.json")


def test_malformed_file_raises_credentials_error(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{ not json")

    with pytest.raises(CredentialsError):
        load_credentials(credentials_path)


@pytest.mark.usefixtures("credentials_logger_capture")
def test_loose_permissions_load_but_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text(
        json.dumps({"bot_token": FAKE_BOT_TOKEN, "app_token": FAKE_APP_TOKEN})
    )
    credentials_path.chmod(0o644)

    with caplog.at_level(logging.WARNING, logger="pan.credentials"):
        loaded = load_credentials(credentials_path)

    assert loaded.bot_token.get_secret_value() == FAKE_BOT_TOKEN
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    # The warning must never carry a token value.
    for record in caplog.records:
        assert FAKE_BOT_TOKEN not in record.getMessage()
        assert FAKE_APP_TOKEN not in record.getMessage()


@pytest.mark.usefixtures("credentials_logger_capture")
def test_mode_0600_does_not_warn(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    credentials_path = tmp_path / "credentials.json"
    save_credentials(_fake_credentials(), credentials_path)

    with caplog.at_level(logging.WARNING, logger="pan.credentials"):
        load_credentials(credentials_path)

    assert not any(record.levelno == logging.WARNING for record in caplog.records)
