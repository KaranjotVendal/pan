from __future__ import annotations

import sys
from typing import TextIO

from pydantic import BaseModel

from pan.errors import ThreadNotFoundError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import ReplyMode, WorkerStatus
from pan.seams import SlackAdapter, ThreadMap

logger = initialise_logger(__name__)

_DEFAULT_QUESTION = "Worker needs input."

# Claude Code sends a Notification both when a prompt waits on the person (a permission or an
# input dialog) and after about a minute of idling at the prompt. Only the first means the
# session is blocked; the idle one says nothing pan does not already know from the Stop hook.
_BLOCKING_TYPES = frozenset({"permission_prompt", "elicitation_dialog"})
_NON_BLOCKING_TYPES = frozenset({"idle_prompt", "auth_success"})
# Used when the payload carries no known type: words that mean a prompt waits for approval.
_BLOCKING_WORDS = ("permission", "approv")


class NotificationHookPayload(BaseModel, frozen=True):
    session_id: str = ""
    message: str = ""
    hook_event_name: str = ""
    notification_type: str = ""


def is_blocking_notification(payload: NotificationHookPayload) -> bool:
    if payload.notification_type in _BLOCKING_TYPES:
        return True
    if payload.notification_type in _NON_BLOCKING_TYPES:
        return False
    lowered_message = payload.message.lower()
    return any(word in lowered_message for word in _BLOCKING_WORDS)


def notification_hook(
    thread_ts: str,
    channel: str,
    thread_map: ThreadMap,
    slack: SlackAdapter,
    *,
    stdin: TextIO = sys.stdin,
    session_name: str | None = None,
) -> None:
    # Questions go to the session's Inbox thread, so a prompt waiting on the person only marks
    # the thread BLOCKED; it posts the prompt to Slack only when the thread, still tied to this
    # session, asked for replies in Slack. An idle reminder changes nothing. `session_name`
    # picks this session's record in the thread.
    payload = NotificationHookPayload.model_validate_json(stdin.read())
    record = thread_map.get(thread_ts, session_name=session_name)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread_ts}")
    if not is_blocking_notification(payload):
        logger.info(
            f"notification hook thread={thread_ts} ignored "
            f"type={payload.notification_type or 'none'}"
        )
        return

    if record.tied and record.reply_mode is ReplyMode.SLACK:
        question = payload.message or _DEFAULT_QUESTION
        slack_post(slack, channel, thread_ts, question)
    thread_map.update_status(thread_ts, WorkerStatus.BLOCKED, session_name=record.terminal_name)
    logger.info(f"notification hook thread={thread_ts} status=blocked")
