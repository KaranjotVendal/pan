from __future__ import annotations

import logging
import sys
import uuid

import pytest

from pan.logging import initialise_logger


def _unique_name() -> str:
    return f"pan.test.{uuid.uuid4().hex}"


def test_has_file_handler_at_debug_and_stderr_stream_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PAN_LOG_LEVEL", raising=False)
    logger = initialise_logger(_unique_name())

    file_handlers = [h for h in logger.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].level == logging.DEBUG

    stream_handlers = [
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert len(stream_handlers) == 1
    assert stream_handlers[0].stream is sys.stderr


def test_console_level_defaults_to_warning_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAN_LOG_LEVEL", raising=False)
    logger = initialise_logger(_unique_name())

    stream_handler = next(
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    )
    assert stream_handler.level == logging.WARNING


def test_console_level_follows_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAN_LOG_LEVEL", "DEBUG")
    logger = initialise_logger(_unique_name())

    stream_handler = next(
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    )
    assert stream_handler.level == logging.DEBUG


def test_invalid_env_falls_back_to_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAN_LOG_LEVEL", "NONSENSE")
    logger = initialise_logger(_unique_name())

    stream_handler = next(
        h
        for h in logger.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    )
    assert stream_handler.level == logging.WARNING


def test_idempotent_no_stacked_handlers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAN_LOG_LEVEL", raising=False)
    name = _unique_name()
    first = initialise_logger(name)
    handler_count = len(first.handlers)
    second = initialise_logger(name)

    assert second is first
    assert len(second.handlers) == handler_count


def test_overwrite_level_adjusts_existing_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAN_LOG_LEVEL", raising=False)
    name = _unique_name()
    initialise_logger(name)
    logger = initialise_logger(name, overwrite_level=logging.ERROR)

    assert logger.level == logging.ERROR


def test_propagate_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAN_LOG_LEVEL", raising=False)
    logger = initialise_logger(_unique_name())

    assert logger.propagate is False
