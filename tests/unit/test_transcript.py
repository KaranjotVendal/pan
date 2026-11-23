from __future__ import annotations

import json
from pathlib import Path

import pytest

from pan.transcript import last_assistant_text, last_user_text, render_transcript


def _write_transcript(path: Path, entries: list[object]) -> Path:
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\nnot json\n\n")
    return path


def _assistant(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def test_render_transcript_keeps_user_and_assistant_prose_in_order(tmp_path: Path) -> None:
    transcript = _write_transcript(
        tmp_path / "s.jsonl",
        [
            {"type": "user", "message": {"content": "fix the login bug"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Looking."},
                        {"type": "tool_use", "name": "Read", "input": {}},
                    ]
                },
            },
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
            {"type": "system", "message": {"content": "ignored"}},
            _assistant("Fixed it."),
        ],
    )

    assert render_transcript(transcript) == (
        "user: fix the login bug\n\nassistant: Looking.\n\nassistant: Fixed it."
    )


def test_render_transcript_raises_for_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        render_transcript(tmp_path / "absent.jsonl")


@pytest.mark.parametrize(
    "entries, expected",
    [
        (
            [
                _assistant("first"),
                {"type": "user", "message": {"content": []}},
                _assistant("final"),
            ],
            "final",
        ),
        ([{"type": "user", "message": {"content": "only a question"}}], None),
    ],
)
def test_last_assistant_text(tmp_path: Path, entries: list[object], expected: str | None) -> None:
    assert last_assistant_text(_write_transcript(tmp_path / "s.jsonl", entries)) == expected


def test_last_assistant_text_is_none_for_a_missing_file(tmp_path: Path) -> None:
    assert last_assistant_text(tmp_path / "absent.jsonl") is None


def _user(content: object, **extra: object) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


@pytest.mark.parametrize(
    "entries, expected",
    [
        (
            [_user("fix the login bug"), _assistant("Done."), _user("now add tests")],
            "now add tests",
        ),
        (
            [
                _user("fix the login bug"),
                _user([{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]),
                _assistant("Done."),
            ],
            "fix the login bug",
        ),
        (
            [_user([{"type": "text", "text": "use the blocks form"}]), _assistant("ok")],
            "use the blocks form",
        ),
        ([_user("the ask"), _user("injected context", isMeta=True)], "the ask"),
        ([_user("the ask"), _user("subagent prompt", isSidechain=True)], "the ask"),
        (
            [
                _user("the ask"),
                _user("<command-name>/clear</command-name>"),
                _user("<local-command-stdout>cleared</local-command-stdout>"),
                _user("[Request interrupted by user]"),
            ],
            "the ask",
        ),
        ([_assistant("hello")], None),
    ],
    ids=[
        "latest-wins",
        "tool-result-skipped",
        "text-blocks",
        "meta-skipped",
        "sidechain-skipped",
        "machine-text-skipped",
        "no-user-text",
    ],
)
def test_last_user_text(tmp_path: Path, entries: list[object], expected: str | None) -> None:
    assert last_user_text(_write_transcript(tmp_path / "s.jsonl", entries)) == expected


def test_last_user_text_is_none_for_a_missing_file(tmp_path: Path) -> None:
    assert last_user_text(tmp_path / "absent.jsonl") is None
