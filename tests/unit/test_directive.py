from __future__ import annotations

import pytest

from pan.directive import X_TO_ROUTE_NOTICE, parse_directive, parse_route, wants_slack_reply
from pan.models import Agent, Route, RouteKind, TaskMode, ThreadSource


@pytest.mark.parametrize(
    "raw_text, expected_mode, expected_cleaned",
    [
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
    assert directive.target_stream is None
    assert directive.agent is None
    assert directive.cleaned_text == "build the thing"


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
    directive = parse_directive("--stream --agent codex build the thing")

    assert directive.target_stream is None
    assert directive.agent is Agent.CODEX
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
    directive = parse_directive("--sessions --stream q tidy up")

    assert directive.mode is TaskMode.SESSIONS
    assert directive.target_stream == "q"
    assert directive.cleaned_text == "tidy up"


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
            "<@U0TESTBOT1> relay pan-test-target hello there",
            "pan-test-target",
            "hello there",
        ),
        (
            "<@U0TESTBOT1>relay pan-test-target hello there",
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
        ("<@U0TESTBOT1> read pan-test-target", False),
        ("<@U0TESTBOT1> read pan-test-target --full", True),
    ],
)
def test_leading_mention_stripped_before_read_verb(raw_text: str, expected_full: bool) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.READ
    assert directive.target == "pan-test-target"
    assert directive.full is expected_full


def test_leading_mention_stripped_before_sessions_flag() -> None:
    directive = parse_directive("<@U0TESTBOT1> --sessions")

    assert directive.mode is TaskMode.SESSIONS


def test_leading_mention_stripped_on_delegate_and_absent_from_cleaned_text() -> None:
    directive = parse_directive("<@U0TESTBOT1> create a file called notes.txt")

    assert directive.mode is TaskMode.DELEGATE
    assert directive.cleaned_text == "create a file called notes.txt"
    assert "<@" not in directive.cleaned_text


def test_only_leading_mention_stripped_not_one_in_message_body() -> None:
    directive = parse_directive("<@U0TESTBOT1> relay pan-test-target tell <@U9999FOO> hi")

    assert directive.mode is TaskMode.RELAY
    assert directive.target == "pan-test-target"
    assert directive.message == "tell <@U9999FOO> hi"


# --- Task 31: mobile smart-punctuation normalization + bare `sessions` trigger ---

# Phone autocorrect turns a typed `--` into an em/en-dash glued to the next word.
_EM_DASH = "—"
_EN_DASH = "–"


@pytest.mark.parametrize(
    "raw_text",
    [
        "--sessions",
        f"{_EM_DASH}sessions",  # phone autocorrected -- into an em-dash
        f"{_EN_DASH}sessions",  # en-dash variant
        "sessions",  # bare leading word
    ],
)
def test_all_sessions_spellings_route_to_sessions_mode(raw_text: str) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.SESSIONS
    assert directive.cleaned_text == ""


@pytest.mark.parametrize(
    "raw_text",
    [
        "<@U0TESTBOT1> --sessions",
        f"<@U0TESTBOT1> {_EM_DASH}sessions",
        f"<@U0TESTBOT1> {_EN_DASH}sessions",
        "<@U0TESTBOT1> sessions",
    ],
)
def test_sessions_spellings_route_after_a_leading_mention(raw_text: str) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.SESSIONS


def test_bare_sessions_word_mid_prose_stays_delegate() -> None:
    # The trigger fires only in position 0 (mirrors the leading relay/read grammar), so a
    # `sessions` deeper in a real task is ordinary prose.
    directive = parse_directive("build the sessions dashboard")

    assert directive.mode is TaskMode.DELEGATE
    assert directive.cleaned_text == "build the sessions dashboard"


@pytest.mark.parametrize(
    "raw_text, expected_full",
    [
        ("read sra-codex --full", True),
        (f"read sra-codex {_EM_DASH}full", True),
    ],
)
def test_read_full_modifier_survives_punctuation_normalization(
    raw_text: str, expected_full: bool
) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.READ
    assert directive.target == "sra-codex"
    assert directive.full is expected_full


def test_relay_verb_still_parses_after_normalization() -> None:
    directive = parse_directive("relay sra-codex ship it")

    assert directive.mode is TaskMode.RELAY
    assert directive.target == "sra-codex"
    assert directive.message == "ship it"


def test_spaced_prose_em_dash_is_not_normalized_and_stays_delegate() -> None:
    # An em-dash with whitespace around it is real prose punctuation, not a mangled `--`,
    # so it is left intact and the directive stays DELEGATE.
    raw_text = f"fix the plan {_EM_DASH} then ship"
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.DELEGATE
    assert directive.cleaned_text == raw_text


def test_glued_em_dash_inside_relay_message_is_normalized_r6() -> None:
    # R-6 documented tradeoff: whole-text normalization repairs an em-dash glued to a word
    # even inside a relay message body. Accepted — the phone-autocorrect case is the target.
    directive = parse_directive(f"relay sra-codex fix{_EM_DASH}now")

    assert directive.mode is TaskMode.RELAY
    assert directive.target == "sra-codex"
    assert directive.message == "fix--now"


def test_spaced_em_dash_inside_relay_message_is_preserved_verbatim() -> None:
    # The counterpart to the R-6 glued case: a real spaced em-dash in a relay brief is prose
    # punctuation and survives verbatim (the glued-to-word rule does not fire).
    directive = parse_directive(f"relay sra-codex fix {_EM_DASH} now")

    assert directive.mode is TaskMode.RELAY
    assert directive.target == "sra-codex"
    assert directive.message == f"fix {_EM_DASH} now"


@pytest.mark.parametrize(
    "raw_text, expected_cleaned",
    [
        ("build the “thing” now", 'build the "thing" now'),
        ("don’t break it", "don't break it"),
        ("‘single’ quotes here", "'single' quotes here"),
    ],
)
def test_curly_quotes_are_straightened(raw_text: str, expected_cleaned: str) -> None:
    directive = parse_directive(raw_text)

    assert directive.mode is TaskMode.DELEGATE
    assert directive.cleaned_text == expected_cleaned


@pytest.mark.parametrize(
    "raw_text, expected",
    [
        ("<@U0TEST> fix the login page, reply in slack", True),
        ("Reply In Slack when you are done", True),
        ("fix it and REPLY IN SLACK", True),
        ("fix it, reply  in\nslack please", True),
        ("fix the login page", False),
        ("reply in slackware docs", False),
        ("reply on slack", False),
        ("", False),
    ],
)
def test_wants_slack_reply_matches_the_phrase_anywhere(raw_text: str, expected: bool) -> None:
    assert wants_slack_reply(raw_text) is expected


_NEW_NOTICE = "--new is retired: start the message with new: instead, as in @pan new: <task>"
_STATUS_NOTICE = (
    "--status is retired: @pan check posts this thread's latest answer, "
    "and @pan sessions lists every session"
)
_SYNC_NOTICE = "--sync is retired: add reply in slack to the message instead"
_BANG_NOTICE = "a leading ! is retired: add reply in slack to the message instead"


@pytest.mark.parametrize(
    "raw_text, expected_route",
    [
        # new: <task>
        ("<@U0TEST> new: fix the login page", Route(kind=RouteKind.NEW, text="fix the login page")),
        ("NEW: fix it", Route(kind=RouteKind.NEW, text="fix it")),
        ("New:   fix it  ", Route(kind=RouteKind.NEW, text="fix it")),
        (
            "<@U0TEST> new: fix it, reply in slack",
            Route(kind=RouteKind.NEW, text="fix it, reply in slack", reply_in_slack=True),
        ),
        ("new: --stream login fix it", Route(kind=RouteKind.NEW, text="--stream login fix it")),
        ("new: fix\nthe two bugs", Route(kind=RouteKind.NEW, text="fix\nthe two bugs")),
        ("new login page for the docs", Route(text="new login page for the docs")),
        ("new:fix it", Route(text="new:fix it")),
        ("new : fix it", Route(text="new : fix it")),
        ("please new: fix it", Route(text="please new: fix it")),
        (
            "new:",
            Route(
                kind=RouteKind.NOTICE, notice="new: needs a task after it, as in @pan new: <task>"
            ),
        ),
        # to <name>: <task>
        (
            "<@U0TEST> to pan-7: add the tests",
            Route(kind=RouteKind.TO, session="pan-7", text="add the tests"),
        ),
        ("TO desk-api: run it", Route(kind=RouteKind.TO, session="desk-api", text="run it")),
        (
            "to pan-7: ship it, Reply In Slack",
            Route(
                kind=RouteKind.TO,
                session="pan-7",
                text="ship it, Reply In Slack",
                reply_in_slack=True,
            ),
        ),
        ("to pan-7 add the tests", Route(text="to pan-7 add the tests")),
        ("to pan-7 : add the tests", Route(text="to pan-7 : add the tests")),
        ("to pan-7:add the tests", Route(text="to pan-7:add the tests")),
        ("to the moon: and back", Route(text="to the moon: and back")),
        ("go to pan-7: add the tests", Route(text="go to pan-7: add the tests")),
        (
            "to pan-7:",
            Route(
                kind=RouteKind.NOTICE,
                session="pan-7",
                notice="to pan-7: needs a task after it, as in @pan to pan-7: <task>",
            ),
        ),
        # whole-message words
        ("<@U0TEST> sessions", Route(kind=RouteKind.SESSIONS)),
        ("Sessions", Route(kind=RouteKind.SESSIONS)),
        ("  check  ", Route(kind=RouteKind.CHECK)),
        ("<@U0TEST>CHECK", Route(kind=RouteKind.CHECK)),
        ("forget", Route(kind=RouteKind.FORGET)),
        ("check the logs", Route(text="check the logs")),
        ("check?", Route(text="check?")),
        ("forget it", Route(text="forget it")),
        ("sessions please", Route(text="sessions please")),
        ("please check", Route(text="please check")),
        # retired flags
        (
            "--new build the feature",
            Route(kind=RouteKind.NOTICE, text="--new build the feature", notice=_NEW_NOTICE),
        ),
        (
            "update the readme --new",
            Route(kind=RouteKind.NOTICE, text="update the readme --new", notice=_NEW_NOTICE),
        ),
        (
            "<@U0TEST> \u2014new fix it",
            Route(kind=RouteKind.NOTICE, text="--new fix it", notice=_NEW_NOTICE),
        ),
        ("--status", Route(kind=RouteKind.NOTICE, text="--status", notice=_STATUS_NOTICE)),
        ("--sync deploy", Route(kind=RouteKind.NOTICE, text="--sync deploy", notice=_SYNC_NOTICE)),
        (
            "!deploy the service",
            Route(kind=RouteKind.NOTICE, text="!deploy the service", notice=_BANG_NOTICE),
        ),
        ("relay pan-7 use --sync here", Route(text="relay pan-7 use --sync here")),
        ("read pan-7 --status", Route(text="read pan-7 --status")),
        # plain tasks
        ("<@U0TEST> fix the login page", Route(text="fix the login page")),
        ("fix it, reply in slack", Route(text="fix it, reply in slack", reply_in_slack=True)),
        ("run the script with args a!b", Route(text="run the script with args a!b")),
        ("--sessions", Route(text="--sessions")),
        ("relay pan-7 hello", Route(text="relay pan-7 hello")),
        ("", Route()),
    ],
)
def test_parse_route(raw_text: str, expected_route: Route) -> None:
    assert parse_route(raw_text) == expected_route


@pytest.mark.parametrize(
    "raw_text, expected_route",
    [
        ("check\n*Sent using* <@U0TEST|Claude>", Route(kind=RouteKind.CHECK)),
        ("<@U0BOT> check\n*Sent using* <@U0TEST|Claude>", Route(kind=RouteKind.CHECK)),
        ("sessions *Sent using* <@U0TEST>", Route(kind=RouteKind.SESSIONS)),
        ("forget\n_sent using_ <@U0TEST|Claude>", Route(kind=RouteKind.FORGET)),
        ("Check\n\n> *Sent using* <@U0TEST|Claude>  ", Route(kind=RouteKind.CHECK)),
        ("check\n&gt; *Sent using* <@U0TEST|Claude>", Route(kind=RouteKind.CHECK)),
        ("check SENT USING <@U0TEST>", Route(kind=RouteKind.CHECK)),
        (
            "new: fix the login page\n*Sent using* <@U0TEST|Claude>",
            Route(kind=RouteKind.NEW, text="fix the login page"),
        ),
        (
            "to desk-test: reply with the word hello *Sent using* <@U0TEST>",
            Route(kind=RouteKind.TO, session="desk-test", text="reply with the word hello"),
        ),
        (
            "fix the bug\n*Sent using* <@U0TEST|Claude>",
            Route(text="fix the bug"),
        ),
        (
            "say who sent using <@U0TEST> earlier",
            Route(text="say who sent using <@U0TEST> earlier"),
        ),
        ("ask <@U0TEST> about it", Route(text="ask <@U0TEST> about it")),
    ],
    ids=[
        "check-exact-form",
        "check-after-mention",
        "sessions-same-line",
        "forget-underscore",
        "check-quoted",
        "check-escaped-quote",
        "check-upper-case",
        "new-task",
        "to-task",
        "plain-task",
        "mid-text-kept",
        "trailing-mention-kept",
    ],
)
def test_parse_route_strips_a_trailing_client_signature(
    raw_text: str, expected_route: Route
) -> None:
    assert parse_route(raw_text) == expected_route


def test_parse_directive_strips_a_trailing_client_signature() -> None:
    directive = parse_directive("<@U0BOT> fix the bug\n*Sent using* <@U0TEST|Claude>")

    assert directive.cleaned_text == "fix the bug"


@pytest.mark.parametrize(
    "raw_text, expected_route",
    [
        (
            "to desk-api: delete the branch",
            Route(kind=RouteKind.NOTICE, session="desk-api", notice=X_TO_ROUTE_NOTICE),
        ),
        (
            "<@U0TEST> to pan-7: add the tests",
            Route(kind=RouteKind.NOTICE, session="pan-7", notice=X_TO_ROUTE_NOTICE),
        ),
        ("new: summarize the post", Route(kind=RouteKind.NEW, text="summarize the post")),
        ("summarize the post", Route(text="summarize the post")),
    ],
    ids=["to-desk-session", "to-pan-session", "new-allowed", "plain-task-allowed"],
)
def test_parse_route_from_x_never_reaches_an_existing_session(
    raw_text: str, expected_route: Route
) -> None:
    assert parse_route(raw_text, ThreadSource.X) == expected_route
