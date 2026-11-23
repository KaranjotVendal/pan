from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from pydantic import BaseModel

from pan.logging import initialise_logger
from pan.seams import ThreadMap

logger = initialise_logger(__name__)


class InboxLink(BaseModel, frozen=True):
    thread_id: str
    url: str


# Bounds the walk over a tool response, so a deeply nested or self-similar payload cannot
# make the hook slow. A real Inbox send result is two or three levels deep.
_MAX_SEARCH_DEPTH = 8


_JSON_DECODER = json.JSONDecoder()


def _embedded_json_values(text: str) -> list[Any]:
    # The JSON objects and arrays a text holds. The Inbox's send result is prose first and
    # the structured result after it ("Sent to the Plannotator Inbox (thread ...). ...\n\n
    # {...}"), so the whole text is not JSON; each `{` or `[` is tried as the start of one.
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    values: list[Any] = []
    position = 0
    while True:
        starts = [
            index for index in (text.find("{", position), text.find("[", position)) if index >= 0
        ]
        if not starts:
            return values
        start = min(starts)
        try:
            value, end = _JSON_DECODER.raw_decode(text, start)
        except ValueError:
            position = start + 1
            continue
        values.append(value)
        position = end


def _find_link(value: Any, depth: int = 0) -> InboxLink | None:
    # Depth-first search for the first object carrying both a string `thread_id` and a
    # string `url`. A string is searched for the JSON it holds (an MCP text block holds the
    # result as text, after a line of prose).
    if depth > _MAX_SEARCH_DEPTH:
        return None
    if isinstance(value, str):
        for decoded in _embedded_json_values(value):
            if isinstance(decoded, dict | list):
                found = _find_link(decoded, depth + 1)
                if found is not None:
                    return found
        return None
    if isinstance(value, dict):
        thread_id, url = value.get("thread_id"), value.get("url")
        if isinstance(thread_id, str) and thread_id and isinstance(url, str) and url:
            return InboxLink(thread_id=thread_id, url=url)
        for nested in value.values():
            found = _find_link(nested, depth + 1)
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for nested in value:
            found = _find_link(nested, depth + 1)
            if found is not None:
                return found
    return None


def parse_inbox_link(raw_payload: str) -> InboxLink | None:
    # The Claude Code PostToolUse payload: {tool_name, tool_input, tool_response, ...}.
    # `tool_response` may be the structured result (a dict), a string, or a list of content
    # blocks (possibly under a `content` key) whose `text` is the Inbox's prose line followed
    # by the result as JSON. All are searched; anything else is None.
    try:
        payload = json.loads(raw_payload)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return _find_link(payload.get("tool_response"))


def _sent_thread_name(raw_payload: str) -> str | None:
    payload = json.loads(raw_payload)
    tool_input = payload.get("tool_input") if isinstance(payload, dict) else None
    thread_name = tool_input.get("thread") if isinstance(tool_input, dict) else None
    return thread_name if isinstance(thread_name, str) else None


def inbox_link_hook(
    thread_ts: str,
    thread_map: ThreadMap,
    *,
    stdin: TextIO = sys.stdin,
    session_name: str | None = None,
) -> None:
    # Record the worker's Inbox thread id and link on its thread record. Never posts to
    # Slack: the link is shown later by `check`. The first link wins, unless a later send
    # names the record's own Inbox thread (the worker may also write in other threads).
    raw_payload = stdin.read()
    link = parse_inbox_link(raw_payload)
    if link is None:
        logger.info(f"inbox-link hook found no thread link thread={thread_ts}")
        return
    record = thread_map.get(thread_ts, session_name=session_name)
    if record is None:
        logger.warning(f"inbox-link hook: no thread record thread={thread_ts}")
        return
    names_own_thread = (
        record.inbox_thread_name is not None
        and _sent_thread_name(raw_payload) == record.inbox_thread_name
    )
    if record.inbox_thread_id is not None and not names_own_thread:
        logger.info(f"inbox-link hook kept the recorded link thread={thread_ts}")
        return
    thread_map.record_inbox_thread(
        thread_ts, link.thread_id, link.url, session_name=record.terminal_name
    )
    logger.info(f"inbox-link hook recorded the Inbox thread thread={thread_ts}")
