from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOG_DIR = Path.home() / ".pan" / "logs"
LOG_FILE = LOG_DIR / "pan.log"
LOG_FORMAT = "%(levelname)s | %(message)s | %(asctime)s | %(name)s | %(funcName)s"
CONSOLE_LEVEL_ENV = "PAN_LOG_LEVEL"
DEFAULT_CONSOLE_LEVEL = "WARNING"
# Points the log file at another folder; the test suite sets it so tests never write to the
# real log.
LOG_DIR_ENV = "PAN_LOG_DIR"


def _log_file() -> Path:
    override = os.environ.get(LOG_DIR_ENV)
    log_dir = Path(override) if override else LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / LOG_FILE.name


def _resolve_console_level() -> int:
    raw_level = os.environ.get(CONSOLE_LEVEL_ENV, DEFAULT_CONSOLE_LEVEL).upper()
    resolved = logging.getLevelNamesMapping().get(raw_level)
    if resolved is None:
        return logging.WARNING
    return resolved


def initialise_logger(name: str, overwrite_level: int | None = None) -> logging.Logger:
    """Return a configured logger for `name`.

    The file handler always captures DEBUG to ``~/.pan/logs/pan.log`` (or ``pan.log`` in
    ``PAN_LOG_DIR`` when set) so a full trace is available after the fact. The console handler writes to **stderr**
    (never stdout, so it can't corrupt command output / ``--json``) and defaults
    to **WARNING** — quiet on a normal run. Raise console verbosity with the
    ``PAN_LOG_LEVEL`` env var (e.g. ``PAN_LOG_LEVEL=DEBUG``); an unrecognized
    value falls back to WARNING.
    """
    logger = logging.getLogger(name)

    if logger.handlers:
        # Idempotent: repeat calls return the same logger without stacking handlers.
        if overwrite_level is not None:
            logger.setLevel(overwrite_level)
        return logger

    logger.setLevel(overwrite_level if overwrite_level is not None else logging.DEBUG)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT)

    file_handler = logging.FileHandler(_log_file())
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(_resolve_console_level())
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger
