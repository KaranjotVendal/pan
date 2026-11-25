from __future__ import annotations

import sys
from typing import TextIO

from pydantic import BaseModel

from pan.backend.status_message import publish_status
from pan.core.errors import ThreadNotFoundError
from pan.core.models import Conversation, StatusState, WorkerStatus
from pan.core.seams import Clock, Reporter, ThreadMap
from pan.utils.clock import SystemClock
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

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
    conversation: Conversation,
    thread_map: ThreadMap,
    reporter: Reporter,
    *,
    stdin: TextIO = sys.stdin,
    session_name: str | None = None,
    clock: Clock | None = None,
) -> None:
    # Questions go to the session's Inbox thread, so a prompt waiting on the person marks the
    # thread BLOCKED and, while the thread is still tied to this session, edits its status
    # message to "Waiting for you" with the prompt's text under the task. Nothing new is
    # posted. An idle reminder changes nothing. `session_name` picks this session's record in
    # the thread.
    payload = NotificationHookPayload.model_validate_json(stdin.read())
    record = thread_map.get(conversation, session_name=session_name)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={conversation.id}")
    if not is_blocking_notification(payload):
        logger.info(
            f"notification hook conversation={conversation.key} ignored "
            f"type={payload.notification_type or 'none'}"
        )
        return

    thread_map.update_status(conversation, WorkerStatus.BLOCKED, session_name=record.terminal_name)
    if record.tied:
        publish_status(
            reporter=reporter,
            thread_map=thread_map,
            record=record,
            state=StatusState.WAITING,
            clock=clock or SystemClock(),
            detail=payload.message or None,
        )
    logger.info(f"notification hook conversation={conversation.key} status=blocked")
