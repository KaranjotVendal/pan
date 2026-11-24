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
from pan.slack_format import to_slack_mrkdwn
from pan.slack_render import (
    SESSIONS_EMPTY_TEXT,
    SESSIONS_FOOTER,
    check_state,
    elapsed_text,
    escape_mrkdwn,
    private_details_text,
    render_sessions_for_slack,
    session_display_name,
    session_group,
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


def _render(*sessions: SessionSummary, permalinks: dict[str, str] | None = None) -> str:
    return render_sessions_for_slack(
        list(sessions), permalinks or {}, orchestrator_name=_ORCHESTRATOR
    )


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


def test_render_sessions_for_slack_groups_needs_action_first() -> None:
    sessions = [
        _session(
            address="t3",
            name="pan-sizing",
            label="Google Doc for the plan",
            agent_status=AgentStatus.BLOCKED,
            thread_ts="1700000000.000100",
            thread_channel="C000TEST",
            last_ask="make a google doc of the sizing plan",
        ),
        _session(address="t5", name="herdr-core-migration", cwd=Path("/tmp/wt/herdr")),
        _session(
            address="t8",
            name=None,
            cwd=Path("/Users/someone/dev/api"),
            agent_status=AgentStatus.DONE,
        ),
    ]

    text = to_slack_mrkdwn(_render(*sessions, permalinks={"1700000000.000100": _THREAD_LINK}))

    assert text == (
        "*3 sessions*\n"
        "*Waiting for you*\n"
        f"• *Google Doc for the plan* · <{_THREAD_LINK}|thread>\n"
        "    _make a google doc of the sizing plan_\n"
        "*Working*\n"
        "• *herdr-core-migration*\n"
        "*Finished (1):* api\n"
        f"{SESSIONS_FOOTER}"
    )
    for hidden_detail in ["t3", "t5", "t8", "/Users/", "/tmp/", "C000TEST|"]:
        assert hidden_detail not in text.replace(_THREAD_LINK, "")


@pytest.mark.parametrize(
    "sessions",
    [
        [],
        [_session(name=_ORCHESTRATOR)],
        [_session(agent="zsh", agent_status=AgentStatus.NONE)],
    ],
    ids=["none", "only-the-orchestrator", "only-a-plain-shell"],
)
def test_render_sessions_for_slack_with_nothing_to_list(sessions: list[SessionSummary]) -> None:
    assert _render(*sessions) == SESSIONS_EMPTY_TEXT


@pytest.mark.parametrize(
    "hidden",
    [
        _session(address="t1", name=_ORCHESTRATOR),
        _session(address="t1", name="zsh-1", agent="zsh", agent_status=AgentStatus.NONE),
    ],
    ids=["orchestrator", "plain-shell"],
)
def test_render_sessions_for_slack_hides_terminals_without_a_listed_session(
    hidden: SessionSummary,
) -> None:
    text = _render(_session(), hidden)

    assert text.startswith("**1 session**\n")
    assert str(hidden.name) not in text


@pytest.mark.parametrize(
    "thread_ts, permalinks, expected_line",
    [
        (
            "1700000000.000100",
            {"1700000000.000100": _THREAD_LINK},
            f"• **pan-sizing** · <{_THREAD_LINK}|thread>",
        ),
        ("1700000000.000100", {}, "• **pan-sizing**"),
        (None, {"1700000000.000100": _THREAD_LINK}, "• **pan-sizing**"),
    ],
    ids=["link", "no-link-from-slack", "no-thread"],
)
def test_render_sessions_for_slack_links_the_thread_when_known(
    thread_ts: str | None, permalinks: dict[str, str], expected_line: str
) -> None:
    text = _render(_session(thread_ts=thread_ts), permalinks=permalinks)

    assert text.splitlines()[2] == expected_line


@pytest.mark.parametrize(
    "last_ask, expected_line",
    [
        ("fix the login page", "    _fix the login page_"),
        ("a" * 120, f"    _{'a' * 77}..._"),
        ("compare <a> & <b>", "    _compare &lt;a&gt; &amp; &lt;b&gt;_"),
        (None, None),
    ],
    ids=["short", "cut-short", "escaped", "none"],
)
def test_render_sessions_for_slack_shows_the_last_ask(
    last_ask: str | None, expected_line: str | None
) -> None:
    lines = _render(_session(last_ask=last_ask)).splitlines()

    assert (lines[3] if lines[3] != SESSIONS_FOOTER else None) == expected_line


def _quiet_session(index: int, agent_status: AgentStatus = AgentStatus.IDLE) -> SessionSummary:
    return _session(
        address=f"t{index}",
        name=f"stream-{index:02d}",
        agent_status=agent_status,
        thread_ts="1700000000.000100",
        last_ask="not shown for a quiet session",
    )


@pytest.mark.parametrize(
    "agent_status, title",
    [
        (AgentStatus.IDLE, "Idle"),
        (AgentStatus.DONE, "Finished"),
        (AgentStatus.ERROR, "Stopped"),
    ],
)
def test_render_sessions_for_slack_lists_a_quiet_group_on_one_line(
    agent_status: AgentStatus, title: str
) -> None:
    sessions = [_quiet_session(index, agent_status) for index in range(1, 4)]

    text = _render(*sessions, permalinks={"1700000000.000100": _THREAD_LINK})

    assert text.splitlines() == [
        "**3 sessions**",
        f"**{title} (3):** stream-01, stream-02, stream-03",
        SESSIONS_FOOTER,
    ]


def test_render_sessions_for_slack_cuts_a_long_quiet_group() -> None:
    sessions = [_quiet_session(index) for index in range(1, 61)]

    idle_line = _render(*sessions).splitlines()[1]

    assert idle_line.startswith("**Idle (60):** stream-01, stream-02, ")
    shown_count = idle_line.count("stream-")
    assert idle_line.endswith(f"stream-{shown_count:02d} and {60 - shown_count} more")
    assert len(idle_line) <= 400


@pytest.mark.parametrize(
    "session, expected_lines",
    [
        (
            _session(label="**urgent** fix", last_ask="make *all* tests pass"),
            ["• **urgent fix**", "    _make all tests pass_"],
        ),
        (
            _session(label="_draft_ ~old~ `plan`", last_ask="keep snake_case_names"),
            ["• **draft old plan**", "    _keep snake_case_names_"],
        ),
    ],
    ids=["asterisks", "underscores-tildes-backticks"],
)
def test_render_sessions_for_slack_drops_emphasis_from_free_text(
    session: SessionSummary, expected_lines: list[str]
) -> None:
    assert _render(session).splitlines()[2:4] == expected_lines


def test_render_sessions_for_slack_drops_emphasis_from_a_compact_line() -> None:
    session = _session(label="*fix* the _build_", agent_status=AgentStatus.IDLE)

    assert _render(session).splitlines()[1] == "**Idle (1):** fix the build"
