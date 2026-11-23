from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from pan.errors import ThreadNotFoundError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import ReplyMode, WorkerStatus
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
    session_name: str | None = None,
) -> None:
    # Runs at the end of every turn, not only at the end of the work, so it marks the
    # session IDLE (live, waiting for a follow-up), never DONE. Replies are on demand: the
    # turn's answer is posted only when the thread asked for replies in Slack, and then at
    # every turn end. `session_name` picks this session's record in the thread; once the
    # thread moved to another session (untied), nothing is posted there any more.
    payload = StopHookPayload.model_validate_json(stdin.read())
    record = thread_map.get(thread_ts, session_name=session_name)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread_ts}")

    if record.tied and record.reply_mode is ReplyMode.SLACK:
        summary = _DEFAULT_SUMMARY
        if payload.transcript_path is not None:
            extracted = last_assistant_text(payload.transcript_path)
            if extracted:
                summary = extracted
        slack_post(slack, channel, thread_ts, summary)

    thread_map.update_status(thread_ts, WorkerStatus.IDLE, session_name=record.terminal_name)
    logger.info(f"stop hook thread={thread_ts} status=idle reply_mode={record.reply_mode}")
