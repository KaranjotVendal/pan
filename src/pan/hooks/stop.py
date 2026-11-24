from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from pan.adapters.clock import SystemClock
from pan.errors import ThreadNotFoundError
from pan.logging import initialise_logger
from pan.models import ReplyMode, StatusState, WorkerStatus
from pan.seams import Clock, SlackAdapter, ThreadMap
from pan.status_message import post_answer, publish_status
from pan.transcript import last_assistant_text

logger = initialise_logger(__name__)


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
    clock: Clock | None = None,
) -> None:
    # Runs at the end of every turn, not only at the end of the work, so it marks the
    # session IDLE (live, waiting for a follow-up), never DONE, and edits the thread's status
    # message to Finished. Replies are on demand: the turn's answer is posted, alone and with
    # nothing about the session around it, only when the thread asked for replies in Slack,
    # and then at every turn end. `session_name` picks this session's record in the thread;
    # once the thread moved to another session (untied), nothing changes there any more.
    payload = StopHookPayload.model_validate_json(stdin.read())
    record = thread_map.get(thread_ts, session_name=session_name)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread_ts}")

    thread_map.update_status(thread_ts, WorkerStatus.IDLE, session_name=record.terminal_name)
    if record.tied:
        publish_status(
            slack=slack,
            thread_map=thread_map,
            record=record,
            state=StatusState.FINISHED,
            clock=clock or SystemClock(),
        )
    if record.tied and record.reply_mode is ReplyMode.SLACK:
        answer = (
            last_assistant_text(payload.transcript_path)
            if payload.transcript_path is not None
            else None
        )
        if answer:
            post_answer(slack=slack, thread_map=thread_map, record=record, answer=answer)
        else:
            logger.info(f"stop hook found no answer to post thread={thread_ts}")
    logger.info(f"stop hook thread={thread_ts} status=idle reply_mode={record.reply_mode}")
