from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Claude Code writes each session as JSONL: one entry per line, `type` "user" or
# "assistant", the text under `message.content` (a string, or a list of blocks of which
# only `text` blocks are prose). The exact shape is version-dependent (live-verify).
_SPEAKERS = ("user", "assistant")
# User entries Claude Code writes itself rather than the person: slash-command echoes and
# their output, injected reminders, interrupt markers. Their text starts with one of these.
_MACHINE_USER_TEXT_PREFIXES = (
    "<command-",
    "<local-command-",
    "<system-reminder>",
    "<bash-",
    "<task-notification>",
    "Caveat:",
    "[Request interrupted",
)


def _entry_texts(entry: Any) -> tuple[str, str] | None:
    if not isinstance(entry, dict) or entry.get("type") not in _SPEAKERS:
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        texts = [content] if content.strip() else []
    elif isinstance(content, list):
        texts = [
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ]
    else:
        texts = []
    if not texts:
        return None
    return str(entry["type"]), "\n".join(texts)


def _entries(transcript_path: Path) -> Iterator[Any]:
    for raw_line in transcript_path.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            yield json.loads(stripped)
        except json.JSONDecodeError:
            continue


def _turns(transcript_path: Path) -> Iterator[tuple[str, str]]:
    for entry in _entries(transcript_path):
        turn = _entry_texts(entry)
        if turn is not None:
            yield turn


def _is_person_text(entry: dict[str, Any], text: str) -> bool:
    # A user entry the person wrote: not a meta entry, not a subagent's (sidechain) prompt,
    # not text Claude Code put there itself. Tool results carry no text block, so
    # _entry_texts already skips them.
    if entry.get("isMeta") or entry.get("isSidechain"):
        return False
    return not text.lstrip().startswith(_MACHINE_USER_TEXT_PREFIXES)


def last_assistant_text(transcript_path: Path) -> str | None:
    # The worker's final summary: the last assistant text in its transcript, or None when
    # the file is unreadable or holds no assistant text.
    try:
        assistant_texts = [
            text for speaker, text in _turns(transcript_path) if speaker == "assistant"
        ]
    except OSError:
        return None
    return assistant_texts[-1] if assistant_texts else None


def last_user_text(transcript_path: Path) -> str | None:
    # The last thing the person asked the session: the last user text they wrote, or None
    # when the file is unreadable or holds none.
    last_text: str | None = None
    try:
        for entry in _entries(transcript_path):
            turn = _entry_texts(entry)
            if turn is None or turn[0] != "user":
                continue
            if _is_person_text(entry, turn[1]):
                last_text = turn[1]
    except OSError:
        return None
    return last_text


def render_transcript(transcript_path: Path) -> str:
    # The conversation as plain text, one `speaker: text` block per turn, tool calls and
    # tool results left out. Raises OSError when the file cannot be read.
    return "\n\n".join(f"{speaker}: {text}" for speaker, text in _turns(transcript_path))
