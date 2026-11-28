from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from inbox_fakes import FakeInboxDirectory
from project_map_fakes import FakeProjectMapSource
from reporter_fakes import RecordingReporter
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.backend.confirmations import (
    FilePendingConfirmations,
    confirm_session,
    propose_sessions,
)
from pan.backend.handoff import handoff_thread
from pan.backend.project_routing import hand_to_project, nudge_unhanded_threads
from pan.backend.session_control import (
    check_thread,
    follow_up_to_session,
    from_slack_text,
    send_to_session,
)
from pan.backend.sessions import collect_sessions
from pan.backend.spawn import inbox_thread_name
from pan.core.errors import (
    HandoffTargetNotFoundError,
    NoExactMatchError,
    NotAwaitingHandoffError,
    PanError,
    SessionBusyError,
    SlackPostError,
    TargetBlockedError,
    TargetInOtherProjectError,
    TerminalGoneError,
    ThreadNotFoundError,
)
from pan.core.events import (
    AnswerReady,
    NotDelivered,
    NotDeliveredReason,
    PrivateDetails,
    QuestionAsked,
    ScreenCaptured,
    SessionNotice,
    SessionNoticeReason,
    StatusChanged,
    ThreadNotice,
    ThreadNoticeReason,
)
from pan.core.models import (
    AgentStatus,
    Conversation,
    ProjectHandoff,
    ReplyMode,
    StatusState,
    Terminal,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.data.threadmap import FileThreadMap
from pan.frontend.slack_render import handoff_line
from pan.utils.config import WORKER_BRIEF_TASK_MARKER

_NOW = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)
_THREAD = "1700000000.000100"
_OTHER_THREAD = "1700000000.000200"
_CONVERSATION = Conversation.slack(_THREAD, "C0TEST")
_OTHER_CONVERSATION = Conversation.slack(_OTHER_THREAD, "C0TEST")
_THREAD_LINK = "https://example.slack.com/archives/C0TEST/p1700000000000100"
_NO_STREAM = ThreadNotice(reason=ThreadNoticeReason.NO_STREAM_PICKED)


class FixedClock:
    def now(self) -> datetime:
        return _NOW


def _orchestrator(**updates: object) -> Terminal:
    terminal = make_terminal(
        "t40", name="atlas-orch", where="atlas / 1", cwd=Path("/tmp/atlas"), turn=3
    )
    return terminal.model_copy(update=updates)


# Two projects (atlas: t40 with streams t41 and t42; borealis: t50 with t51), as the map
# source reports them.
_PROJECTS = [("atlas", ["t40", "t41", "t42"]), ("borealis", ["t50", "t51"])]


def _map_source(*extra_projects: tuple[str, list[str]]) -> FakeProjectMapSource:
    return FakeProjectMapSource(projects=[*_PROJECTS, *extra_projects])


def _terminals(*extra: Terminal, orchestrator: Terminal | None = None) -> list[Terminal]:
    # The two projects' sessions, a standalone session t60 and a plain shell t61.
    return [
        orchestrator if orchestrator is not None else _orchestrator(),
        make_terminal("t41", name="stream-login", parent="t40", cwd=Path("/tmp/atlas-login")),
        make_terminal("t42", name="stream-docs", parent="t40", cwd=Path("/tmp/atlas-docs")),
        make_terminal("t50", name="borealis-orch", where="borealis / 1", cwd=Path("/tmp/b")),
        make_terminal("t51", name="stream-api", parent="t50", cwd=Path("/tmp/b-api")),
        make_terminal("t60", name="solo", where="misc / 1", cwd=Path("/tmp/solo")),
        make_terminal(
            "t61",
            name="dev-shell",
            parent="t40",
            cwd=Path("/tmp/atlas"),
            agent="zsh",
            agent_status=AgentStatus.NONE,
        ),
        *extra,
    ]


def _host(terminals: list[Terminal]) -> FakeTerminalHost:
    terminal_host = FakeTerminalHost()
    for terminal in terminals:
        terminal_host.add(terminal)
    return terminal_host


def _thread_map(tmp_path: Path) -> FileThreadMap:
    return FileThreadMap(tmp_path / "threads.json", FixedClock())


def _hand(
    terminal_host: FakeTerminalHost,
    thread_map: FileThreadMap,
    reporter: RecordingReporter,
    selector: str = "atlas",
    *,
    conversation: Conversation = _CONVERSATION,
    task: str = "fix the login page",
    project_map_source: FakeProjectMapSource | None = None,
) -> ThreadRecord:
    return hand_to_project(
        conversation=conversation,
        selector=selector,
        text=task,
        requester="U000TEST",
        reply_in_slack=False,
        thread_link=lambda: _THREAD_LINK,
        terminal_host=terminal_host,
        thread_map=thread_map,
        reporter=reporter,
        clock=FixedClock(),
        project_map_source=project_map_source or _map_source(),
    )


def _handoff(
    terminal_host: FakeTerminalHost,
    thread_map: FileThreadMap,
    reporter: RecordingReporter,
    selector: str,
    project_map_source: FakeProjectMapSource | None = None,
    note: str | None = None,
) -> ThreadRecord:
    return handoff_thread(
        conversation=_CONVERSATION,
        selector=selector,
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=FakeInboxDirectory(),
        reporter=reporter,
        clock=FixedClock(),
        project_map_source=project_map_source or _map_source(),
        note=note,
    )


def _nudge(terminal_host: FakeTerminalHost, thread_map: FileThreadMap) -> RecordingReporter:
    reporter = RecordingReporter()
    nudge_unhanded_threads(
        thread_map=thread_map,
        terminal_host=terminal_host,
        open_reporter=lambda: reporter,
        clock=FixedClock(),
    )
    return reporter


def test_hand_to_project_types_the_task_with_the_filled_in_handoff_command(
    tmp_path: Path,
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    reporter = RecordingReporter()

    _hand(terminal_host, thread_map, reporter)

    assert terminal_host.sent == [
        (
            "t40",
            "From Slack via pan, for the atlas project. Pick the stream that should do this "
            f"task (or start one), then run: pan handoff --thread {_THREAD} --channel C0TEST "
            "--to <stream> (<stream> is that stream's name or address; add --note '<text>' "
            "for instructions of your own). pan passes the task and the thread to that "
            "stream. Don't do the task yourself unless you hand it to yourself (--to <your "
            "own name>). Until then nothing you write reaches Slack."
            f"\nSlack thread: {_THREAD_LINK}"
            f"{WORKER_BRIEF_TASK_MARKER}fix the login page",
        )
    ]


def test_hand_to_project_ties_the_thread_to_the_project_awaiting_a_stream(
    tmp_path: Path,
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    reporter = RecordingReporter()

    _hand(terminal_host, thread_map, reporter)

    record = thread_map.get(_CONVERSATION)
    assert record is not None
    assert record.tied
    assert record.started_by_pan is False
    assert record.terminal_address == "t40"
    assert record.handoff == ProjectHandoff(
        project="atlas",
        project_root="t40",
        thread_link=_THREAD_LINK,
        orchestrator_turn=3,
        task="fix the login page",
    )
    (status,) = reporter.of_type(StatusChanged)
    assert (status.session_name, status.project, status.awaiting_handoff) == (
        "atlas",
        "atlas",
        True,
    )
    # Nothing about the orchestrator's terminal goes to the requester.
    assert reporter.of_type(PrivateDetails) == []


@pytest.mark.parametrize(
    "selector", ["atlas", "ATLAS", "t40", "atlas-orch"], ids=["label", "case", "address", "name"]
)
def test_hand_to_project_hands_to_a_project_named_exactly(tmp_path: Path, selector: str) -> None:
    terminal_host = _host(_terminals())

    _hand(terminal_host, _thread_map(tmp_path), RecordingReporter(), selector)

    assert [address for address, _text in terminal_host.sent] == ["t40"]


@pytest.mark.parametrize(
    "selector, extra_projects",
    [
        ("atlas-login", []),
        ("stream-login", []),
        ("t41", []),
        ("solo", []),
        ("atlas", [("atlas", ["t60"])]),
    ],
    ids=["not-a-project", "stream-name", "stream-address", "no-orchestrator", "shared-label"],
)
def test_hand_to_project_sends_nothing_without_one_exact_project(
    tmp_path: Path, selector: str, extra_projects: list[tuple[str, list[str]]]
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    reporter = RecordingReporter()

    with pytest.raises(NoExactMatchError):
        _hand(
            terminal_host,
            thread_map,
            reporter,
            selector,
            project_map_source=_map_source(*extra_projects),
        )

    assert terminal_host.sent == []
    assert thread_map.get(_CONVERSATION) is None
    assert reporter.events() == []


def test_hand_to_project_refuses_a_busy_orchestrator(tmp_path: Path) -> None:
    terminal_host = _host(_terminals(orchestrator=_orchestrator(agent_status=AgentStatus.WORKING)))
    thread_map = _thread_map(tmp_path)

    with pytest.raises(SessionBusyError):
        _hand(terminal_host, thread_map, RecordingReporter())

    assert terminal_host.sent == []
    assert thread_map.get(_CONVERSATION) is None


def test_check_on_a_thread_awaiting_handoff_posts_nothing_from_the_orchestrator(
    tmp_path: Path,
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    reporter = RecordingReporter()

    check_thread(
        conversation=_CONVERSATION,
        thread_map=thread_map,
        terminal_host=terminal_host,
        inbox_directory=FakeInboxDirectory(),
        reporter=reporter,
        clock=FixedClock(),
        screen_lines=30,
    )

    assert terminal_host.reads == []
    (status,) = reporter.events()
    assert isinstance(status, StatusChanged)
    assert status.awaiting_handoff is True
    assert reporter.of_type(AnswerReady) == reporter.of_type(ScreenCaptured) == []


def test_a_follow_up_while_awaiting_goes_to_the_orchestrator_again(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    terminal_host.add(_orchestrator(turn=4))
    thread_map.mark_handoff_nudged(_CONVERSATION, session_name="atlas-orch")

    follow_up_to_session(
        conversation=_CONVERSATION,
        text="also the signup page",
        terminal_host=terminal_host,
        thread_map=thread_map,
        reporter=RecordingReporter(),
        clock=FixedClock(),
    )

    address, typed = terminal_host.sent[-1]
    assert address == "t40"
    assert f"pan handoff --thread {_THREAD} --channel C0TEST --to <stream>" in typed
    assert typed.endswith(f"{WORKER_BRIEF_TASK_MARKER}also the signup page")
    record = thread_map.get(_CONVERSATION)
    assert record is not None
    assert record.handoff is not None
    # Watched anew: from the orchestrator's turn count now, not yet nudged.
    assert (record.handoff.orchestrator_turn, record.handoff.nudged) == (4, False)
    # The stream will get the whole request: the task, then the follow-up.
    assert record.handoff.task == "fix the login page\n\nalso the signup page"


@pytest.mark.parametrize(
    "orchestrator_now, expected_nudge",
    [
        (_orchestrator(turn=4), True),
        (_orchestrator(turn=4, agent_status=AgentStatus.DONE), True),
        (_orchestrator(turn=3), False),
        (_orchestrator(turn=4, agent_status=AgentStatus.WORKING), False),
        (_orchestrator(turn=5, agent_status=AgentStatus.BLOCKED), False),
        (_orchestrator(turn=None), False),
    ],
    ids=[
        "turn-ended-idle",
        "turn-ended-done",
        "not-started-yet",
        "still-working",
        "waiting-on-a-prompt",
        "no-turn-count",
    ],
)
def test_turn_end_without_a_handoff_posts_the_nudge(
    tmp_path: Path, orchestrator_now: Terminal | None, expected_nudge: bool
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    del terminal_host.terminals["t40"]
    if orchestrator_now is not None:
        terminal_host.add(orchestrator_now)

    reporter = _nudge(terminal_host, thread_map)

    assert reporter.events() == ([_NO_STREAM] if expected_nudge else [])


def test_the_nudge_is_posted_once(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    terminal_host.add(_orchestrator(turn=4))

    first = _nudge(terminal_host, thread_map)
    second = _nudge(terminal_host, thread_map)

    assert first.reports == [(_CONVERSATION, _NO_STREAM)]
    assert second.events() == []


@pytest.mark.parametrize(
    "selector", ["stream-login", "t41", "t40"], ids=["name", "address", "orch"]
)
def test_handoff_ties_the_thread_to_the_picked_session(tmp_path: Path, selector: str) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    reporter = RecordingReporter()

    handed = _handoff(terminal_host, thread_map, reporter, selector)

    record = thread_map.get(_CONVERSATION)
    assert record == handed
    assert record.tied
    assert record.awaiting_handoff is False
    assert record.handoff == ProjectHandoff(project="atlas", project_root="t40", awaiting=False)
    assert record.requester == "U000TEST"
    assert record.status_label == "fix the login page"
    status = reporter.of_type(StatusChanged)[-1]
    assert handoff_line(status.session_name, status.project, awaiting_handoff=False) == (
        f"Handed to {record.terminal_name} in atlas"
    )
    # Tied like any session now: no nudge, and a follow-up goes to it.
    terminal_host.add(_orchestrator(turn=4))
    assert _nudge(terminal_host, thread_map).events() == []


def test_after_a_handoff_follow_ups_go_to_the_stream(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    _handoff(terminal_host, thread_map, RecordingReporter(), "stream-login")

    follow_up_to_session(
        conversation=_CONVERSATION,
        text="and the tests",
        terminal_host=terminal_host,
        thread_map=thread_map,
        reporter=RecordingReporter(),
        clock=FixedClock(),
    )

    assert terminal_host.sent[-1][0] == "t41"


@pytest.mark.parametrize(
    "selector, handed_first, expected_error",
    [
        ("stream-login", False, NotAwaitingHandoffError),
        ("t99", True, HandoffTargetNotFoundError),
        ("dev-shell", True, HandoffTargetNotFoundError),
        ("atlas / 1", True, HandoffTargetNotFoundError),
        ("atlas", True, HandoffTargetNotFoundError),
        ("t51", True, TargetInOtherProjectError),
        ("solo", True, TargetInOtherProjectError),
    ],
    ids=[
        "not-awaiting",
        "not-found",
        "not-an-agent-session",
        "herdr-label-is-not-a-name",
        "herdr-space-is-not-a-name",
        "other-project",
        "standalone",
    ],
)
def test_handoff_refuses_with_nothing_tied_or_posted(
    tmp_path: Path, selector: str, handed_first: bool, expected_error: type[PanError]
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    if handed_first:
        _hand(terminal_host, thread_map, RecordingReporter())
    before = thread_map.records()
    reporter = RecordingReporter()

    with pytest.raises(expected_error):
        _handoff(terminal_host, thread_map, reporter, selector)

    assert thread_map.records() == before
    assert reporter.events() == []


def test_handoff_refuses_a_thread_already_handed_on(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    _handoff(terminal_host, thread_map, RecordingReporter(), "stream-login")

    with pytest.raises(NotAwaitingHandoffError):
        _handoff(terminal_host, thread_map, RecordingReporter(), "stream-docs")


def test_threads_handed_to_one_project_and_a_send_to_its_orchestrator_stay_apart(
    tmp_path: Path,
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    third = Conversation.slack("1700000000.000300", "C0TEST")
    _hand(terminal_host, thread_map, RecordingReporter(), conversation=third)

    sent = send_to_session(
        conversation=_OTHER_CONVERSATION,
        selector="atlas-orch",
        text="what is left?",
        requester="U000TEST",
        reply_in_slack=False,
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=FakeInboxDirectory(),
        reporter=RecordingReporter(),
        clock=FixedClock(),
    )

    assert sent.handoff is None
    for conversation in (_CONVERSATION, third):
        record = thread_map.get(conversation)
        assert record is not None
        assert record.tied
        assert record.awaiting_handoff


def test_exact_send_to_a_stream_is_unchanged(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)

    record = send_to_session(
        conversation=_CONVERSATION,
        selector="t41",
        text="fix it",
        requester="U000TEST",
        reply_in_slack=False,
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=FakeInboxDirectory(),
        reporter=RecordingReporter(),
        clock=FixedClock(),
    )

    assert [address for address, _text in terminal_host.sent] == ["t41"]
    assert record.handoff is None
    assert record.terminal_address == "t41"


def test_the_sessions_list_does_not_join_a_waiting_thread_to_the_orchestrator(
    tmp_path: Path,
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())

    summaries = collect_sessions(terminal_host, thread_map, project_map_source=_map_source())

    (orchestrator,) = [summary for summary in summaries if summary.address == "t40"]
    assert orchestrator.thread_ts is None


def test_propose_a_project_then_confirm_hands_it_over(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    confirmations = FilePendingConfirmations(tmp_path / "confirmations.json")
    reporter = RecordingReporter()

    pending = propose_sessions(
        conversation=_CONVERSATION,
        selector="atlass",
        task="fix the login page",
        requester="U000TEST",
        reply_in_slack=False,
        candidate_addresses=[],
        project_addresses=["t40", "t41"],
        terminal_host=terminal_host,
        confirmations=confirmations,
        reporter=reporter,
        clock=FixedClock(),
        project_map_source=_map_source(),
    )

    # A stream is not a project: only the orchestrator's address is kept.
    assert pending is not None
    assert [candidate.project for candidate in pending.candidates] == ["atlas"]
    assert reporter.of_type(QuestionAsked) == [
        QuestionAsked(candidate_labels=["the atlas project"])
    ]
    assert terminal_host.sent == []

    confirmations.mark_answered(_CONVERSATION, _NOW, "yes")
    confirm_session(
        conversation=_CONVERSATION,
        choice="yes",
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=FakeInboxDirectory(),
        confirmations=confirmations,
        reporter=reporter,
        clock=FixedClock(),
        project_map_source=_map_source(),
        thread_link=lambda: _THREAD_LINK,
    )

    (address, typed) = terminal_host.sent[0]
    assert address == "t40"
    assert f"Slack thread: {_THREAD_LINK}" in typed
    record = thread_map.get(_CONVERSATION)
    assert record is not None
    assert record.awaiting_handoff
    assert confirmations.get(_CONVERSATION) is None


@pytest.mark.parametrize(
    "project, awaiting, expected",
    [
        (None, False, None),
        ("atlas", True, "Handed to atlas"),
        ("atlas", False, "Handed to stream-login in atlas"),
    ],
    ids=["no-project", "awaiting", "handed-on"],
)
def test_handoff_line(project: str | None, awaiting: bool, expected: str | None) -> None:
    assert handoff_line("stream-login", project, awaiting_handoff=awaiting) == expected


# Review regressions.


def _streams_gone(terminal_host: FakeTerminalHost) -> FakeProjectMapSource:
    # atlas's streams have ended: t40 no longer roots a project.
    for address in ("t41", "t42", "t61"):
        del terminal_host.terminals[address]
    return FakeProjectMapSource(projects=[("borealis", ["t50", "t51"])])


def test_a_follow_up_reaches_the_orchestrator_after_its_last_stream_ends(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    _streams_gone(terminal_host)

    follow_up_to_session(
        conversation=_CONVERSATION,
        text="also the signup page",
        terminal_host=terminal_host,
        thread_map=thread_map,
        reporter=RecordingReporter(),
        clock=FixedClock(),
    )

    assert terminal_host.sent[-1][0] == "t40"


@pytest.mark.parametrize("selector", ["t40", "atlas-orch"], ids=["address", "name"])
def test_handoff_to_the_orchestrator_itself_works_after_its_last_stream_ends(
    tmp_path: Path, selector: str
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    project_map_source = _streams_gone(terminal_host)

    record = _handoff(terminal_host, thread_map, RecordingReporter(), selector, project_map_source)

    assert (record.terminal_address, record.tied, record.awaiting_handoff) == ("t40", True, False)


@pytest.mark.parametrize(
    "selector, expected_session",
    [("stream-login", "stream-login"), ("atlas-orch", "atlas-orch")],
    ids=["a-stream", "the-orchestrator"],
)
def test_to_a_session_from_a_waiting_thread_edits_its_one_status_message_and_ends_the_wait(
    tmp_path: Path, selector: str, expected_session: str
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    handed = _hand(terminal_host, thread_map, RecordingReporter())
    reporter = RecordingReporter()

    send_to_session(
        conversation=_CONVERSATION,
        selector=selector,
        text="fix it",
        requester="U000TEST",
        reply_in_slack=False,
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=FakeInboxDirectory(),
        reporter=reporter,
        clock=FixedClock(),
    )

    (status,) = reporter.of_type(StatusChanged)
    assert status.status_message_id == handed.status_message_ts
    in_thread = [
        record for record in thread_map.records() if record.conversation.key == _CONVERSATION.key
    ]
    assert [record.terminal_name for record in in_thread if record.tied] == [expected_session]
    assert not any(record.awaiting_handoff for record in in_thread)


def test_handing_to_an_orchestrator_keeps_the_threads_own_record_of_it(tmp_path: Path) -> None:
    # The thread already belongs to pan's worker atlas-orch, which then started streams.
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    worker = ThreadRecord(
        conversation=_CONVERSATION,
        terminal_name="atlas-orch",
        terminal_address="t40",
        worktree_path=Path("/tmp/atlas"),
        status=WorkerStatus.IDLE,
        created_at=_NOW,
        updated_at=_NOW,
        started_by_pan=True,
        inbox_thread_name="atlas-orch: plan the release",
        last_answer_digest="digest-1",
        status_message_ts="status-0",
    )
    thread_map.tie(worker)

    record = _hand(terminal_host, thread_map, RecordingReporter())

    assert record.awaiting_handoff
    assert (
        record.started_by_pan,
        record.inbox_thread_name,
        record.last_answer_digest,
        record.created_at,
        record.status_message_ts,
    ) == (True, "atlas-orch: plan the release", "digest-1", _NOW, "status-0")
    assert len(thread_map.records()) == 1


def _ended_by_follow_up(
    terminal_host: FakeTerminalHost, thread_map: FileThreadMap, reporter: RecordingReporter
) -> None:
    with pytest.raises(TerminalGoneError):
        follow_up_to_session(
            conversation=_CONVERSATION,
            text="any news?",
            terminal_host=terminal_host,
            thread_map=thread_map,
            reporter=reporter,
            clock=FixedClock(),
        )


def _ended_by_check(
    terminal_host: FakeTerminalHost, thread_map: FileThreadMap, reporter: RecordingReporter
) -> None:
    check_thread(
        conversation=_CONVERSATION,
        thread_map=thread_map,
        terminal_host=terminal_host,
        inbox_directory=FakeInboxDirectory(),
        reporter=reporter,
        clock=FixedClock(),
        screen_lines=30,
    )


def _ended_by_sweep(
    terminal_host: FakeTerminalHost, thread_map: FileThreadMap, reporter: RecordingReporter
) -> None:
    nudge_unhanded_threads(
        thread_map=thread_map,
        terminal_host=terminal_host,
        open_reporter=lambda: reporter,
        clock=FixedClock(),
    )


_ORCHESTRATOR_ENDED = SessionNotice(
    reason=SessionNoticeReason.PROJECT_ORCHESTRATOR_ENDED, session_name="atlas"
)


@pytest.mark.parametrize("finished", [False, True], ids=["gone", "finished"])
@pytest.mark.parametrize(
    "noticed_by",
    [_ended_by_follow_up, _ended_by_check, _ended_by_sweep],
    ids=["follow-up", "check", "sweep"],
)
def test_an_ended_orchestrator_ends_the_wait_once(
    tmp_path: Path, noticed_by: object, finished: bool
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    del terminal_host.terminals["t40"]
    if finished:
        terminal_host.add(_orchestrator(state=TerminalState.FINISHED))
    reporter = RecordingReporter()

    noticed_by(terminal_host, thread_map, reporter)  # type: ignore[operator]

    assert reporter.states() == [StatusState.STOPPED]
    assert reporter.of_type(SessionNotice) == [_ORCHESTRATOR_ENDED]
    record = thread_map.get(_CONVERSATION)
    assert record is not None
    assert (record.tied, record.handoff) == (False, None)
    # Once: the thread no longer waits, so the sweep leaves it alone and a reply finds it untied.
    later = RecordingReporter()
    _ended_by_sweep(terminal_host, thread_map, later)
    with pytest.raises(ThreadNotFoundError):
        follow_up_to_session(
            conversation=_CONVERSATION,
            text="hello?",
            terminal_host=terminal_host,
            thread_map=thread_map,
            reporter=later,
            clock=FixedClock(),
        )
    assert later.of_type(SessionNotice) == []


def test_a_failed_nudge_is_tried_again_by_the_next_sweep(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    terminal_host.add(_orchestrator(turn=4))
    reporter = RecordingReporter()
    reporter.error = SlackPostError("not delivered")

    def sweep() -> list[Conversation]:
        return nudge_unhanded_threads(
            thread_map=thread_map,
            terminal_host=terminal_host,
            open_reporter=lambda: reporter,
            clock=FixedClock(),
        )

    assert sweep() == []
    record = thread_map.get(_CONVERSATION)
    assert record is not None
    assert record.handoff is not None
    assert record.handoff.nudged is False
    reporter.error = None
    assert sweep() == [_CONVERSATION]
    assert reporter.events() == [_NO_STREAM]


@pytest.mark.parametrize(
    "selector, expected_lookups",
    [("atlas", 1), ("atlass", 0)],
    ids=["project-found", "no-project"],
)
def test_the_thread_link_is_looked_up_only_once_a_project_is_found(
    tmp_path: Path, selector: str, expected_lookups: int
) -> None:
    lookups: list[str] = []

    def thread_link() -> str:
        lookups.append("permalink")
        return _THREAD_LINK

    with contextlib.suppress(NoExactMatchError):
        hand_to_project(
            conversation=_CONVERSATION,
            selector=selector,
            text="fix the login page",
            requester="U000TEST",
            reply_in_slack=False,
            thread_link=thread_link,
            terminal_host=_host(_terminals()),
            thread_map=_thread_map(tmp_path),
            reporter=RecordingReporter(),
            clock=FixedClock(),
            project_map_source=_map_source(),
        )

    assert len(lookups) == expected_lookups


# The handoff delivers the task.


@pytest.mark.parametrize(
    "note, expected_body",
    [
        (None, f"fix the login page\n\nSlack thread: {_THREAD_LINK}"),
        (
            "use the staging database",
            "fix the login page\n\nFrom the atlas orchestrator: use the staging database"
            f"\n\nSlack thread: {_THREAD_LINK}",
        ),
    ],
    ids=["task-only", "with-note"],
)
def test_handoff_types_the_task_into_the_stream_once(
    tmp_path: Path, note: str | None, expected_body: str
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    handed_text = list(terminal_host.sent)

    _handoff(terminal_host, thread_map, RecordingReporter(), "stream-login", note=note)

    # Exactly what `to stream-login` would type for this task: the usual "from Slack" line,
    # then the task, the note and the thread's link.
    assert terminal_host.sent == [
        *handed_text,
        (
            "t41",
            from_slack_text(
                inbox_thread_name("stream-login", expected_body),
                expected_body,
                ReplyMode.ON_DEMAND,
                thread_known=False,
            ),
        ),
    ]


def test_handoff_to_the_orchestrator_itself_types_nothing(tmp_path: Path) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    handed_text = list(terminal_host.sent)

    record = _handoff(terminal_host, thread_map, RecordingReporter(), "atlas-orch", note="mine")

    assert terminal_host.sent == handed_text
    assert (record.terminal_name, record.tied, record.awaiting_handoff) == (
        "atlas-orch",
        True,
        False,
    )
    assert len(thread_map.records()) == 1


@pytest.mark.parametrize(
    "agent_status, expected_error, expected_reason",
    [
        (AgentStatus.WORKING, SessionBusyError, NotDeliveredReason.BUSY),
        (AgentStatus.BLOCKED, TargetBlockedError, NotDeliveredReason.WAITING_ON_PROMPT),
    ],
    ids=["busy", "waiting-on-a-prompt"],
)
def test_a_busy_stream_is_refused_as_a_send_is_and_the_thread_keeps_waiting(
    tmp_path: Path,
    agent_status: AgentStatus,
    expected_error: type[PanError],
    expected_reason: NotDeliveredReason,
) -> None:
    terminal_host = _host(_terminals())
    thread_map = _thread_map(tmp_path)
    _hand(terminal_host, thread_map, RecordingReporter())
    busy_stream = make_terminal(
        "t41",
        name="stream-login",
        parent="t40",
        cwd=Path("/tmp/atlas-login"),
        agent_status=agent_status,
    )
    terminal_host.add(busy_stream)
    handed_text = list(terminal_host.sent)
    reporter = RecordingReporter()

    with pytest.raises(expected_error):
        _handoff(terminal_host, thread_map, reporter, "stream-login")

    assert terminal_host.sent == handed_text
    assert reporter.events() == [NotDelivered(reason=expected_reason, session_name="stream-login")]
    record = thread_map.get(_CONVERSATION)
    assert record is not None
    assert (record.terminal_name, record.awaiting_handoff) == ("atlas-orch", True)
