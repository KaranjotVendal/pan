from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from pydantic import ValidationError

from pan.errors import CredentialsError
from pan.logging import initialise_logger
from pan.models import SlackCredentials

logger = initialise_logger(__name__)

DEFAULT_CREDENTIALS_PATH = Path.home() / ".pan" / "credentials.json"

# Any group- or other-permission bit means the file is readable beyond the owner.
_GROUP_AND_OTHER_BITS = 0o077


def save_credentials(credentials: SlackCredentials, path: Path | None = None) -> None:
    credentials_path = path if path is not None else DEFAULT_CREDENTIALS_PATH
    credentials_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "bot_token": credentials.bot_token.get_secret_value(),
        "app_token": credentials.app_token.get_secret_value(),
    }

    # Write the secret to a fresh temp file that is 0600 from the instant it is
    # created (O_EXCL refuses to open an existing path, so a truncated pre-existing
    # loose-perm file — or a planted symlink — can never receive the token bytes),
    # then atomically swap it into place with os.replace, which carries the temp
    # file's 0600 mode. This closes the write-window race a create-then-chmod on the
    # destination would leave open when the destination already existed at looser perms.
    temp_path = credentials_path.with_name(credentials_path.name + ".tmp")
    temp_path.unlink(missing_ok=True)
    descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w") as credentials_file:
            json.dump(payload, credentials_file)
        os.replace(temp_path, credentials_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    logger.info(f"credentials saved path={credentials_path}")


def load_credentials(path: Path | None = None) -> SlackCredentials:
    credentials_path = path if path is not None else DEFAULT_CREDENTIALS_PATH

    try:
        raw_text = credentials_path.read_text()
    except FileNotFoundError as error:
        raise CredentialsError(f"credentials file not found: {credentials_path}") from error

    mode = stat.S_IMODE(credentials_path.stat().st_mode)
    if mode & _GROUP_AND_OTHER_BITS:
        logger.warning(
            f"credentials file has loose permissions mode={mode:#o} path={credentials_path}"
        )

    try:
        raw_credentials = json.loads(raw_text)
        credentials = SlackCredentials.model_validate(raw_credentials)
    except (json.JSONDecodeError, ValidationError) as error:
        raise CredentialsError(f"credentials file is malformed: {credentials_path}") from error

    return credentials
