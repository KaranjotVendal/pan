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


@pytest.mark.parametrize(
    "raw_text, expected_cleaned",
    [
        ("--sessions", ""),
        ("--sessions right now", "right now"),
        ("what's running", "what's running"),
        ("whats running?", "whats running?"),
        ("list sessions", "list sessions"),
        ("list threads", "list threads"),
        ("can you list all the threads?", "can you list all the threads?"),
    ],
)
def test_sessions_flag_and_soft_triggers_yield_sessions_mode(
    raw_text: str, expected_cleaned: str
) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.SESSIONS
    assert directive.cleaned_text == expected_cleaned


def test_sessions_flag_combines_with_other_flags_and_is_stripped() -> None:
    directive = parse_directive("--sessions --new tidy up")

    assert directive.mode is TaskMode.SESSIONS
    assert directive.force_new is True
    assert directive.cleaned_text == "tidy up"


def test_sessions_flag_takes_precedence_over_status() -> None:
    # Both are no-worker report modes; the broader "list all" sessions view wins.
    directive = parse_directive("--status --sessions")

    assert directive.mode is TaskMode.SESSIONS
    assert directive.cleaned_text == ""


def test_explicit_status_flag_beats_a_soft_sessions_trigger() -> None:
    # An explicit mode flag is authoritative over the convenience soft trigger.
    directive = parse_directive("--status list threads")

    assert directive.mode is TaskMode.STATUS


def test_ordinary_task_prose_does_not_trigger_sessions() -> None:
    directive = parse_directive("build the dashboard that shows running jobs")

    assert directive.mode is TaskMode.DELEGATE


def test_relay_verb_sets_target_and_verbatim_message() -> None:
    directive = parse_directive("relay sra-codex re-run the failing test")

    assert directive.mode is TaskMode.RELAY
    assert directive.target == "sra-codex"
    assert directive.message == "re-run the failing test"
    assert directive.cleaned_text == "re-run the failing test"


def test_relay_message_preserves_flag_looking_tokens() -> None:
    # Everything after the target is the message verbatim — flag scanning must not run,
    # so "--json" inside the brief is kept, not stripped.
    directive = parse_directive("relay sra-codex add the --json path")

    assert directive.mode is TaskMode.RELAY
    assert directive.target == "sra-codex"
    assert directive.message == "add the --json path"


def test_relay_with_leading_bang_still_parses() -> None:
    directive = parse_directive("!relay sra-codex ship it")

    assert directive.mode is TaskMode.RELAY
    assert directive.target == "sra-codex"
    assert directive.message == "ship it"


def test_relay_with_no_target_leaves_target_none() -> None:
    directive = parse_directive("relay")

    assert directive.mode is TaskMode.RELAY
    assert directive.target is None
    assert directive.message == ""


def test_non_leading_relay_verb_stays_delegate() -> None:
    directive = parse_directive("please relay this to the team")

    assert directive.mode is TaskMode.DELEGATE
    assert directive.target is None


def test_read_verb_sets_target_and_defaults_full_false() -> None:
    directive = parse_directive("read sra-codex")

    assert directive.mode is TaskMode.READ
    assert directive.target == "sra-codex"
    assert directive.full is False
    assert directive.cleaned_text == ""
    assert directive.message is None


def test_read_verb_with_full_flag_sets_full() -> None:
    directive = parse_directive("read sra-codex --full")

    assert directive.mode is TaskMode.READ
    assert directive.target == "sra-codex"
    assert directive.full is True


def test_read_with_leading_bang_still_parses() -> None:
    directive = parse_directive("!read sra-codex --full")

    assert directive.mode is TaskMode.READ
    assert directive.target == "sra-codex"
    assert directive.full is True


def test_read_with_no_target_leaves_target_none() -> None:
    directive = parse_directive("read")

    assert directive.mode is TaskMode.READ
    assert directive.target is None
    assert directive.full is False


def test_non_leading_read_verb_stays_delegate() -> None:
    directive = parse_directive("please read the logs and report")

    assert directive.mode is TaskMode.DELEGATE
    assert directive.target is None


@pytest.mark.parametrize(
    "raw_text, expected_target, expected_message",
    [
        (
            "<@U0BHY6GH48L> relay pan-test-target hello there",
            "pan-test-target",
            "hello there",
        ),
        (
            "<@U0BHY6GH48L>relay pan-test-target hello there",
            "pan-test-target",
            "hello there",
        ),
    ],
)
def test_leading_mention_stripped_before_relay_verb(
    raw_text: str, expected_target: str, expected_message: str
) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.RELAY
    assert directive.target == expected_target
    assert directive.message == expected_message
    assert "<@" not in (directive.message or "")
    assert "<@" not in directive.cleaned_text


@pytest.mark.parametrize(
    "raw_text, expected_full",
    [
        ("<@U0BHY6GH48L> read pan-test-target", False),
        ("<@U0BHY6GH48L> read pan-test-target --full", True),
    ],
)
def test_leading_mention_stripped_before_read_verb(raw_text: str, expected_full: bool) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.READ
    assert directive.target == "pan-test-target"
    assert directive.full is expected_full


def test_leading_mention_stripped_before_sessions_flag() -> None:
    directive = parse_directive("<@U0BHY6GH48L> --sessions")

    assert directive.mode is TaskMode.SESSIONS


def test_leading_mention_stripped_on_delegate_and_absent_from_cleaned_text() -> None:
    directive = parse_directive("<@U0BHY6GH48L> create a file called notes.txt")

    assert directive.mode is TaskMode.DELEGATE
    assert directive.cleaned_text == "create a file called notes.txt"
    assert "<@" not in directive.cleaned_text


def test_only_leading_mention_stripped_not_one_in_message_body() -> None:
    directive = parse_directive("<@U0BHY6GH48L> relay pan-test-target tell <@U9999FOO> hi")

    assert directive.mode is TaskMode.RELAY
    assert directive.target == "pan-test-target"
    assert directive.message == "tell <@U9999FOO> hi"
