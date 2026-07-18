from __future__ import annotations

import pytest

from pan.errors import UnauthorizedSenderError
from pan.gateway.auth import auth_check
from pan.models import UserPolicy

_USERS = {
    "U_wild": UserPolicy(channels=["*"]),
    "U_scoped": UserPolicy(channels=["C1", "C2"]),
}


@pytest.mark.parametrize(
    "slack_user, channel",
    [
        ("U_wild", "C1"),
        ("U_wild", "C999"),
        ("U_scoped", "C1"),
        ("U_scoped", "C2"),
    ],
)
def test_allowed_returns_none(slack_user: str, channel: str) -> None:
    assert auth_check(slack_user, channel, _USERS) is None


@pytest.mark.parametrize(
    "slack_user, channel, users",
    [
        ("U_stranger", "C1", _USERS),
        ("U_scoped", "C3", _USERS),
        ("U_wild", "C1", {}),
        ("U_empty", "C1", {"U_empty": UserPolicy(channels=[])}),
    ],
    ids=[
        "non-allowlisted-user",
        "user-outside-channel-policy",
        "empty-allowlist-denies-all",
        "explicit-empty-channels-denies-all",
    ],
)
def test_denied_raises_unauthorized(
    slack_user: str, channel: str, users: dict[str, UserPolicy]
) -> None:
    with pytest.raises(UnauthorizedSenderError):
        auth_check(slack_user, channel, users)
