from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from pan.logging import initialise_logger
from pan.seams import ThreadMap

logger = initialise_logger(__name__)


class SessionStartHookPayload(BaseModel, frozen=True):
    session_id: str = ""
    transcript_path: Path | None = None
    hook_event_name: str = ""


def session_start_hook(
    thread_ts: str,
    thread_map: ThreadMap,
    *,
    stdin: TextIO = sys.stdin,
    session_name: str | None = None,
) -> None:
    # Record the worker's Claude session id and transcript path on its thread record, so
    # `pan read --full` can render the transcript without any session index. Posts nothing.
    payload = SessionStartHookPayload.model_validate_json(stdin.read())
    if not payload.session_id:
        logger.warning(f"session-start hook without a session id thread={thread_ts}")
        return
    thread_map.record_session(
        thread_ts, payload.session_id, payload.transcript_path, session_name=session_name
    )
    logger.info(f"session-start hook thread={thread_ts}")
