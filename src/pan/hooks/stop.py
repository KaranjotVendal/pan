from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from pan.errors import ThreadNotFoundError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import WorkerStatus
from pan.seams import SlackAdapter, ThreadMap

logger = initialise_logger(__name__)

_DEFAULT_SUMMARY = "Worker finished."


class StopHookPayload(BaseModel, frozen=True):
    session_id: str = ""
    transcript_path: Path | None = None
    hook_event_name: str = ""


def _last_assistant_text(transcript_path: Path) -> str | None:
    # Claude Code writes the session transcript as JSONL; the final summary is the
    # last assistant text block. The exact shape is version-dependent (live-verify).
    try:
        raw_lines = transcript_path.read_text().splitlines()
    except OSError:
        return None

    last_text: str | None = None
    for raw_line in raw_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
        if texts:
            last_text = "\n".join(texts)
    return last_text


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
        extracted = _last_assistant_text(payload.transcript_path)
        if extracted:
            summary = extracted

    slack_post(slack, channel, thread_ts, summary)
    thread_map.update_status(thread_ts, WorkerStatus.DONE)
    logger.info(f"stop hook thread={thread_ts} status=done")
