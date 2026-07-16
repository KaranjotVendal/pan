from __future__ import annotations

import pytest

from pan.errors import (
    ConfigMissingError,
    CredentialsError,
    GatedOpDeniedError,
    HerdrError,
    InboxError,
    PanError,
    SlackPostError,
    SpawnError,
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
        HerdrError,
        SlackPostError,
        GatedOpDeniedError,
    ],
)
def test_taxonomy_subclasses_pan_error(exception_class: type[PanError]) -> None:
    assert issubclass(exception_class, PanError)
