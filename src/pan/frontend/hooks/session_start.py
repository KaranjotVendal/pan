from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from pan.core.models import Conversation
from pan.core.seams import ThreadMap
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)


class SessionStartHookPayload(BaseModel, frozen=True):
    session_id: str = ""
    transcript_path: Path | None = None
    hook_event_name: str = ""


def session_start_hook(
    conversation: Conversation,
    thread_map: ThreadMap,
    *,
    stdin: TextIO = sys.stdin,
    session_name: str | None = None,
) -> None:
    # Record the worker's Claude session id and transcript path on its thread record, so
    # `pan read --full` can render the transcript without any session index. Posts nothing.
    payload = SessionStartHookPayload.model_validate_json(stdin.read())
    if not payload.session_id:
        logger.warning(f"session-start hook without a session id conversation={conversation.key}")
        return
    thread_map.record_session(
        conversation, payload.session_id, payload.transcript_path, session_name=session_name
    )
    logger.info(f"session-start hook conversation={conversation.key}")
