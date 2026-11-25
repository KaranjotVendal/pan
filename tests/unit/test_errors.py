from __future__ import annotations

import pytest

from pan.core.errors import (
    ConfigMissingError,
    CredentialsError,
    InboxError,
    PanError,
    ServiceError,
    SlackPostError,
    SpawnError,
    TargetBlockedError,
    TerminalGoneError,
    TerminalHostError,
    TerminalNameTakenError,
    ThreadNotFoundError,
    UnauthorizedSenderError,
)


def test_pan_error_is_exception_subclass() -> None:
    assert issubclass(PanError, Exception)


@pytest.mark.parametrize(
    "exception_class",
    [
        UnauthorizedSenderError,
        ConfigMissingError,
        CredentialsError,
        InboxError,
        ThreadNotFoundError,
        SpawnError,
        TerminalHostError,
        SlackPostError,
        ServiceError,
    ],
)
def test_taxonomy_subclasses_pan_error(exception_class: type[PanError]) -> None:
    assert issubclass(exception_class, PanError)


@pytest.mark.parametrize(
    "exception_class", [TerminalGoneError, TargetBlockedError, TerminalNameTakenError]
)
def test_terminal_host_failure_kinds_subclass_terminal_host_error(
    exception_class: type[PanError],
) -> None:
    assert issubclass(exception_class, TerminalHostError)
