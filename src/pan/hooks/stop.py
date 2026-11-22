from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from pan.errors import ThreadNotFoundError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import WorkerStatus
from pan.seams import SlackAdapter, ThreadMap
from pan.transcript import last_assistant_text

logger = initialise_logger(__name__)

_DEFAULT_SUMMARY = "Worker finished."


class StopHookPayload(BaseModel, frozen=True):
    session_id: str = ""
    transcript_path: Path | None = None
    hook_event_name: str = ""


def stop_hook(
    thread_ts: str,
    channel: str,
    thread_map: ThreadMap,
    slack: SlackAdapter,
    *,
    stdin: TextIO = sys.stdin,
) -> None:
    payload = StopHookPayload.model_validate_json(stdin.read())
    if thread_map.get(thread_ts) is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread_ts}")

    summary = _DEFAULT_SUMMARY
    if payload.transcript_path is not None:
        extracted = last_assistant_text(payload.transcript_path)
        if extracted:
            summary = extracted

    slack_post(slack, channel, thread_ts, summary)
    thread_map.update_status(thread_ts, WorkerStatus.DONE)
    logger.info(f"stop hook thread={thread_ts} status=done")
