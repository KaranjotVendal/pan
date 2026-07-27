from __future__ import annotations

import pytest

from pan.config import (
    HELP_ECHOED_COMMAND_MAX_LENGTH,
    HELP_USER_FACING_COMMANDS,
    SLACK_DIRECTIVE_FLAGS_FOOTER,
)
from pan.help import HelpRequest, parse_help_request, render_cli_help

_INFRA_COMMANDS = ("gateway", "watcher", "hook", "inbox", "threads", "spawn", "slack-post")
# Rich's box-drawing frame is exactly what must NOT reach a Slack code block.
_BOX_DRAWING = ("╭", "╮", "│", "╰", "╯", "─")


@pytest.mark.parametrize(
    "raw_text",
    [
        "help",
        "--help",
        "-h",
        "?",
        "  help  ",
        "HELP",
        "—help",
        "–help",
        "<@B0T> help",
        "<@B0T> --help",
        "<@B0T> -h",
        "<@B0T> ?",
        "<@B0T> —help",
    ],
    ids=[
        "bare-help",
        "double-dash",
        "short-flag",
        "question-mark",
        "surrounding-whitespace",
        "uppercase",
        "em-dash-help",
        "en-dash-help",
        "mention-help",
        "mention-double-dash",
        "mention-short-flag",
        "mention-question-mark",
        "mention-em-dash-help",
    ],
)
def test_top_level_help_spellings_are_detected(raw_text: str) -> None:
    assert parse_help_request(raw_text) == HelpRequest(command=None)


@pytest.mark.parametrize(
    "raw_text",
    [
        "help relay",
        "--help relay",
        "-h relay",
        "? relay",
        "relay --help",
        "relay -h",
        "<@B0T> help relay",
        "<@B0T> —help relay",
        "<@B0T> relay --help",
    ],
    ids=[
        "help-then-command",
        "double-dash-then-command",
        "short-flag-then-command",
        "question-mark-then-command",
        "command-then-double-dash",
        "command-then-short-flag",
        "mention-help-then-command",
        "mention-em-dash-then-command",
        "mention-command-then-double-dash",
    ],
)
def test_named_command_help_is_detected(raw_text: str) -> None:
    assert parse_help_request(raw_text) == HelpRequest(command="relay")


@pytest.mark.parametrize(
    "raw_text",
    [
        "",
        "   ",
        "<@B0T>",
        "fix the login bug",
        "<@B0T> fix the login bug",
        "<@B0T> fix the login bug and add a --help flag to the tool",
        "<@B0T> relay sra ship it",
        "<@B0T> read sra --full",
        "<@B0T> sessions",
        "<@B0T> --sessions",
        "<@B0T> is this broken?",
        "<@B0T> helpful refactor",
        "<@B0T> deploy ?",
        "<@B0T> retry help",
        "<@B0T> --status ?",
    ],
    ids=[
        "empty",
        "whitespace-only",
        "mention-only",
        "plain-prose",
        "mention-plain-prose",
        "task-brief-containing-help-flag",
        "relay-directive",
        "read-directive",
        "bare-sessions-directive",
        "sessions-flag-directive",
        "trailing-question-mark-prose",
        "help-as-word-prefix",
        "two-word-task-ending-in-question-mark",
        "two-word-task-ending-in-help",
        "status-directive-with-question-mark",
    ],
)
def test_non_help_text_is_not_hijacked(raw_text: str) -> None:
    assert parse_help_request(raw_text) is None


def test_top_level_help_lists_user_facing_commands_with_footer() -> None:
    rendered = render_cli_help()

    assert rendered.rstrip().endswith(SLACK_DIRECTIVE_FLAGS_FOOTER.rstrip())
    # Assert the command names against the generated LISTING only: the hand-written footer
    # mentions several of them too, which would otherwise satisfy the assertion by itself.
    listing = rendered.split(SLACK_DIRECTIVE_FLAGS_FOOTER)[0]
    assert "Usage: pan" in listing
    for command_name in HELP_USER_FACING_COMMANDS:
        assert command_name in listing
    for infra_command in _INFRA_COMMANDS:
        assert infra_command not in listing


@pytest.mark.parametrize("command_path", [None, "relay", "read", "nonesuch"])
def test_rendered_help_is_plain_text(command_path: str | None) -> None:
    rendered = render_cli_help(command_path)

    # A positive anchor first: with Rich left enabled, get_help() returns "" (Rich prints to a
    # console instead), and the absence assertions below would pass on an empty string.
    assert "Usage: pan" in rendered
    assert "Show this message and exit" in rendered
    assert "\x1b" not in rendered
    for box_character in _BOX_DRAWING:
        assert box_character not in rendered


def test_named_command_help_renders_its_own_usage_and_flags() -> None:
    rendered = render_cli_help("relay")

    assert "Usage: pan relay" in rendered
    # The info_name fix: a bare command name as parent doubles the usage line.
    assert "pan pan relay" not in rendered
    assert "target" in rendered
    assert "message" in rendered
    assert "--help" in rendered
    # A named command's help carries no directive footer (top-level only).
    assert SLACK_DIRECTIVE_FLAGS_FOOTER.strip() not in rendered


def test_named_command_help_is_generated_from_the_live_app() -> None:
    # The real `--full` option help string in cli.read — proving the text is
    # introspected from the Typer app, not a hand-written literal.
    assert "full transcript via morcli instead of recent" in render_cli_help("read")


@pytest.mark.parametrize(
    "command_path, expected_notice",
    [
        ("nonesuch", "unknown command: nonesuch"),
        ("--json", "unknown command: --json"),
        ("relay read", "unknown command: relayread"),
        ("", "unknown command"),
    ],
    ids=["typo", "flag-not-a-command", "two-words", "empty"],
)
def test_unknown_command_falls_back_to_top_level_help(
    command_path: str, expected_notice: str
) -> None:
    rendered = render_cli_help(command_path)

    assert rendered.startswith(expected_notice)
    assert "Usage: pan" in rendered
    assert "Kill switch" in rendered  # the real `stop` description, i.e. the full listing


@pytest.mark.parametrize(
    "command_path, unwanted",
    [
        ("```", "`"),
        ("re```lay", "`"),
        ("<!channel>", "<"),
        ("<@U0123>", "@"),
        ("nonesuch​", "​"),
    ],
    ids=["bare-fence", "embedded-fence", "broadcast-entity", "user-entity", "zero-width"],
)
def test_unknown_command_notice_strips_unsafe_characters(command_path: str, unwanted: str) -> None:
    # The notice echoes a Slack-supplied token, and the gateway wraps the result in a code
    # fence: a backtick run would break out of it and a Slack entity survives a fence
    # unescaped (it would ping the channel).
    rendered = render_cli_help(command_path)

    notice_line = rendered.splitlines()[0]
    assert notice_line.startswith("unknown command")
    assert unwanted not in notice_line
    assert "Usage: pan" in rendered


def test_unknown_command_notice_is_length_bounded() -> None:
    rendered = render_cli_help("x" * 50_000)

    notice_line = rendered.splitlines()[0]
    assert len(notice_line) <= len("unknown command: ") + HELP_ECHOED_COMMAND_MAX_LENGTH
    assert len(rendered) < len(render_cli_help()) + 200


def test_render_does_not_mutate_the_shared_app() -> None:
    render_cli_help()

    # The filtered top-level listing must not leak into a later full render, and
    # the CLI's own --help must be unaffected.
    assert "gateway" in render_cli_help("gateway")
    assert "Usage: pan gateway" in render_cli_help("gateway")


def test_directive_footer_documents_the_slack_only_grammar() -> None:
    # These spellings live in parse_directive, never in Typer, so the generator
    # cannot see them (R-1) — the footer is the only place they are documented.
    for spelling in ("!", "--sync", "--status", "--sessions", "--new", "--stream"):
        assert spelling in SLACK_DIRECTIVE_FLAGS_FOOTER
