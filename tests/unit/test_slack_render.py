from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from terminal_host_fakes import make_terminal

from pan.models import (
    AgentStatus,
    SessionSummary,
    StatusState,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.slack_render import (
    SESSIONS_EMPTY_TEXT,
    SESSIONS_HEADER,
    SESSIONS_NONE_SHOWN_TEXT,
    check_state,
    elapsed_text,
    escape_mrkdwn,
    plain_mrkdwn,
    private_details_text,
    render_sessions_for_slack,
    session_display_name,
    session_group,
    sessions_to_show,
    slack_time,
    status_blocks,
    status_label,
    status_text,
)

_NOW = datetime(2026, 7, 16, 10, 30, 0, tzinfo=UTC)
_NOW_SLACK_TIME = "<!date^1784197800^{time}|10:30 UTC>"


def _record(**updates: object) -> ThreadRecord:
    record = ThreadRecord(
        thread_ts="1700000000.000100",
        terminal_name="pan-login",
        terminal_address="t7",
        channel="C0TEST",
        worktree_path=Path("/tmp/wt/pan-login"),
        created_at=_NOW,
        updated_at=_NOW,
        inbox_thread_name="pan-login: fix the login page",
    )
    return record.model_copy(update=updates)


@pytest.mark.parametrize(
    "task, max_length, expected",
    [
        ("fix the login page", 80, "fix the login page"),
        ("  fix\n the   login\tpage ", 80, "fix the login page"),
        ("fix the login page", 10, "fix the..."),
        ("abcdefghij", 10, "abcdefghij"),
        ("", 80, ""),
    ],
    ids=["short", "whitespace-collapsed", "cut", "exactly-max", "empty"],
)
def test_status_label(task: str, max_length: int, expected: str) -> None:
    assert status_label(task, max_length) == expected


@pytest.mark.parametrize(
    "elapsed, expected",
    [
        (timedelta(seconds=59), None),
        (timedelta(minutes=1), "1m"),
        (timedelta(minutes=59, seconds=59), "59m"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=2, minutes=5), "2h 5m"),
        (timedelta(seconds=-5), None),
    ],
    ids=[
        "under-a-minute",
        "one-minute",
        "under-an-hour",
        "one-hour",
        "hours-and-minutes",
        "clock-skew",
    ],
)
def test_elapsed_text(elapsed: timedelta, expected: str | None) -> None:
    assert elapsed_text(elapsed) == expected


def test_slack_time_renders_in_the_readers_zone_with_a_utc_fallback() -> None:
    assert slack_time(_NOW) == _NOW_SLACK_TIME


@pytest.mark.parametrize(
    "text, expected",
    [("a < b & c > d", "a &lt; b &amp; c &gt; d"), ("plain", "plain")],
    ids=["control-characters", "plain"],
)
def test_escape_mrkdwn(text: str, expected: str) -> None:
    assert escape_mrkdwn(text) == expected


@pytest.mark.parametrize(
    "terminal_state, agent_status, recorded, expected_state",
    [
        (TerminalState.LIVE, AgentStatus.WORKING, WorkerStatus.IDLE, StatusState.WORKING),
        (TerminalState.LIVE, AgentStatus.BLOCKED, WorkerStatus.RUNNING, StatusState.WAITING),
        (TerminalState.LIVE, AgentStatus.IDLE, WorkerStatus.RUNNING, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.DONE, WorkerStatus.IDLE, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.DONE, WorkerStatus.DONE, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.ERROR, WorkerStatus.RUNNING, StatusState.STOPPED),
        (TerminalState.LIVE, AgentStatus.UNKNOWN, WorkerStatus.RUNNING, StatusState.WORKING),
        (TerminalState.LIVE, AgentStatus.UNKNOWN, WorkerStatus.IDLE, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.NONE, WorkerStatus.SPAWNING, StatusState.WORKING),
        (TerminalState.LIVE, AgentStatus.NONE, WorkerStatus.FAILED, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.NONE, WorkerStatus.BLOCKED, StatusState.WAITING),
        (TerminalState.FINISHED, AgentStatus.DONE, WorkerStatus.IDLE, StatusState.STOPPED),
        (None, None, WorkerStatus.IDLE, StatusState.STOPPED),
        (None, None, WorkerStatus.FAILED, StatusState.STOPPED),
        (None, None, WorkerStatus.DONE, StatusState.CLOSED),
    ],
    ids=[
        "working",
        "blocked-on-a-prompt",
        "idle",
        "hc-done-is-a-finished-turn",
        "hc-done-never-closed-while-live",
        "agent-error",
        "unknown-while-running",
        "unknown-while-idle",
        "no-status-while-starting",
        "no-status-never-stopped-while-live",
        "no-status-blocked-record",
        "terminal-finished",
        "terminal-gone",
        "terminal-gone-failed",
        "terminal-gone-after-pan-done",
    ],
)
def test_check_state(
    terminal_state: TerminalState | None,
    agent_status: AgentStatus | None,
    recorded: WorkerStatus,
    expected_state: StatusState,
) -> None:
    terminal = (
        make_terminal("t7", name="pan-login", state=terminal_state, agent_status=agent_status)
        if terminal_state is not None and agent_status is not None
        else None
    )

    assert check_state(terminal, recorded) is expected_state


@pytest.mark.parametrize(
    "state, label, expected",
    [
        (StatusState.WORKING, "fix the login page", "Working: fix the login page"),
        (StatusState.WAITING, "fix the login page", "Waiting for you: fix the login page"),
        (StatusState.FINISHED, "fix the login page", "Finished: fix the login page"),
        (StatusState.CLOSED, "fix the login page", "Closed: fix the login page"),
        (StatusState.STOPPED, "fix the login page", "Stopped: fix the login page"),
        (StatusState.FINISHED, None, "Finished"),
    ],
    ids=["working", "waiting", "finished", "closed", "stopped", "no-label"],
)
def test_status_text(state: StatusState, label: str | None, expected: str) -> None:
    assert status_text(state, label) == expected


@pytest.mark.parametrize(
    "state, expected_title",
    [
        (StatusState.WORKING, "Working"),
        (StatusState.WAITING, "Waiting for you"),
        (StatusState.FINISHED, "Finished"),
        (StatusState.CLOSED, "Closed"),
        (StatusState.STOPPED, "Stopped"),
    ],
)
def test_status_blocks_per_state(state: StatusState, expected_title: str) -> None:
    blocks = status_blocks(
        state,
        session_name="pan-login",
        label="fix the login page",
        started_at=_NOW - timedelta(minutes=4),
        now=_NOW,
    )

    assert blocks == [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{expected_title}*\nfix the login page"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"pan-login · 4m elapsed · updated {_NOW_SLACK_TIME}"}
            ],
        },
    ]


@pytest.mark.parametrize(
    "label, started_at, replies_in_thread, detail, expected_section, expected_context",
    [
        (
            None,
            None,
            False,
            None,
            "*Working*",
            f"pan-login · updated {_NOW_SLACK_TIME}",
        ),
        (
            "fix <the> page",
            _NOW - timedelta(seconds=20),
            True,
            None,
            "*Working*\nfix &lt;the&gt; page",
            f"pan-login · updated {_NOW_SLACK_TIME} · answers posted here",
        ),
        (
            "fix the login page",
            _NOW - timedelta(hours=1, minutes=2),
            False,
            "Claude needs your permission to use Bash",
            "*Working*\nfix the login page\n_Claude needs your permission to use Bash_",
            f"pan-login · 1h 2m elapsed · updated {_NOW_SLACK_TIME}",
        ),
    ],
    ids=["old-record-no-label", "escaped-label-replies-here", "with-detail"],
)
def test_status_blocks_optional_parts(
    label: str | None,
    started_at: datetime | None,
    replies_in_thread: bool,
    detail: str | None,
    expected_section: str,
    expected_context: str,
) -> None:
    blocks = status_blocks(
        StatusState.WORKING,
        session_name="pan-login",
        label=label,
        started_at=started_at,
        now=_NOW,
        replies_in_thread=replies_in_thread,
        detail=detail,
    )

    assert blocks[0]["text"]["text"] == expected_section
    assert blocks[1]["elements"][0]["text"] == expected_context


@pytest.mark.parametrize(
    "updates, expected_inbox_line",
    [
        (
            {"inbox_thread_url": "http://localhost:19441/#thread=abc"},
            "Inbox thread: <http://localhost:19441/#thread=abc|pan-login: fix the login page>",
        ),
        (
            {"inbox_thread_url": "http://localhost:19441/#thread=abc", "inbox_thread_name": None},
            "Inbox thread: <http://localhost:19441/#thread=abc|open>",
        ),
        ({}, 'Inbox thread: "pan-login: fix the login page"'),
        (
            {"started_by_pan": False},
            'Inbox thread: its existing one, or "pan-login: fix the login page"',
        ),
        (
            {"started_by_pan": False, "inbox_thread_id": "thr_1"},
            'Inbox thread: "pan-login: fix the login page"',
        ),
        ({"inbox_thread_name": None}, "Inbox thread: none yet"),
    ],
    ids=[
        "link",
        "link-without-name",
        "own-session-name",
        "desk-session-suggested-name",
        "desk-session-found-thread",
        "none",
    ],
)
def test_private_details_text_names_the_inbox_thread(
    updates: dict[str, object], expected_inbox_line: str
) -> None:
    assert private_details_text(_record(**updates)) == (
        f"Details for pan-login (only visible to you)\n{expected_inbox_line}\nTerminal: t7"
    )


def test_private_details_text_without_a_terminal_address() -> None:
    assert private_details_text(_record(terminal_address="")) == (
        'Details for pan-login (only visible to you)\nInbox thread: "pan-login: fix the login page"'
    )


_ORCHESTRATOR = "pan-orchestrator"
_THREAD_LINK = "https://example.slack.com/archives/C000TEST/p1700000000000100"


def _session(**updates: object) -> SessionSummary:
    session = SessionSummary(
        address="t12",
        name="pan-sizing",
        cwd=Path("/tmp/wt/pan-sizing"),
        agent="claude-code",
        agent_status=AgentStatus.WORKING,
    )
    return session.model_copy(update=updates)


def _names(sessions: list[SessionSummary]) -> list[str | None]:
    return [session.name for session in sessions]


@pytest.mark.parametrize(
    "agent_status, agent, pan_status, expected",
    [
        (AgentStatus.BLOCKED, "claude-code", None, "Waiting for you"),
        (AgentStatus.WORKING, "claude-code", None, "Working"),
        (AgentStatus.IDLE, "claude-code", WorkerStatus.IDLE, "Idle"),
        (AgentStatus.DONE, "claude-code", None, "Finished"),
        (AgentStatus.ERROR, "claude-code", WorkerStatus.FAILED, "Stopped"),
        (AgentStatus.NONE, "zsh", WorkerStatus.SPAWNING, "Working"),
        (AgentStatus.NONE, "zsh", WorkerStatus.RUNNING, None),
        (AgentStatus.NONE, None, None, None),
        (AgentStatus.NONE, "zsh", None, None),
        (AgentStatus.NONE, "claude-code", WorkerStatus.RUNNING, "Working"),
        (AgentStatus.NONE, "claude", WorkerStatus.BLOCKED, "Waiting for you"),
        (AgentStatus.NONE, "claude-code", None, "Idle"),
        (AgentStatus.UNKNOWN, "claude-code", WorkerStatus.BLOCKED, "Waiting for you"),
        (AgentStatus.UNKNOWN, "claude-code", WorkerStatus.FAILED, "Idle"),
        (AgentStatus.UNKNOWN, "claude-code", None, "Working"),
    ],
    ids=[
        "blocked",
        "working",
        "idle",
        "error",
        "done",
        "booting-worker",
        "dropped-to-a-shell",
        "no-program",
        "plain-shell",
        "claude-first-turn-uses-the-record",
        "claude-first-turn-blocked-record",
        "claude-first-turn-without-a-record",
        "unknown-uses-the-record",
        "unknown-live-failed-record-is-never-stopped",
        "unknown-without-a-record",
    ],
)
def test_session_group(
    agent_status: AgentStatus,
    agent: str | None,
    pan_status: WorkerStatus | None,
    expected: str | None,
) -> None:
    summary = _session(agent_status=agent_status, agent=agent, pan_status=pan_status)

    assert session_group(summary) == expected


@pytest.mark.parametrize(
    "updates, expected",
    [
        (
            {"label": "Google Doc for the plan", "inbox_thread_name": "pan-sizing: plan"},
            "Google Doc for the plan",
        ),
        ({"inbox_thread_name": "pan-sizing: plan"}, "pan-sizing: plan"),
        ({}, "pan-sizing"),
        ({"name": None}, "pan-sizing"),
        ({"name": None, "where": "jiracli-generate / 1"}, "jiracli-generate"),
        ({"name": None, "where": "moshi-browser / pan"}, "moshi-browser / pan"),
        ({"name": None, "where": "moshi-browser / pan 2"}, "moshi-browser / pan 2"),
        ({"name": "pan", "where": "moshi-browser / pan"}, "pan"),
        ({"name": None, "cwd": Path("/Users/someone/dev/api")}, "api"),
    ],
    ids=[
        "label",
        "inbox-thread",
        "terminal-name",
        "folder-of-a-worktree",
        "where-without-its-tab-number",
        "where-with-a-named-tab",
        "where-ending-in-a-word-and-number",
        "terminal-name-before-where",
        "folder",
    ],
)
def test_session_display_name(updates: dict[str, object], expected: str) -> None:
    assert session_display_name(_session(**updates)) == expected


@pytest.mark.parametrize(
    "addresses, expected_names",
    [
        (["t3", "t1", "t2"], ["three", "one", "two"]),
        (["t2", "t2", "t1"], ["two", "one"]),
        (["t9", "t1"], ["one"]),
        (["t0", "t4", "t1"], ["one"]),
        ([], []),
        (["t1", "t2", "t3", "t5", "t6", "t7", "t8"], ["one", "two", "three", "five", "six"]),
    ],
    ids=[
        "given-order",
        "each-once",
        "unknown-dropped",
        "orchestrator-and-shell-dropped",
        "none",
        "at-most-five",
    ],
)
def test_sessions_to_show(addresses: list[str], expected_names: list[str]) -> None:
    sessions = [
        _session(address="t0", name=_ORCHESTRATOR),
        _session(address="t1", name="one"),
        _session(address="t2", name="two", agent_status=AgentStatus.IDLE),
        _session(address="t3", name="three", agent_status=AgentStatus.BLOCKED),
        _session(address="t4", name="zsh-1", agent="zsh", agent_status=AgentStatus.NONE),
        *(
            _session(address=f"t{index}", name=name)
            for index, name in [(5, "five"), (6, "six"), (7, "seven"), (8, "eight")]
        ),
    ]

    assert _names(sessions_to_show(sessions, _ORCHESTRATOR, addresses)) == expected_names


def test_render_sessions_for_slack() -> None:
    sessions = [
        _session(
            address="t3",
            name="pan-sizing",
            label="Google Doc for the plan",
            agent_status=AgentStatus.BLOCKED,
            thread_ts="1700000000.000100",
        ),
        _session(address="t5", name="herdr-core-migration", cwd=Path("/tmp/wt/herdr")),
        _session(
            address="t8",
            name=None,
            cwd=Path("/Users/someone/dev/api"),
            agent_status=AgentStatus.IDLE,
        ),
    ]

    text = render_sessions_for_slack(sessions, {"1700000000.000100": _THREAD_LINK})

    assert text == (
        f"{SESSIONS_HEADER}\n"
        f"• *Google Doc for the plan* · Waiting for you · <{_THREAD_LINK}|thread>\n"
        "• *herdr-core-migration* · Working\n"
        "• *api* · Idle"
    )
    for hidden_detail in ["t3", "t5", "t8", "/Users/", "/tmp/"]:
        assert hidden_detail not in text.replace(_THREAD_LINK, "")


@pytest.mark.parametrize(
    "any_running, expected",
    [(True, SESSIONS_NONE_SHOWN_TEXT), (False, SESSIONS_EMPTY_TEXT)],
    ids=["sessions-running", "nothing-running"],
)
def test_render_sessions_for_slack_with_nothing_selected(any_running: bool, expected: str) -> None:
    assert render_sessions_for_slack([], {}, any_running=any_running) == expected


@pytest.mark.parametrize(
    "thread_ts, permalinks, expected_line",
    [
        (
            "1700000000.000100",
            {"1700000000.000100": _THREAD_LINK},
            f"• *pan-sizing* · Working · <{_THREAD_LINK}|thread>",
        ),
        ("1700000000.000100", {}, "• *pan-sizing* · Working"),
        (None, {"1700000000.000100": _THREAD_LINK}, "• *pan-sizing* · Working"),
    ],
    ids=["link", "no-link-from-slack", "no-thread"],
)
def test_render_sessions_for_slack_links_the_thread_when_known(
    thread_ts: str | None, permalinks: dict[str, str], expected_line: str
) -> None:
    text = render_sessions_for_slack([_session(thread_ts=thread_ts)], permalinks)

    assert text.splitlines()[1] == expected_line


@pytest.mark.parametrize(
    "text, expected",
    [
        ("**urgent** fix", "urgent fix"),
        ("make *all* tests pass", "make all tests pass"),
        ("_draft_ ~old~ `plan`", "draft old plan"),
        ("keep snake_case_names", "keep snake_case_names"),
        ("fix the _build_", "fix the build"),
        ("compare <a> & <b>", "compare &lt;a&gt; &amp; &lt;b&gt;"),
    ],
    ids=[
        "bold",
        "italic-asterisks",
        "underscores-tildes-backticks",
        "snake-case",
        "edge",
        "escaped",
    ],
)
def test_plain_mrkdwn(text: str, expected: str) -> None:
    assert plain_mrkdwn(text) == expected


def test_render_sessions_for_slack_drops_emphasis_from_a_name() -> None:
    text = render_sessions_for_slack([_session(label="**urgent** fix")], {})

    assert text.splitlines()[1] == "• *urgent fix* · Working"
