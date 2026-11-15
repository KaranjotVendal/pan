from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Claude Code writes each session as JSONL: one entry per line, `type` "user" or
# "assistant", the text under `message.content` (a string, or a list of blocks of which
# only `text` blocks are prose). The exact shape is version-dependent (live-verify).
_SPEAKERS = ("user", "assistant")


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


def _turns(transcript_path: Path) -> Iterator[tuple[str, str]]:
    for raw_line in transcript_path.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        turn = _entry_texts(entry)
        if turn is not None:
            yield turn


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


def render_transcript(transcript_path: Path) -> str:
    # The conversation as plain text, one `speaker: text` block per turn, tool calls and
    # tool results left out. Raises OSError when the file cannot be read.
    return "\n\n".join(f"{speaker}: {text}" for speaker, text in _turns(transcript_path))
