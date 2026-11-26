from __future__ import annotations

from pathlib import Path

import pytest

from pan.data.workset import goal_from_text, read_workset_goal


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "# Stream: exports\n\n## Goal\nShip the CSV export.\n\n## Current task\nTests\n",
            "Ship the CSV export.",
        ),
        ("## goal\n\n\n  Ship the   CSV export.  \n", "Ship the CSV export."),
        ("Goal: ship the CSV export\nmore\n", "ship the CSV export"),
        ("GOALS:\nShip the CSV export.\n", "Ship the CSV export."),
        ("# Exports\n\n## Current task\nTests\n", None),
        ("Goal-setting notes\nnot a goal heading\n", None),
        ("## Goal\n\n## Current task\nTests\n", None),
        ("## Goal\n", None),
        ("**Goal:** ship the CSV export\n", "ship the CSV export"),
        ("- Goal: ship the CSV export\n", "ship the CSV export"),
        ("## Goal (v2)\nShip the CSV export.\n", "Ship the CSV export."),
        ("__Goal__\n- **Ship the CSV export.**\n", "Ship the CSV export."),
        ("* Goal: _ship the CSV export_\n", "ship the CSV export"),
        ("1. Goal:\n2. Ship the CSV export.\n", "Ship the CSV export."),
    ],
    ids=[
        "heading",
        "lowercase-heading-spaces-collapsed",
        "inline",
        "plural-with-colon",
        "no-goal-line",
        "prose-starting-with-the-word",
        "heading-then-another-heading",
        "heading-at-the-end",
        "bold-label",
        "list-item",
        "heading-with-a-note",
        "goal-line-bullet-and-emphasis-stripped",
        "starred-bullet-italic-goal",
        "numbered-list",
    ],
)
def test_goal_from_text(text: str, expected: str | None) -> None:
    assert goal_from_text(text) == expected


def test_a_long_goal_is_cut_to_one_short_line() -> None:
    goal = goal_from_text("## Goal\n" + "word " * 60)

    assert goal is not None
    assert len(goal) == 120
    assert goal.endswith("...")


@pytest.mark.parametrize(
    "content, expected",
    [
        ("## Goal\nShip the CSV export.\n", "Ship the CSV export."),
        ("## Notes\nnothing here\n", None),
        (None, None),
    ],
    ids=["present", "no-goal-line", "absent"],
)
def test_read_workset_goal(tmp_path: Path, content: str | None, expected: str | None) -> None:
    workset = tmp_path / "workset.md"
    if content is not None:
        workset.write_text(content)

    assert read_workset_goal(workset) == expected
