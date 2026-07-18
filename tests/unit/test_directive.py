from __future__ import annotations

import pytest

from pan.directive import parse_directive
from pan.models import Agent, TaskMode


@pytest.mark.parametrize(
    "raw_text, expected_mode, expected_cleaned",
    [
        ("--sync deploy the service", TaskMode.SYNC, "deploy the service"),
        ("!deploy the service", TaskMode.SYNC, "deploy the service"),
        ("  !fix the bug  ", TaskMode.SYNC, "fix the bug"),
        ("--status", TaskMode.STATUS, ""),
        ("just build the feature", TaskMode.DELEGATE, "just build the feature"),
        ("", TaskMode.DELEGATE, ""),
    ],
)
def test_mode_resolution(raw_text: str, expected_mode: TaskMode, expected_cleaned: str) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is expected_mode
    assert directive.cleaned_text == expected_cleaned


def test_no_flags_is_delegate_with_no_extras() -> None:
    directive = parse_directive("build the thing")

    assert directive.mode is TaskMode.DELEGATE
    assert directive.force_new is False
    assert directive.target_stream is None
    assert directive.agent is None
    assert directive.cleaned_text == "build the thing"


def test_new_flag_sets_force_new_and_is_stripped() -> None:
    directive = parse_directive("--new build the feature")

    assert directive.force_new is True
    assert directive.mode is TaskMode.DELEGATE
    assert directive.cleaned_text == "build the feature"


def test_stream_flag_captures_name_and_is_stripped() -> None:
    directive = parse_directive("--stream backend-work do the migration")

    assert directive.target_stream == "backend-work"
    assert directive.cleaned_text == "do the migration"


@pytest.mark.parametrize(
    "raw_text, expected_agent",
    [
        ("--agent codex refactor this", Agent.CODEX),
        ("--agent claude refactor this", Agent.CLAUDE),
        ("--agent pi refactor this", Agent.PI),
    ],
)
def test_agent_flag_parsed_and_reserved(raw_text: str, expected_agent: Agent) -> None:
    directive = parse_directive(raw_text)

    assert directive.agent is expected_agent
    assert directive.cleaned_text == "refactor this"


def test_flag_combination_and_precedence() -> None:
    directive = parse_directive("--sync --new --stream feat-x ship the release")

    assert directive.mode is TaskMode.SYNC
    assert directive.force_new is True
    assert directive.target_stream == "feat-x"
    assert directive.cleaned_text == "ship the release"


def test_flags_recognized_mid_text_and_prose_preserved() -> None:
    directive = parse_directive("update the readme --new then run tests")

    assert directive.force_new is True
    assert directive.cleaned_text == "update the readme then run tests"


def test_leading_bang_combines_with_other_flags() -> None:
    directive = parse_directive("! --new --stream q ship it")

    assert directive.mode is TaskMode.SYNC
    assert directive.force_new is True
    assert directive.target_stream == "q"
    assert directive.cleaned_text == "ship it"


def test_status_takes_precedence_over_sync() -> None:
    directive = parse_directive("--sync --status")

    assert directive.mode is TaskMode.STATUS
    assert directive.cleaned_text == ""


def test_bang_only_as_leading_shorthand_not_mid_text() -> None:
    directive = parse_directive("run the script with args a!b")

    assert directive.mode is TaskMode.DELEGATE
    assert directive.cleaned_text == "run the script with args a!b"


def test_trailing_stream_without_value_is_stripped() -> None:
    directive = parse_directive("do the migration --stream")

    assert directive.target_stream is None
    assert directive.cleaned_text == "do the migration"


def test_trailing_agent_without_value_is_stripped() -> None:
    directive = parse_directive("refactor this --agent")

    assert directive.agent is None
    assert directive.cleaned_text == "refactor this"


def test_agent_with_unknown_value_leaves_agent_none_and_drops_value() -> None:
    directive = parse_directive("--agent gpt refactor this")

    assert directive.agent is None
    assert directive.cleaned_text == "refactor this"


def test_value_flag_does_not_swallow_a_following_flag() -> None:
    directive = parse_directive("--stream --new build the thing")

    assert directive.target_stream is None
    assert directive.force_new is True
    assert directive.cleaned_text == "build the thing"
