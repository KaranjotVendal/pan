from __future__ import annotations

from pan.errors import UnauthorizedSenderError
from pan.logging import initialise_logger
from pan.models import UserPolicy

logger = initialise_logger(__name__)

_CHANNEL_WILDCARD = "*"


def auth_check(slack_user: str, channel: str, users: dict[str, UserPolicy]) -> None:
    policy = users.get(slack_user)
    if policy is None:
        logger.warning(f"dropped unauthorized sender in channel {channel}")
        raise UnauthorizedSenderError(f"sender not in allowlist: {slack_user}")

    if _CHANNEL_WILDCARD in policy.channels or channel in policy.channels:
        return

    logger.warning(f"dropped sender outside channel policy in channel {channel}")
    raise UnauthorizedSenderError(f"sender {slack_user} not permitted in channel {channel}")
