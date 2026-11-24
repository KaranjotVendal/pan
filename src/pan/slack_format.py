from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pan.config import (
    CODE_FENCE,
    SLACK_MESSAGE_PART_MAX_LENGTH,
    SLACK_MRKDWN_PLACEHOLDER_TEMPLATE,
    SLACK_TABLE_COLUMN_SEPARATOR,
    SLACK_TABLE_PAD_CHAR,
)

# Ordered protect/convert passes adapted from Hermes' `format_message`. Each is a fixed regex
# (INV-3 determinism); the table pass (added here, absent in Hermes) is a line scanner.
_FENCED_CODE = re.compile(r"(```(?:[^\n]*\n)?[\s\S]*?```)")
_INLINE_CODE = re.compile(r"(`[^`]+`)")
_MARKDOWN_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_SLACK_ENTITY = re.compile(r"(<(?:[@#!]|(?:https?|mailto|tel):)[^>\n]+>)")
_BLOCKQUOTE_MARKER = re.compile(r"^(>+\s)", re.MULTILINE)
_HEADER = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_HEADER_INNER_BOLD = re.compile(r"\*\*(.+?)\*\*")
# `__bold__`, only outside a word (as in Markdown), so `snake__case__name` stays as is.
_UNDERSCORE_BOLD = re.compile(r"(?<![\w_])__(?=\S)(.+?)(?<=\S)__(?![\w_])")
_BOLD_ITALIC = re.compile(r"\*\*\*(.+?)\*\*\*")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
# Single `*emphasis*`: the emphasized run must touch non-whitespace on both sides, so literal
# spaced delimiters like `a * b * c` are preserved (Hermes' guard, reused verbatim).
_ITALIC = re.compile(r"(?<!\*)\*(\S(?:[^*\n]*?\S)?)\*(?!\*)")
_STRIKETHROUGH = re.compile(r"~~(.+?)~~")
# A GFM delimiter row cell: optional alignment colons around one or more dashes.
_DELIMITER_CELL = re.compile(r":?-+:?")


def to_slack_mrkdwn(text: str) -> str:
    if not text:
        return text

    placeholders: dict[str, str] = {}
    counter = [0]

    def stash(value: str) -> str:
        key = SLACK_MRKDWN_PLACEHOLDER_TEMPLATE.format(index=counter[0])
        counter[0] += 1
        placeholders[key] = value
        return key

    def convert_link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2).strip()
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1].strip()
        return stash(f"<{url}|{label}>")

    def convert_header(match: re.Match[str]) -> str:
        inner = _HEADER_INNER_BOLD.sub(r"\1", match.group(1).strip())
        return stash(f"*{inner}*")

    # Protect regions that must survive every later pass byte-for-byte.
    text = _FENCED_CODE.sub(lambda match: stash(match.group(0)), text)
    text = _INLINE_CODE.sub(lambda match: stash(match.group(0)), text)
    text = _MARKDOWN_LINK.sub(convert_link, text)
    text = _SLACK_ENTITY.sub(lambda match: stash(match.group(1)), text)
    text = _BLOCKQUOTE_MARKER.sub(lambda match: stash(match.group(0)), text)

    # Escape Slack control characters in the remaining plain text; unescape first so
    # already-escaped input is not double-escaped.
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Emphasis passes: bold/italic/strikethrough map to their mrkdwn spellings, headers
    # degrade to a bold line. Each result is stashed so a later pass never re-splits it.
    text = _HEADER.sub(convert_header, text)
    text = _BOLD_ITALIC.sub(lambda match: stash(f"*_{match.group(1)}_*"), text)
    text = _BOLD.sub(lambda match: stash(f"*{match.group(1)}*"), text)
    text = _UNDERSCORE_BOLD.sub(lambda match: stash(f"*{match.group(1)}*"), text)
    text = _ITALIC.sub(lambda match: stash(f"_{match.group(1)}_"), text)
    text = _STRIKETHROUGH.sub(lambda match: stash(f"~{match.group(1)}~"), text)

    # Table pass (new vs Hermes): after escaping/emphasis so cells inherit the same escaping,
    # before restore so the generated fence is carried through verbatim.
    text = _convert_tables(text, stash)

    for key in reversed(placeholders):
        text = text.replace(key, placeholders[key])
    return text


def _split_row(line: str) -> list[str]:
    cells = [cell.strip() for cell in line.split("|")]
    if cells and cells[0] == "":
        cells = cells[1:]
    if cells and cells[-1] == "":
        cells = cells[:-1]
    return cells


def _is_pipe_row(line: str) -> bool:
    return "|" in line


def _is_delimiter_row(line: str) -> bool:
    cells = _split_row(line)
    return bool(cells) and all(_DELIMITER_CELL.fullmatch(cell) for cell in cells)


def _render_table(rows: list[list[str]]) -> str:
    column_count = max(len(row) for row in rows)
    widths = [0] * column_count
    for row in rows:
        for column, cell in enumerate(row):
            widths[column] = max(widths[column], len(cell))

    rendered_lines: list[str] = []
    for row in rows:
        padded_cells = [
            (row[column] if column < len(row) else "").ljust(widths[column], SLACK_TABLE_PAD_CHAR)
            for column in range(column_count)
        ]
        line = SLACK_TABLE_COLUMN_SEPARATOR.join(padded_cells).rstrip(SLACK_TABLE_PAD_CHAR)
        rendered_lines.append(line)
    return "\n".join(rendered_lines)


def _convert_tables(text: str, stash: Callable[[str], str]) -> str:
    lines = text.split("\n")
    output_lines: list[str] = []
    index = 0
    while index < len(lines):
        header_line = lines[index]
        # A GFM table requires the delimiter row to have the SAME number of cells as the
        # header — otherwise a prose line with a pipe followed by a `---` section divider
        # (single cell) would be mis-detected as a table, fencing the prose and dropping
        # the divider. The pipe check is first so _split_row only runs on candidate rows.
        is_table_header = (
            _is_pipe_row(header_line)
            and index + 1 < len(lines)
            and _is_delimiter_row(lines[index + 1])
            and len(_split_row(lines[index + 1])) == len(_split_row(header_line))
        )
        if is_table_header:
            body_end = index + 2
            while body_end < len(lines) and _is_pipe_row(lines[body_end]):
                body_end += 1
            row_lines = [header_line, *lines[index + 2 : body_end]]
            rendered = _render_table([_split_row(line) for line in row_lines])
            output_lines.append(stash(f"```\n{rendered}\n```"))
            index = body_end
        else:
            output_lines.append(header_line)
            index += 1
    return "\n".join(output_lines)


def split_for_slack(text: str, max_length: int = SLACK_MESSAGE_PART_MAX_LENGTH) -> list[str]:
    # Split a long message into parts of at most max_length characters, at line breaks where
    # possible (a line longer than a part is cut). A code fence open at a split is closed at
    # the end of its part and opened again at the start of the next, so every part renders.
    if len(text) <= max_length:
        return [text]
    # Room for a closing fence on every part, and for a reopening fence before a cut line.
    fence_line_length = len(CODE_FENCE) + 1
    budget = max(max_length - fence_line_length, 1)
    piece_length = max(budget - fence_line_length, 1)

    parts: list[str] = []
    current_lines: list[str] = []
    current_length = 0
    in_fence = False

    def flush() -> None:
        nonlocal current_lines, current_length
        body = "\n".join(current_lines)
        parts.append(f"{body}\n{CODE_FENCE}" if in_fence else body)
        current_lines = [CODE_FENCE] if in_fence else []
        current_length = len(CODE_FENCE) if in_fence else 0

    for line in text.split("\n"):
        pieces = [
            line[start : start + piece_length] for start in range(0, len(line), piece_length)
        ] or [""]
        for piece in pieces:
            if current_lines and current_length + 1 + len(piece) > budget:
                flush()
            current_length += len(piece) + (1 if current_lines else 0)
            current_lines.append(piece)
        if line.strip().startswith(CODE_FENCE):
            in_fence = not in_fence
    if current_lines:
        parts.append("\n".join(current_lines))
    return parts


# A local path in a public post: one under /Users, /home or ~. It must start a word: a `/`,
# `:`, `.`, `~`, `-` or word character before it means it is part of a URL (`https://x/home/a`,
# `file:///Users/a`) or a longer path, which is left alone. Slack entities (`<url|label>`,
# mentions, dates) are skipped whole.
_LOCAL_PATH = re.compile(r"(?<![\w/:.~-])(?:/Users/|/home/|~/)[^\s<>|`'\"()\[\]{}&]*")
_SLACK_ENTITY_SPAN = re.compile(r"<[^<>\n]*>")
_PATH_TRAILING_PUNCTUATION = ".,;:!?"
REDACTED_PATH_PREFIX = ".../"
# The block keys whose string values are shown to readers.
_BLOCK_TEXT_KEY = "text"


def _shortened_path(match: re.Match[str]) -> str:
    path = match.group(0)
    trimmed = path.rstrip(_PATH_TRAILING_PUNCTUATION)
    trailing = path[len(trimmed) :]
    last_part = trimmed.rstrip("/").rsplit("/", 1)[-1]
    if last_part in ("", "~"):
        return f"...{trailing}"
    return f"{REDACTED_PATH_PREFIX}{last_part}{trailing}"


def redact_paths(text: str) -> str:
    # Each local path cut to its last part (`/Users/me/dev/pan` -> `.../pan`), outside Slack
    # entities only.
    pieces: list[str] = []
    position = 0
    for entity in _SLACK_ENTITY_SPAN.finditer(text):
        pieces.append(_LOCAL_PATH.sub(_shortened_path, text[position : entity.start()]))
        pieces.append(entity.group(0))
        position = entity.end()
    pieces.append(_LOCAL_PATH.sub(_shortened_path, text[position:]))
    return "".join(pieces)


def redact_block_paths(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # redact_paths on every `text` string in the blocks, at any depth; a copy is returned.
    return [_redact_block_value(block) for block in blocks]


def _redact_block_value(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            entry_key: _redact_block_value(entry_value, entry_key)
            for entry_key, entry_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_block_value(entry) for entry in value]
    if isinstance(value, str) and key == _BLOCK_TEXT_KEY:
        return redact_paths(value)
    return value
