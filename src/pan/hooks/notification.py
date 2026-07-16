from __future__ import annotations

import sys
from typing import TextIO

from pydantic import BaseModel

from pan.errors import ThreadNotFoundError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import WorkerStatus
from pan.seams import SlackAdapter, ThreadMap

logger = initialise_logger(__name__)

_DEFAULT_QUESTION = "Worker needs input."


class NotificationHookPayload(BaseModel, frozen=True):
    session_id: str = ""
    message: str = ""
    hook_event_name: str = ""


def notification_hook(
    thread_ts: str,
    channel: str,
    thread_map: ThreadMap,
    slack: SlackAdapter,
    *,
    stdin: TextIO = sys.stdin,
) -> None:
    payload = NotificationHookPayload.model_validate_json(stdin.read())
    if thread_map.get(thread_ts) is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread_ts}")

    question = payload.message or _DEFAULT_QUESTION
    slack_post(slack, channel, thread_ts, question)
    thread_map.update_status(thread_ts, WorkerStatus.BLOCKED)
    logger.info(f"notification hook thread={thread_ts} status=blocked")
