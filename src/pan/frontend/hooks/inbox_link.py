from __future__ import annotations

import json
import re
import sys
from typing import Any, TextIO

from pydantic import BaseModel

from pan.core.seams import ThreadMap
from pan.utils.config import (
    PLANNOTATOR_INBOX_TOOL_MARKER,
    PLANNOTATOR_INBOX_TOOL_NAME,
    REPL_TOOL_NAME,
)
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)


class InboxLink(BaseModel, frozen=True):
    thread_id: str
    url: str


# Bounds the walk over a tool response, so a deeply nested or self-similar payload cannot
# make the hook slow. A real Inbox send result is two or three levels deep.
_MAX_SEARCH_DEPTH = 8


_JSON_DECODER = json.JSONDecoder()

# The Inbox's send result in prose, at the start of a line: "Sent to the Plannotator Inbox
# (thread <id>, <url>)", also "Already sent to the ..." and "Sent to the Plannotator Inbox asking
# 1 question (thread <id>, <url>)". Anchored to a line start and only searched in the result's
# top-level text, so a quote of it inside a message body (as a thread read returns) does not
# match. Code mode's REPL tool returns whatever its code returned, often only this prose.
_INBOX_PROSE_LINK = re.compile(
    r"^(?:Already )?[Ss]ent to the Plannotator Inbox\b[^()\n]*\(thread ([^\s,()]+), ([^\s()]+)\)",
    re.MULTILINE,
)


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


def _top_level_texts(tool_response: Any) -> list[str]:
    # The result's own text, not text nested in the data it returned: the response when it is
    # a string, the `text` of its top-level content blocks (as a list or under a top-level
    # `content` key), and the top-level string values of a result object (a REPL result).
    if isinstance(tool_response, str):
        return [tool_response]
    if isinstance(tool_response, dict):
        texts = [value for value in tool_response.values() if isinstance(value, str)]
        content = tool_response.get("content")
        if isinstance(content, list):
            texts.extend(_block_texts(content))
        return texts
    if isinstance(tool_response, list):
        return _block_texts(tool_response)
    return []


def _block_texts(blocks: list[Any]) -> list[str]:
    texts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            texts.append(block["text"])
    return texts


def _prose_links(tool_response: Any) -> list[InboxLink]:
    # Every send line in the result's top-level text, one per distinct thread id, in order.
    links: dict[str, InboxLink] = {}
    for text in _top_level_texts(tool_response):
        for match in _INBOX_PROSE_LINK.finditer(text):
            links.setdefault(match[1], InboxLink(thread_id=match[1], url=match[2]))
    return list(links.values())


def _payload(raw_payload: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_payload)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _is_repl_call(payload: dict[str, Any]) -> bool:
    return payload.get("tool_name") == REPL_TOOL_NAME


def _candidate_links(payload: dict[str, Any]) -> list[InboxLink]:
    # The Claude Code PostToolUse payload: {tool_name, tool_input, tool_response, ...}. Its
    # `tool_response` may be the structured result (a dict), a string, or a list of content blocks
    # (possibly under a `content` key) whose `text` is the Inbox's prose line followed by the
    # result as JSON. Returns the links a send result offers: the direct tool's JSON result is read
    # first, as one link; else the send lines, one per distinct thread id. A code mode REPL call's
    # result is only searched for send lines: its code may also have read or listed threads, whose
    # JSON carries other threads' ids and urls.
    tool_response = payload.get("tool_response")
    if not _is_repl_call(payload):
        json_link = _find_link(tool_response)
        if json_link is not None:
            return [json_link]
    return _prose_links(tool_response)


def _input_texts(value: Any, depth: int = 0) -> list[str]:
    # Every string in a tool input (for a REPL call, its code), bounded like the result search.
    if depth > _MAX_SEARCH_DEPTH:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, list):
        return [text for nested in value for text in _input_texts(nested, depth + 1)]
    return []


def _is_unrelated_repl_call(raw_payload: str) -> bool:
    # A code mode REPL call is about the Inbox only when its code names the Inbox tool (under
    # any accessor) or its result holds the Inbox's prose send line; any other REPL call is not
    # the hook's business.
    payload = _payload(raw_payload)
    if payload is None or not _is_repl_call(payload):
        return False
    names_inbox_tool = any(
        PLANNOTATOR_INBOX_TOOL_MARKER in text for text in _input_texts(payload.get("tool_input"))
    )
    return not names_inbox_tool and not _prose_links(payload.get("tool_response"))


def _names_own_thread(raw_payload: str, own_thread_name: str | None) -> bool:
    # Whether the send named the record's own Inbox thread: the direct tool's `thread` input,
    # or, for a REPL call (whose input is code), a `thread: "<name>"` argument in the code
    # naming it exactly (any of the three JS quotes). A `read_thread` call naming the own
    # thread in the same code also matches; accepted, since the result's send line still has
    # to be the only one for anything to be recorded.
    if own_thread_name is None:
        return False
    payload = _payload(raw_payload)
    if payload is None:
        return False
    tool_input = payload.get("tool_input")
    if _is_repl_call(payload):
        thread_argument = re.compile(rf"thread\s*:\s*([\"'`]){re.escape(own_thread_name)}\1")
        return any(thread_argument.search(text) for text in _input_texts(tool_input))
    thread_name = tool_input.get("thread") if isinstance(tool_input, dict) else None
    return thread_name == own_thread_name


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
    payload = _payload(raw_payload)
    if payload is None or payload.get("tool_name") not in (
        PLANNOTATOR_INBOX_TOOL_NAME,
        REPL_TOOL_NAME,
    ):
        tool_name = payload.get("tool_name") if payload is not None else None
        logger.info(f"inbox-link hook skipped tool={tool_name} thread={thread_ts}")
        return
    if _is_unrelated_repl_call(raw_payload):
        logger.debug(f"inbox-link hook skipped a REPL call with no Inbox use thread={thread_ts}")
        return
    links = _candidate_links(payload)
    if not links:
        logger.info(f"inbox-link hook found no thread link thread={thread_ts}")
        return
    record = thread_map.get(thread_ts, session_name=session_name)
    if record is None:
        logger.warning(f"inbox-link hook: no thread record thread={thread_ts}")
        return
    if len(links) > 1:
        thread_ids = ", ".join(link.thread_id for link in links)
        if record.inbox_thread_id in {link.thread_id for link in links}:
            logger.info(
                f"inbox-link hook kept the recorded link; the result names threads "
                f"{thread_ids} thread={thread_ts}"
            )
        else:
            logger.info(
                f"inbox-link hook recorded nothing; the result names more than one thread "
                f"({thread_ids}) thread={thread_ts}"
            )
        return
    (link,) = links
    if record.inbox_thread_id is not None and not _names_own_thread(
        raw_payload, record.inbox_thread_name
    ):
        logger.info(f"inbox-link hook kept the recorded link thread={thread_ts}")
        return
    thread_map.record_inbox_thread(
        thread_ts, link.thread_id, link.url, session_name=record.terminal_name
    )
    logger.info(f"inbox-link hook recorded the Inbox thread thread={thread_ts}")
