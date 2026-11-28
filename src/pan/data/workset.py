from __future__ import annotations

import re
from pathlib import Path

from pan.utils.config import WORKSET_GOAL_MAX_LENGTH
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# A Markdown heading's hashes, then a list bullet ("-", "*", "+", "1.", "1)").
_LINE_MARKERS = re.compile(r"^\s*(?:#+\s*)?(?:(?:[-*+]|\d+[.)])\s+)?")
# A Goal line, once its markers are gone: "Goal", "Goals", "**Goal:** ship it", "__Goal__:",
# "Goal (v2)" (any case). The group is the goal when it follows a colon on the same line;
# prose that merely starts with the word ("Goal-setting is ...") is not a Goal line.
_GOAL_LINE = re.compile(
    r"^[*_]*goals?[*_]*(?:\s*\([^)]*\))?[*_]*\s*(?::[*_]*\s*(.*))?$", re.IGNORECASE
)
_HEADING_PREFIX = "#"
_EMPHASIS_CHARACTERS = "*_ "


def _without_markers(line: str) -> str:
    return _LINE_MARKERS.sub("", line, count=1).strip()


def _goal_heading_text(line: str) -> str | None:
    # The text after a Goal line: "" when the goal follows on a later line, None when the line
    # is not a Goal line.
    match = _GOAL_LINE.match(_without_markers(line))
    if match is None:
        return None
    return (match.group(1) or "").strip()


def _goal_text(line: str) -> str:
    # A goal without the list bullet or emphasis around it ("- **Ship it**" is "Ship it").
    return _without_markers(line).strip(_EMPHASIS_CHARACTERS)


def _shortened(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= WORKSET_GOAL_MAX_LENGTH:
        return one_line
    return one_line[: WORKSET_GOAL_MAX_LENGTH - 3].rstrip() + "..."


def goal_from_text(text: str) -> str | None:
    # The first non-empty line after the first Goal line (or the text on the Goal line itself,
    # as in "Goal: ship it"), on one short line. None when there is no Goal line, or when
    # nothing but another heading follows it.
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        inline_goal = _goal_heading_text(line)
        if inline_goal is None:
            continue
        if inline_goal:
            return _shortened(_goal_text(inline_goal)) or None
        following = next((later for later in lines[idx + 1 :] if later.strip()), None)
        if following is None or following.lstrip().startswith(_HEADING_PREFIX):
            return None
        return _shortened(_goal_text(following)) or None
    return None


def read_workset_goal(path: Path) -> str | None:
    # The goal of the workset file at `path`; None when it is missing, unreadable or has no
    # Goal line.
    try:
        text = path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    goal = goal_from_text(text)
    logger.debug(f"workset read path={path} has_goal={goal is not None}")
    return goal
