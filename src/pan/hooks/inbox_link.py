from __future__ import annotations

import json
import re
import sys
from typing import Any, TextIO

from pydantic import BaseModel

from pan.config import PLANNOTATOR_INBOX_TOOL_NAME, REPL_TOOL_NAME
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

# The Inbox's send result in prose: "Sent to the Plannotator Inbox (thread <id>, <url>)", also
# "Already sent to the ..." and "... Plannotator Inbox asking 1 question (thread <id>, <url>)".
# Code mode's REPL tool returns whatever its code returned, often only this prose.
_INBOX_PROSE_LINK = re.compile(r"Plannotator Inbox\b[^()]*\(thread ([^\s,()]+), ([^\s()]+)\)")


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


def _find_prose_link(value: Any, depth: int = 0) -> InboxLink | None:
    # Depth-first search of every string in the value for the Inbox's prose line; the first
    # match wins.
    if depth > _MAX_SEARCH_DEPTH:
        return None
    if isinstance(value, str):
        match = _INBOX_PROSE_LINK.search(value)
        return InboxLink(thread_id=match[1], url=match[2]) if match else None
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        for nested in value:
            found = _find_prose_link(nested, depth + 1)
            if found is not None:
                return found
    return None


def _payload(raw_payload: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_payload)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def parse_inbox_link(raw_payload: str) -> InboxLink | None:
    # The Claude Code PostToolUse payload: {tool_name, tool_input, tool_response, ...}.
    # `tool_response` may be the structured result (a dict), a string, or a list of content
    # blocks (possibly under a `content` key) whose `text` is the Inbox's prose line followed
    # by the result as JSON. All are searched for the JSON result first, then for the prose
    # line alone (what a code mode REPL call often returns); anything else is None.
    payload = _payload(raw_payload)
    if payload is None:
        return None
    tool_response = payload.get("tool_response")
    return _find_link(tool_response) or _find_prose_link(tool_response)


def _is_unrelated_repl_call(raw_payload: str) -> bool:
    # A code mode REPL call is about the Inbox only when its code names the Inbox tool or its
    # result holds the Inbox's prose line; any other REPL call is not the hook's business.
    payload = _payload(raw_payload)
    if payload is None or payload.get("tool_name") != REPL_TOOL_NAME:
        return False
    names_inbox_tool = PLANNOTATOR_INBOX_TOOL_NAME in json.dumps(payload.get("tool_input"))
    return not names_inbox_tool and _find_prose_link(payload.get("tool_response")) is None


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
    if _is_unrelated_repl_call(raw_payload):
        logger.debug(f"inbox-link hook skipped a REPL call with no Inbox use thread={thread_ts}")
        return
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
