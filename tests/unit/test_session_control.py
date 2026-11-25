from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from git_worktree_fakes import FakeGitWorktree
from inbox_fakes import FakeInboxDirectory
from reporter_fakes import RecordingReporter
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.backend.interface import INCOMPLETE_READ_NOTICE
from pan.backend.session_control import (
    check_thread,
    clean_screen_text,
    done_thread,
    follow_up_to_session,
    forget_thread,
    from_slack_text,
    send_to_session,
    stop_thread,
)
from pan.backend.spawn import RELAYED_FOLLOW_UP_SENTENCE
from pan.backend.status_message import answer_digest
from pan.core.errors import (
    CredentialsError,
    GitWorktreeError,
    NoExactMatchError,
    NotPanSessionError,
    PanError,
    SessionBusyError,
    TargetBlockedError,
    TerminalGoneError,
    ThreadNotFoundError,
    UnsafeWorktreeError,
    UnsavedWorkError,
)
from pan.core.events import (
    AnswerReady,
    CleanupFailed,
    Event,
    NotDelivered,
    NotDeliveredReason,
    PrivateDetails,
    ScreenCaptured,
    SessionClosed,
    SessionNotice,
    SessionNoticeReason,
    StatusChanged,
    ThreadNotice,
    ThreadNoticeReason,
    WorkKept,
)
from pan.core.models import (
    AgentStatus,
    BranchStatus,
    Conversation,
    InboxThread,
    ReplyMode,
    StatusState,
    TerminalRead,
    ThreadRecord,
    WorkerStatus,
    WorktreeStatus,
)
from pan.data.threadmap import FileThreadMap
from pan.frontend.hooks.stop import stop_hook

_FIXED_NOW = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)
_OLD_THREAD = "1700000000.000100"
_NEW_THREAD = "1700000000.000200"
_OLD_CONVERSATION = Conversation.slack(_OLD_THREAD, "C0TEST")
_NEW_CONVERSATION = Conversation.slack(_NEW_THREAD, "C0TEST")
_NOT_TIED = ThreadNotice(reason=ThreadNoticeReason.THREAD_NOT_TIED)


def _line_events(reporter: RecordingReporter) -> list[Event]:
    # What was reported besides the status and the private details: answers and notices.
    return [
        event
        for event in reporter.events()
        if not isinstance(event, StatusChanged | PrivateDetails)
    ]


class FixedClock:
    def now(self) -> datetime:
        return _FIXED_NOW


def _record(
    conversation: Conversation = _OLD_CONVERSATION,
    *,
    terminal_name: str = "pan-7",
    terminal_address: str = "t7",
    started_by_pan: bool = True,
    **updates: object,
) -> ThreadRecord:
    record = ThreadRecord(
        conversation=conversation,
        terminal_name=terminal_name,
        terminal_address=terminal_address,
        worktree_path=Path("/tmp/wt/pan-7"),
        status=WorkerStatus.IDLE,
        created_at=_FIXED_NOW,
        updated_at=_FIXED_NOW,
        started_by_pan=started_by_pan,
        inbox_thread_name="pan-7: fix the login page",
    )
    return record.model_copy(update=updates)


def _thread_map(tmp_path: Path, *records: ThreadRecord) -> FileThreadMap:
    thread_map = FileThreadMap(tmp_path / "threads.json", FixedClock())
    for record in records:
        thread_map.put(record)
    return thread_map


def _host(*terminals: object) -> FakeTerminalHost:
    terminal_host = FakeTerminalHost()
    for terminal in terminals:
        terminal_host.add(terminal)  # type: ignore[arg-type]
    return terminal_host


def _send(
    terminal_host: FakeTerminalHost,
    thread_map: FileThreadMap,
    reporter: RecordingReporter,
    selector: str = "pan-7",
    *,
    reply_in_slack: bool = False,
    inbox_directory: FakeInboxDirectory | None = None,
) -> ThreadRecord:
    return send_to_session(
        conversation=_NEW_CONVERSATION,
        selector=selector,
        text="add the tests",
        requester="U0TEST",
        reply_in_slack=reply_in_slack,
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=inbox_directory if inbox_directory is not None else FakeInboxDirectory(),
        reporter=reporter,
        clock=FixedClock(),
    )


@pytest.mark.parametrize(
    "reply_in_slack, inbox_url",
    [(False, None), (True, "http://localhost:19441/#thread=abc")],
    ids=["inbox-name", "inbox-link-and-slack-replies"],
)
def test_send_to_session_sends_ties_and_reports_a_status(
    tmp_path: Path, reply_in_slack: bool, inbox_url: str | None
) -> None:
    thread_map = _thread_map(tmp_path, _record(inbox_thread_url=inbox_url))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    reporter = RecordingReporter()

    _send(terminal_host, thread_map, reporter, reply_in_slack=reply_in_slack)

    assert terminal_host.sent == [
        (
            "t7",
            from_slack_text(
                "pan-7: fix the login page",
                "add the tests",
                ReplyMode.SLACK if reply_in_slack else ReplyMode.ON_DEMAND,
            ),
        )
    ]
    tied = thread_map.get(_NEW_CONVERSATION)
    assert tied is not None
    assert tied.tied is True
    assert tied.terminal_name == "pan-7"
    assert tied.requester == "U0TEST"
    assert tied.reply_mode is (ReplyMode.SLACK if reply_in_slack else ReplyMode.ON_DEMAND)
    old = thread_map.get(_OLD_CONVERSATION)
    assert old is not None
    assert old.tied is False
    # One status in the new thread, nothing else in it; the Inbox link (on this machine) and
    # the terminal go only to the requester.
    assert reporter.reports == [
        (
            _NEW_CONVERSATION,
            StatusChanged(
                state=StatusState.WORKING,
                session_name="pan-7",
                label="add the tests",
                task_started_at=_FIXED_NOW,
                updated_at=_FIXED_NOW,
                replies_in_thread=reply_in_slack,
            ),
        ),
        (
            _NEW_CONVERSATION,
            PrivateDetails(
                requester="U0TEST",
                session_name="pan-7",
                terminal_address="t7",
                inbox_thread_name="pan-7: fix the login page",
                inbox_thread_url=inbox_url,
                inbox_thread_known=True,
            ),
        ),
    ]
    assert tied.status_message_ts == "status-1"
    assert tied.status_label == "add the tests"


def test_send_to_a_session_from_another_thread_never_edits_that_threads_status(
    tmp_path: Path,
) -> None:
    thread_map = _thread_map(tmp_path, _record(status_message_ts="1700000000.000900"))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    reporter = RecordingReporter()

    tied = _send(terminal_host, thread_map, reporter)

    # A new status in the new thread: the old thread's message is never named.
    assert [
        (conversation, event.status_message_id)
        for conversation, event in reporter.reports
        if isinstance(event, StatusChanged)
    ] == [(_NEW_CONVERSATION, None)]
    assert tied.status_message_ts == "status-1"


@pytest.mark.parametrize(
    "selector, terminals, records, expected_error, expected_event",
    [
        (
            "pan-7",
            [make_terminal("t7", name="pan-7", agent_status=AgentStatus.WORKING)],
            [_record()],
            SessionBusyError,
            NotDelivered(reason=NotDeliveredReason.BUSY, session_name="pan-7"),
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent_status=AgentStatus.WORKING)],
            [],
            SessionBusyError,
            NotDelivered(reason=NotDeliveredReason.BUSY, session_name="desk-api"),
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent_status=AgentStatus.NONE, turn=4)],
            [],
            SessionBusyError,
            NotDelivered(reason=NotDeliveredReason.BUSY, session_name="desk-api"),
        ),
        (
            "desk-api",
            [
                make_terminal(
                    "t9", name="desk-api", agent="zsh", agent_status=AgentStatus.NONE, turn=0
                )
            ],
            [],
            SessionBusyError,
            NotDelivered(reason=NotDeliveredReason.BUSY, session_name="desk-api"),
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent=None, agent_status=AgentStatus.NONE)],
            [],
            SessionBusyError,
            NotDelivered(reason=NotDeliveredReason.BUSY, session_name="desk-api"),
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent_status=AgentStatus.UNKNOWN)],
            [],
            SessionBusyError,
            NotDelivered(reason=NotDeliveredReason.BUSY, session_name="desk-api"),
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent_status=AgentStatus.BLOCKED)],
            [],
            TargetBlockedError,
            NotDelivered(reason=NotDeliveredReason.WAITING_ON_PROMPT, session_name="desk-api"),
        ),
    ],
    ids=[
        "busy-pan-session",
        "busy-desk-session",
        "no-status-after-a-turn-counts-as-busy",
        "shell-with-no-status-counts-as-busy",
        "unknown-program-with-no-status-counts-as-busy",
        "unknown-status-counts-as-busy",
        "blocked-desk-session",
    ],
)
def test_send_to_session_refuses_and_sends_nothing(
    tmp_path: Path,
    selector: str,
    terminals: list[object],
    records: list[ThreadRecord],
    expected_error: type[PanError],
    expected_event: NotDelivered,
) -> None:
    thread_map = _thread_map(tmp_path, *records)
    terminal_host = _host(*terminals)
    reporter = RecordingReporter()

    with pytest.raises(expected_error):
        _send(terminal_host, thread_map, reporter, selector)

    assert terminal_host.sent == []
    assert thread_map.get(_NEW_CONVERSATION) is None
    assert reporter.events() == [expected_event]


@pytest.mark.parametrize(
    "agent, turn",
    [("claude", 0), ("claude-code", 0), ("claude", None)],
    ids=["claude-turn-0", "claude-code-turn-0", "claude-no-turn-count"],
)
def test_send_to_a_brand_new_claude_session_with_no_status_types_the_task(
    tmp_path: Path, agent: str, turn: int | None
) -> None:
    thread_map = _thread_map(tmp_path)
    terminal_host = _host(
        make_terminal("t9", name="desk-api", agent=agent, agent_status=AgentStatus.NONE, turn=turn)
    )
    reporter = RecordingReporter()

    _send(terminal_host, thread_map, reporter, "desk-api")

    assert [address for address, _ in terminal_host.sent] == ["t9"]


def test_send_to_a_desk_session_ties_it_with_a_from_slack_line(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path)
    desk_folder = Path("/tmp/desk/api")
    terminal_host = _host(make_terminal("t9", name="desk-api", cwd=desk_folder))
    reporter = RecordingReporter()

    _send(terminal_host, thread_map, reporter, "desk-api")

    assert terminal_host.sent == [
        (
            "t9",
            from_slack_text(
                "desk-api: add the tests",
                "add the tests",
                ReplyMode.ON_DEMAND,
                thread_known=False,
            ),
        )
    ]
    assert terminal_host.sent[0][1].startswith("From Slack via pan.")
    assert "existing Plannotator Inbox thread" in terminal_host.sent[0][1]
    tied = thread_map.get(_NEW_CONVERSATION)
    assert tied is not None
    assert tied.tied is True
    assert tied.started_by_pan is False
    assert tied.terminal_name == "desk-api"
    assert tied.terminal_address == "t9"
    assert tied.worktree_path == desk_folder
    assert tied.inbox_thread_name == "desk-api: add the tests"
    assert _line_events(reporter) == []
    assert reporter.states() == [StatusState.WORKING]
    ((details),) = reporter.of_type(PrivateDetails)
    assert (details.inbox_thread_name, details.inbox_thread_known) == (
        "desk-api: add the tests",
        False,
    )


@pytest.mark.parametrize(
    "started_by_pan, inbox_thread_id, expected_thread_known",
    [(True, None, True), (False, "thr_9", True), (False, None, False)],
    ids=["pan-worker", "desk-session-with-a-found-thread", "desk-session-thread-unknown"],
)
def test_send_to_a_session_whose_record_has_no_thread_name_uses_the_shared_known_rule(
    tmp_path: Path,
    started_by_pan: bool,
    inbox_thread_id: str | None,
    expected_thread_known: bool,
) -> None:
    earlier = _record(
        terminal_name="desk-api",
        terminal_address="t9",
        started_by_pan=started_by_pan,
        inbox_thread_name=None,
        inbox_thread_id=inbox_thread_id,
    )
    thread_map = _thread_map(tmp_path, earlier)
    terminal_host = _host(make_terminal("t9", name="desk-api"))

    _send(terminal_host, thread_map, RecordingReporter(), "desk-api")

    assert terminal_host.sent == [
        (
            "t9",
            from_slack_text(
                "desk-api: add the tests",
                "add the tests",
                ReplyMode.ON_DEMAND,
                thread_known=expected_thread_known,
            ),
        )
    ]


def test_send_to_a_desk_session_again_keeps_its_inbox_thread_and_shows_the_found_link(
    tmp_path: Path,
) -> None:
    desk_folder = Path("/tmp/desk/api")
    earlier = _record(
        terminal_name="desk-api",
        terminal_address="t9",
        started_by_pan=False,
        worktree_path=desk_folder,
        inbox_thread_name="desk-api: fix the build",
    )
    thread_map = _thread_map(tmp_path, earlier)
    terminal_host = _host(make_terminal("t9", name="desk-api", cwd=desk_folder))
    inbox_directory = FakeInboxDirectory(
        {
            (desk_folder, "desk-api: fix the build"): InboxThread(
                thread_id="thr_9",
                name="desk-api: fix the build",
                url="https://inbox.example/threads/thr_9",
            )
        }
    )
    reporter = RecordingReporter()

    _send(terminal_host, thread_map, reporter, "desk-api", inbox_directory=inbox_directory)

    assert terminal_host.sent == [
        ("t9", from_slack_text("desk-api: fix the build", "add the tests", ReplyMode.ON_DEMAND))
    ]
    tied = thread_map.get(_NEW_CONVERSATION)
    assert tied is not None
    assert tied.inbox_thread_id == "thr_9"
    assert tied.inbox_thread_url == "https://inbox.example/threads/thr_9"
    old = thread_map.get(_OLD_CONVERSATION)
    assert old is not None
    assert old.tied is False
    assert _line_events(reporter) == []
    ((details),) = reporter.of_type(PrivateDetails)
    assert (details.inbox_thread_name, details.inbox_thread_url, details.inbox_thread_known) == (
        "desk-api: fix the build",
        "https://inbox.example/threads/thr_9",
        True,
    )


@pytest.mark.parametrize(
    "earlier, terminal, expected_inbox_thread_name, expected_thread_known",
    [
        (
            _record(terminal_name="desk-test", terminal_address="", started_by_pan=False),
            make_terminal("t10", name="desk-test"),
            "pan-7: fix the login page",
            False,
        ),
        (
            _record(terminal_name="desk-test", terminal_address="t9", started_by_pan=False),
            make_terminal("t10", name="desk-test"),
            "desk-test: add the tests",
            False,
        ),
        (
            _record(terminal_name="desk-test", terminal_address="t10", started_by_pan=False),
            make_terminal("t10", name="desk-test"),
            "pan-7: fix the login page",
            False,
        ),
        (
            _record(terminal_name="desk-old", terminal_address="t10", started_by_pan=False),
            make_terminal("t10", name="desk-test"),
            "desk-test: add the tests",
            False,
        ),
        (
            _record(terminal_name="desk-test", terminal_address="t9"),
            make_terminal("t10", name="desk-test", cwd=Path("/tmp/wt/pan-7")),
            "pan-7: fix the login page",
            True,
        ),
        (
            _record(terminal_name="desk-test", terminal_address="t9"),
            make_terminal("t10", name="desk-test", cwd=Path("/tmp/elsewhere")),
            "desk-test: add the tests",
            False,
        ),
    ],
    ids=[
        "legacy-record-without-address-joins-by-name",
        "stale-record-of-a-killed-terminal",
        "same-address-and-name",
        "address-reused-by-another-name",
        "pan-worker-back-at-a-new-address-in-its-worktree",
        "pan-worker-name-outside-its-worktree",
    ],
)
def test_send_joins_a_session_record_only_to_its_own_terminal(
    tmp_path: Path,
    earlier: ThreadRecord,
    terminal: object,
    expected_inbox_thread_name: str,
    expected_thread_known: bool,
) -> None:
    # A joined record keeps its Inbox thread name; a new session gets one of its own.
    thread_map = _thread_map(tmp_path, earlier)
    terminal_host = _host(terminal)

    _send(terminal_host, thread_map, RecordingReporter(), "desk-test")

    assert terminal_host.sent == [
        (
            "t10",
            from_slack_text(
                expected_inbox_thread_name,
                "add the tests",
                ReplyMode.ON_DEMAND,
                thread_known=expected_thread_known,
            ),
        )
    ]
    tied = thread_map.get(_NEW_CONVERSATION)
    assert tied is not None
    assert tied.terminal_address == "t10"
    assert tied.inbox_thread_name == expected_inbox_thread_name


def test_send_to_a_blocked_session_types_nothing_and_ties_nothing(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    terminal_host.errors["send"] = TargetBlockedError("blocked")
    reporter = RecordingReporter()

    with pytest.raises(TargetBlockedError):
        _send(terminal_host, thread_map, reporter)

    assert thread_map.get(_NEW_CONVERSATION) is None
    assert reporter.events() == [
        NotDelivered(reason=NotDeliveredReason.WAITING_ON_PROMPT, session_name="pan-7")
    ]


@pytest.mark.parametrize(
    "records, expected_event, expect_untied",
    [
        (
            [_record()],
            SessionNotice(reason=SessionNoticeReason.THREAD_UNTIED, session_name="pan-7"),
            True,
        ),
        ([_record(tied=False)], _NOT_TIED, True),
        ([], _NOT_TIED, False),
    ],
    ids=["tied", "already-untied", "no-record"],
)
def test_forget_thread(
    tmp_path: Path, records: list[ThreadRecord], expected_event: Event, expect_untied: bool
) -> None:
    thread_map = _thread_map(tmp_path, *records)
    reporter = RecordingReporter()

    forget_thread(conversation=_OLD_CONVERSATION, thread_map=thread_map, reporter=reporter)

    assert reporter.reports == [(_OLD_CONVERSATION, expected_event)]
    record = thread_map.get(_OLD_CONVERSATION)
    assert (record is not None and record.tied is False) is expect_untied


def _transcript_answering(tmp_path: Path, answer: str) -> Path:
    transcript = tmp_path / "session.jsonl"
    entries = [
        {"type": "user", "message": {"content": "fix the login page"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}},
    ]
    transcript.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")
    return transcript


def _check(
    thread_map: FileThreadMap,
    terminal_host: FakeTerminalHost,
    reporter: RecordingReporter,
    inbox_directory: FakeInboxDirectory | None = None,
) -> None:
    check_thread(
        conversation=_OLD_CONVERSATION,
        thread_map=thread_map,
        terminal_host=terminal_host,
        inbox_directory=inbox_directory if inbox_directory is not None else FakeInboxDirectory(),
        reporter=reporter,
        clock=FixedClock(),
        screen_lines=50,
    )


@pytest.mark.parametrize(
    "live_terminal_name, expected_state",
    [("pan-7", StatusState.FINISHED), ("pan-8", StatusState.STOPPED)],
    ids=["same-terminal", "address-reused-by-another-name"],
)
def test_check_finds_the_session_terminal_by_address_only_when_names_agree(
    tmp_path: Path, live_terminal_name: str, expected_state: StatusState
) -> None:
    thread_map = _thread_map(
        tmp_path, _record(transcript_path=_transcript_answering(tmp_path, "Done."))
    )
    terminal_host = _host(make_terminal("t7", name=live_terminal_name))
    reporter = RecordingReporter()

    _check(thread_map, terminal_host, reporter)

    assert reporter.states() == [expected_state]


@pytest.mark.parametrize(
    "agent_status, expected_state",
    [
        (AgentStatus.IDLE, StatusState.FINISHED),
        (AgentStatus.WORKING, StatusState.WORKING),
        (AgentStatus.BLOCKED, StatusState.WAITING),
    ],
    ids=["idle", "working", "blocked"],
)
def test_check_edits_the_status_reports_the_answer_and_the_link_privately(
    tmp_path: Path, agent_status: AgentStatus, expected_state: StatusState
) -> None:
    record = _record(
        transcript_path=_transcript_answering(tmp_path, "Fixed the **login** page."),
        inbox_thread_url="http://localhost:19441/#thread=abc",
        requester="U0TEST",
        status_message_ts="1700000000.000900",
        status_label="fix the login page",
    )
    thread_map = _thread_map(tmp_path, record)
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=agent_status))
    reporter = RecordingReporter()

    _check(thread_map, terminal_host, reporter)

    # The answer alone, as the transcript has it: no session name, state or Inbox link.
    assert reporter.events() == [
        StatusChanged(
            state=expected_state,
            session_name="pan-7",
            label="fix the login page",
            updated_at=_FIXED_NOW,
            status_message_id="1700000000.000900",
        ),
        AnswerReady(text="Fixed the **login** page."),
        PrivateDetails(
            requester="U0TEST",
            session_name="pan-7",
            terminal_address="t7",
            inbox_thread_name="pan-7: fix the login page",
            inbox_thread_url="http://localhost:19441/#thread=abc",
            inbox_thread_known=True,
        ),
    ]
    assert terminal_host.reads == []


def test_check_without_a_transcript_posts_screen_text(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    terminal_host.read_result = TerminalRead(text="last screen lines", complete=True)
    reporter = RecordingReporter()

    _check(thread_map, terminal_host, reporter)

    assert _line_events(reporter) == [ScreenCaptured(text="last screen lines")]
    assert terminal_host.reads == [("t7", 50)]


def test_check_on_an_ended_session_with_no_answer_only_updates_the_status(
    tmp_path: Path,
) -> None:
    thread_map = _thread_map(tmp_path, _record(inbox_thread_name=None, requester="U0TEST"))
    reporter = RecordingReporter()

    _check(thread_map, _host(), reporter)

    assert _line_events(reporter) == []
    assert reporter.states() == [StatusState.STOPPED]
    assert reporter.of_type(PrivateDetails) == [
        PrivateDetails(
            requester="U0TEST",
            session_name="pan-7",
            terminal_address="t7",
            inbox_thread_known=True,
        )
    ]


@pytest.mark.parametrize("records", [[], [_record(tied=False)]], ids=["no-record", "untied"])
def test_check_on_an_untied_thread_replies_one_line(
    tmp_path: Path, records: list[ThreadRecord]
) -> None:
    thread_map = _thread_map(tmp_path, *records)
    reporter = RecordingReporter()

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), reporter)

    assert reporter.reports == [(_OLD_CONVERSATION, _NOT_TIED)]


@pytest.mark.parametrize(
    "found, expected_url, expected_known",
    [
        (InboxThread(thread_id="thr_9", name="desk-api: fix the build", url=None), None, True),
        (
            InboxThread(
                thread_id="thr_9",
                name="desk-api: fix the build",
                url="https://inbox.example/threads/thr_9",
            ),
            "https://inbox.example/threads/thr_9",
            True,
        ),
        (None, None, False),
    ],
    ids=["found-without-link", "found-with-link", "not-found"],
)
def test_check_on_a_desk_session_looks_its_inbox_thread_up_by_name(
    tmp_path: Path, found: InboxThread | None, expected_url: str | None, expected_known: bool
) -> None:
    desk_folder = Path("/tmp/desk/api")
    record = _record(
        terminal_name="desk-api",
        terminal_address="t9",
        started_by_pan=False,
        worktree_path=desk_folder,
        inbox_thread_name="desk-api: fix the build",
        requester="U0TEST",
    )
    thread_map = _thread_map(tmp_path, record)
    known_threads = {(desk_folder, "desk-api: fix the build"): found} if found else {}
    inbox_directory = FakeInboxDirectory(known_threads)
    reporter = RecordingReporter()

    _check(thread_map, _host(make_terminal("t9", name="desk-api")), reporter, inbox_directory)

    ((details),) = reporter.of_type(PrivateDetails)
    assert (details.inbox_thread_url, details.inbox_thread_known) == (expected_url, expected_known)
    assert inbox_directory.lookups == [(desk_folder, "desk-api: fix the build")]
    stored = thread_map.get(_OLD_CONVERSATION)
    assert stored is not None
    assert stored.inbox_thread_id == (found.thread_id if found is not None else None)


def test_check_looks_again_once_the_session_has_created_its_inbox_thread(
    tmp_path: Path,
) -> None:
    # The session creates its Inbox thread only on its first send, so an early lookup misses;
    # a later check looks again and stores what it finds.
    desk_folder = Path("/tmp/desk/test")
    thread_name = "desk-test: reply with the word hello"
    record = _record(
        terminal_name="desk-test",
        terminal_address="t9",
        started_by_pan=False,
        worktree_path=desk_folder,
        inbox_thread_name=thread_name,
    )
    thread_map = _thread_map(tmp_path, record)
    terminal_host = _host(make_terminal("t9", name="desk-test"))

    _check(thread_map, terminal_host, RecordingReporter(), FakeInboxDirectory())
    created = InboxThread(thread_id="msg_1", name=thread_name)
    _check(
        thread_map,
        terminal_host,
        RecordingReporter(),
        FakeInboxDirectory({(desk_folder, thread_name): created}),
    )

    stored = thread_map.get(_OLD_CONVERSATION)
    assert stored is not None
    assert stored.inbox_thread_id == "msg_1"


def test_check_does_not_look_up_a_session_whose_inbox_thread_is_known(tmp_path: Path) -> None:
    record = _record(inbox_thread_id="thr_1", inbox_thread_url="https://inbox.example/t/thr_1")
    thread_map = _thread_map(tmp_path, record)
    inbox_directory = FakeInboxDirectory()

    _check(
        thread_map, _host(make_terminal("t7", name="pan-7")), RecordingReporter(), inbox_directory
    )

    assert inbox_directory.lookups == []


def _follow_up(
    terminal_host: FakeTerminalHost,
    thread_map: FileThreadMap,
    reporter: RecordingReporter,
    *,
    reply_in_slack: bool = False,
) -> ThreadRecord:
    return follow_up_to_session(
        conversation=_OLD_CONVERSATION,
        text="and the docs",
        terminal_host=terminal_host,
        thread_map=thread_map,
        reporter=reporter,
        clock=FixedClock(),
        reply_in_slack=reply_in_slack,
    )


@pytest.mark.parametrize(
    ("started_by_pan", "initial_mode", "reply_in_slack", "expected_mode"),
    [
        (True, ReplyMode.ON_DEMAND, True, ReplyMode.SLACK),
        (False, ReplyMode.ON_DEMAND, True, ReplyMode.SLACK),
        (True, ReplyMode.ON_DEMAND, False, ReplyMode.ON_DEMAND),
        (False, ReplyMode.ON_DEMAND, False, ReplyMode.ON_DEMAND),
        (True, ReplyMode.SLACK, False, ReplyMode.SLACK),
        (False, ReplyMode.SLACK, False, ReplyMode.SLACK),
    ],
    ids=[
        "pan-session-flag-sets-slack",
        "desk-session-flag-sets-slack",
        "pan-session-no-flag-keeps-on-demand",
        "desk-session-no-flag-keeps-on-demand",
        "pan-session-no-flag-never-downgrades",
        "desk-session-no-flag-never-downgrades",
    ],
)
def test_follow_up_reply_in_slack_persists_the_mode_for_the_thread(
    tmp_path: Path,
    started_by_pan: bool,
    initial_mode: ReplyMode,
    reply_in_slack: bool,
    expected_mode: ReplyMode,
) -> None:
    name, address = ("pan-7", "t7") if started_by_pan else ("desk-api", "t9")
    record = _record(
        terminal_name=name,
        terminal_address=address,
        started_by_pan=started_by_pan,
        reply_mode=initial_mode,
        inbox_thread_name=f"{name}: fix the build",
    )
    thread_map = _thread_map(tmp_path, record)
    agent_status = AgentStatus.IDLE if started_by_pan else AgentStatus.DONE
    terminal_host = _host(make_terminal(address, name=name, agent_status=agent_status))

    returned = _follow_up(
        terminal_host, thread_map, RecordingReporter(), reply_in_slack=reply_in_slack
    )

    stored = thread_map.get(_OLD_CONVERSATION)
    assert stored is not None
    assert stored.reply_mode is expected_mode
    assert returned.reply_mode is expected_mode
    # A desk session's typed header says who replies in Slack under the mode as it now stands.
    expected_text = (
        "and the docs"
        if started_by_pan
        else from_slack_text(
            f"{name}: fix the build", "and the docs", expected_mode, thread_known=False
        )
    )
    assert terminal_host.sent == [(address, expected_text)]


@pytest.mark.parametrize(
    "record, terminal, expected_text",
    [
        (_record(), make_terminal("t7", name="pan-7"), "and the docs"),
        (
            _record(
                terminal_name="desk-api",
                terminal_address="t9",
                started_by_pan=False,
                inbox_thread_name="desk-api: fix the build",
            ),
            make_terminal("t9", name="desk-api", agent_status=AgentStatus.DONE),
            from_slack_text(
                "desk-api: fix the build", "and the docs", ReplyMode.ON_DEMAND, thread_known=False
            ),
        ),
    ],
    ids=["pan-session-verbatim", "desk-session-with-from-slack-line"],
)
def test_follow_up_types_into_the_tied_idle_session(
    tmp_path: Path, record: ThreadRecord, terminal: object, expected_text: str
) -> None:
    thread_map = _thread_map(tmp_path, record)
    terminal_host = _host(terminal)
    reporter = RecordingReporter()

    _follow_up(terminal_host, thread_map, reporter)

    assert terminal_host.sent == [(record.terminal_address, expected_text)]
    assert _line_events(reporter) == []
    # The follow-up is the thread's task now, and its status message says Working.
    assert [(status.state, status.label) for status in reporter.of_type(StatusChanged)] == [
        (StatusState.WORKING, "and the docs")
    ]
    stored = thread_map.get(_OLD_CONVERSATION)
    assert stored is not None
    assert stored.status_label == "and the docs"
    assert stored.task_started_at == _FIXED_NOW


@pytest.mark.parametrize(
    "agent_status, expected_error, expected_reason",
    [
        (AgentStatus.WORKING, SessionBusyError, NotDeliveredReason.BUSY),
        (AgentStatus.ERROR, SessionBusyError, NotDeliveredReason.BUSY),
        (AgentStatus.BLOCKED, TargetBlockedError, NotDeliveredReason.WAITING_ON_PROMPT),
    ],
    ids=["working", "error-counts-as-busy", "blocked"],
)
def test_follow_up_to_a_session_not_ready_types_nothing(
    tmp_path: Path,
    agent_status: AgentStatus,
    expected_error: type[PanError],
    expected_reason: NotDeliveredReason,
) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=agent_status))
    reporter = RecordingReporter()

    with pytest.raises(expected_error):
        _follow_up(terminal_host, thread_map, reporter)

    assert terminal_host.sent == []
    assert reporter.events() == [NotDelivered(reason=expected_reason, session_name="pan-7")]


@pytest.mark.parametrize(
    "started_by_pan, expected_events",
    [
        (True, []),
        (
            False,
            [SessionNotice(reason=SessionNoticeReason.DESK_SESSION_ENDED, session_name="pan-7")],
        ),
    ],
    ids=["pan-session-left-to-reopen", "desk-session-never-reopened"],
)
def test_follow_up_to_an_ended_session(
    tmp_path: Path, started_by_pan: bool, expected_events: list[Event]
) -> None:
    thread_map = _thread_map(tmp_path, _record(started_by_pan=started_by_pan))
    reporter = RecordingReporter()

    with pytest.raises(TerminalGoneError):
        _follow_up(_host(), thread_map, reporter)

    assert reporter.events() == expected_events


def test_follow_up_in_an_untied_thread_replies_one_line(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record(tied=False))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    reporter = RecordingReporter()

    with pytest.raises(ThreadNotFoundError):
        _follow_up(terminal_host, thread_map, reporter)

    assert terminal_host.sent == []
    assert reporter.events() == [_NOT_TIED]


@pytest.mark.parametrize(
    "reply_mode, expected_sentence",
    [
        (
            ReplyMode.SLACK,
            "Do not post to Slack yourself: pan posts your final answer to this Slack thread "
            "for you.",
        ),
        (
            ReplyMode.ON_DEMAND,
            "Do not post to Slack yourself: the Slack thread gets a reply only when the person "
            "asks for one, and pan posts it for you.",
        ),
    ],
    ids=["slack", "on-demand"],
)
def test_from_slack_text_says_who_replies_in_slack(
    reply_mode: ReplyMode, expected_sentence: str
) -> None:
    typed = from_slack_text("desk-api: fix the build", "add the tests", reply_mode)

    header, task = typed.split("\n\nTask:\n")
    assert task == "add the tests"
    assert '"desk-api: fix the build"' in header
    assert header.endswith(f"{expected_sentence} {RELAYED_FOLLOW_UP_SENTENCE}")


def test_follow_up_to_a_desk_session_in_slack_mode_says_pan_posts_the_answer(
    tmp_path: Path,
) -> None:
    record = _record(
        terminal_name="desk-api",
        terminal_address="t9",
        started_by_pan=False,
        inbox_thread_name="desk-api: fix the build",
        reply_mode=ReplyMode.SLACK,
    )
    thread_map = _thread_map(tmp_path, record)
    terminal_host = _host(make_terminal("t9", name="desk-api", agent_status=AgentStatus.DONE))

    follow_up_to_session(
        conversation=_OLD_CONVERSATION,
        text="and the docs",
        terminal_host=terminal_host,
        thread_map=thread_map,
        reporter=RecordingReporter(),
        clock=FixedClock(),
    )

    assert terminal_host.sent == [
        (
            "t9",
            from_slack_text(
                "desk-api: fix the build", "and the docs", ReplyMode.SLACK, thread_known=False
            ),
        )
    ]


_DIVIDER = "\u2500" * 200


@pytest.mark.parametrize(
    "screen, max_lines, expected",
    [
        ("first line\nsecond line", 30, "first line\nsecond line"),
        (f"{_DIVIDER}\n> fix it\n{_DIVIDER}\n  ? for shortcuts", 30, "> fix it\n  ? for shortcuts"),
        (
            "\u256d\u2500\u2500\u256e\n\u2502 Claude Code \u2502\n\u2570\u2500\u2500\u256f\n--- \n===",
            30,
            "\u2502 Claude Code \u2502",
        ),
        ("\n\nanswer\n\n\n   \n\nnext\n\n", 30, "answer\n\nnext"),
        (f"answer\n\n{_DIVIDER}\n\nnext", 30, "answer\n\nnext"),
        ("trailing   \nspaces\t", 30, "trailing\nspaces"),
        ("one\ntwo\n\nthree\nfour", 2, "three\nfour"),
        ("one\ntwo\n\nthree\nfour", 3, "two\n\nthree\nfour"),
        (
            f"{INCOMPLETE_READ_NOTICE}\none\n{_DIVIDER}\ntwo\nthree",
            2,
            f"{INCOMPLETE_READ_NOTICE}\ntwo\nthree",
        ),
    ],
    ids=[
        "normal-text-kept",
        "dividers-removed",
        "banner-frame-and-ascii-rules-removed",
        "blank-runs-collapsed",
        "blank-runs-around-a-divider-collapsed",
        "trailing-whitespace-stripped",
        "tail-limit",
        "tail-limit-keeps-inner-blank-line",
        "incomplete-marker-kept",
    ],
)
def test_clean_screen_text(screen: str, max_lines: int, expected: str) -> None:
    assert clean_screen_text(screen, max_lines) == expected


def test_check_without_a_transcript_posts_cleaned_screen_text(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    screen = f"{_DIVIDER}\n\n\nlast screen lines\n\n{_DIVIDER}"
    terminal_host.read_result = TerminalRead(text=screen, complete=False)
    reporter = RecordingReporter()

    _check(thread_map, terminal_host, reporter)

    assert _line_events(reporter) == [
        ScreenCaptured(text=f"{INCOMPLETE_READ_NOTICE}\nlast screen lines")
    ]


def test_send_to_a_desk_session_whose_suggested_thread_exists_names_it(tmp_path: Path) -> None:
    desk_folder = Path("/tmp/desk/api")
    earlier = _record(
        terminal_name="desk-api",
        terminal_address="t9",
        started_by_pan=False,
        worktree_path=desk_folder,
        inbox_thread_name="desk-api: fix the build",
    )
    thread_map = _thread_map(tmp_path, earlier)
    terminal_host = _host(make_terminal("t9", name="desk-api", cwd=desk_folder))
    found = InboxThread(thread_id="thr_9", name="desk-api: fix the build")

    _send(
        terminal_host,
        thread_map,
        RecordingReporter(),
        "desk-api",
        inbox_directory=FakeInboxDirectory({(desk_folder, "desk-api: fix the build"): found}),
    )

    assert terminal_host.sent == [
        ("t9", from_slack_text("desk-api: fix the build", "add the tests", ReplyMode.ON_DEMAND))
    ]
    tied = thread_map.get(_NEW_CONVERSATION)
    assert tied is not None
    assert tied.inbox_thread_id == "thr_9"


@pytest.mark.parametrize(
    "selector, terminals",
    [
        ("pan-8", [make_terminal("t7", name="pan-7")]),
        ("fix the login bug", [make_terminal("t7", name="pan-7")]),
        ("web", [make_terminal("t3", where="web / one"), make_terminal("t4", where="web / two")]),
    ],
    ids=["unknown-name", "sentence", "label-several-sessions-share"],
)
def test_send_without_an_exact_match_sends_and_posts_nothing(
    tmp_path: Path, selector: str, terminals: list[object]
) -> None:
    thread_map = _thread_map(tmp_path)
    terminal_host = _host(*terminals)
    reporter = RecordingReporter()

    with pytest.raises(NoExactMatchError):
        _send(terminal_host, thread_map, reporter, selector)

    assert terminal_host.sent == []
    assert terminal_host.runs == []
    assert thread_map.get(_NEW_CONVERSATION) is None
    assert reporter.events() == []


def test_send_looks_a_suggested_thread_up_only_once(tmp_path: Path) -> None:
    desk_folder = Path("/tmp/desk/api")
    earlier = _record(
        terminal_name="desk-api",
        terminal_address="t9",
        started_by_pan=False,
        worktree_path=desk_folder,
        inbox_thread_name="desk-api: fix the build",
    )
    thread_map = _thread_map(tmp_path, earlier)
    inbox_directory = FakeInboxDirectory()

    _send(
        _host(make_terminal("t9", name="desk-api", cwd=desk_folder)),
        thread_map,
        RecordingReporter(),
        "desk-api",
        inbox_directory=inbox_directory,
    )

    assert inbox_directory.lookups == [(desk_folder, "desk-api: fix the build")]


@pytest.mark.parametrize(
    ("agent_status", "expected_error"),
    [(AgentStatus.WORKING, SessionBusyError), (AgentStatus.BLOCKED, TargetBlockedError)],
    ids=["busy", "blocked"],
)
@pytest.mark.parametrize("started_by_pan", [True, False], ids=["pan-session", "desk-session"])
def test_refused_follow_up_with_reply_in_slack_leaves_the_mode_unchanged(
    tmp_path: Path,
    agent_status: AgentStatus,
    expected_error: type[PanError],
    started_by_pan: bool,
) -> None:
    name, address = ("pan-7", "t7") if started_by_pan else ("desk-api", "t9")
    record = _record(terminal_name=name, terminal_address=address, started_by_pan=started_by_pan)
    thread_map = _thread_map(tmp_path, record)
    terminal_host = _host(make_terminal(address, name=name, agent_status=agent_status))

    with pytest.raises(expected_error):
        _follow_up(terminal_host, thread_map, RecordingReporter(), reply_in_slack=True)

    stored = thread_map.get(_OLD_CONVERSATION)
    assert stored is not None
    assert stored.reply_mode is ReplyMode.ON_DEMAND
    assert terminal_host.sent == []


_MAIN_REPO = Path("/repos/app")
_PAN_SETTINGS = ".claude/settings.json"


def _closed(
    *, worktree_removed: bool, branch_deleted: bool, repo_known: bool = True
) -> SessionClosed:
    return SessionClosed(
        session_name="pan-7",
        worktree_removed=worktree_removed,
        branch_deleted=branch_deleted,
        repo_known=repo_known,
    )


def _worktree_status(**updates: object) -> WorktreeStatus:
    # A fresh pan worktree: on its own branch, only pan's untracked settings file.
    status = WorktreeStatus(
        main_repo=_MAIN_REPO,
        is_main_worktree=False,
        branch="pan-7",
        untracked_paths=[_PAN_SETTINGS],
    )
    return status.model_copy(update=updates)


def _pan_worktree(tmp_path: Path) -> Path:
    worktree_path = tmp_path / "wt" / "pan-7"
    worktree_path.mkdir(parents=True)
    return worktree_path


def _git_with(
    status: WorktreeStatus | None = None,
    branch: BranchStatus | None = None,
    repo: Path = _MAIN_REPO,
) -> FakeGitWorktree:
    return FakeGitWorktree(
        status=status if status is not None else _worktree_status(),
        branches={repo: branch if branch is not None else BranchStatus()},
    )


def _done(
    tmp_path: Path,
    thread_map: FileThreadMap,
    terminal_host: FakeTerminalHost,
    git: FakeGitWorktree,
    reporter: RecordingReporter,
    *,
    discard: bool = False,
) -> None:
    done_thread(
        conversation=_OLD_CONVERSATION,
        discard=discard,
        worktree_base=tmp_path / "wt",
        fallback_repo=tmp_path / "orchestrator",
        thread_map=thread_map,
        terminal_host=terminal_host,
        git=git,
        reporter=reporter,
        clock=FixedClock(),
    )


def _tied_record(thread_map: FileThreadMap) -> ThreadRecord:
    record = thread_map.get(_OLD_CONVERSATION)
    assert record is not None
    return record


@pytest.mark.parametrize("records", [[], [_record(tied=False)]], ids=["no-record", "untied"])
def test_done_in_a_thread_with_no_session_does_nothing(
    tmp_path: Path, records: list[ThreadRecord]
) -> None:
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    git = _git_with()
    reporter = RecordingReporter()

    _done(tmp_path, _thread_map(tmp_path, *records), terminal_host, git, reporter)

    assert reporter.events() == [ThreadNotice(reason=ThreadNoticeReason.NO_SESSION)]
    assert terminal_host.removed == []
    assert git.calls == []


def test_done_only_unties_a_session_pan_did_not_start(tmp_path: Path) -> None:
    desk_folder = _pan_worktree(tmp_path)
    thread_map = _thread_map(
        tmp_path,
        _record(terminal_name="desk-x", started_by_pan=False, worktree_path=desk_folder),
    )
    terminal_host = _host(make_terminal("t7", name="desk-x"))
    git = _git_with()
    reporter = RecordingReporter()

    _done(tmp_path, thread_map, terminal_host, git, reporter, discard=True)

    assert reporter.events() == [
        SessionNotice(reason=SessionNoticeReason.DESK_SESSION_UNTIED, session_name="desk-x")
    ]
    record = _tied_record(thread_map)
    assert record.tied is False
    assert record.status is WorkerStatus.IDLE
    assert terminal_host.removed == []
    assert git.calls == []


@pytest.mark.parametrize(
    "agent_status, expected_error, expected_reason",
    [
        (AgentStatus.WORKING, SessionBusyError, SessionNoticeReason.NOT_CLOSED_WORKING),
        (
            AgentStatus.BLOCKED,
            TargetBlockedError,
            SessionNoticeReason.NOT_CLOSED_WAITING_ON_PROMPT,
        ),
    ],
    ids=["working", "waiting-on-a-prompt"],
)
def test_done_refuses_a_session_that_is_not_finished(
    tmp_path: Path,
    agent_status: AgentStatus,
    expected_error: type[Exception],
    expected_reason: SessionNoticeReason,
) -> None:
    thread_map = _thread_map(tmp_path, _record(worktree_path=_pan_worktree(tmp_path)))
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=agent_status))
    git = _git_with()
    reporter = RecordingReporter()

    with pytest.raises(expected_error):
        _done(tmp_path, thread_map, terminal_host, git, reporter)

    assert reporter.events() == [SessionNotice(reason=expected_reason, session_name="pan-7")]
    assert _tied_record(thread_map).tied is True
    assert terminal_host.removed == []
    assert git.calls == []


@pytest.mark.parametrize(
    "status, branch, expected_uncommitted, expected_unpushed",
    [
        (_worktree_status(modified_paths=["README.md"]), BranchStatus(), 1, 0),
        (
            _worktree_status(untracked_paths=[], modified_paths=[_PAN_SETTINGS]),
            BranchStatus(),
            1,
            0,
        ),
        (
            _worktree_status(untracked_paths=[_PAN_SETTINGS, "notes.txt"], modified_paths=["a"]),
            BranchStatus(commits_not_in_base=1),
            2,
            1,
        ),
        (
            _worktree_status(),
            BranchStatus(upstream="origin/pan-7", commits_not_on_upstream=2, commits_not_in_base=3),
            0,
            2,
        ),
        (_worktree_status(branch=None, detached_commits=1), BranchStatus(), 0, 1),
    ],
    ids=[
        "modified-file",
        "tracked-settings-file-changed",
        "files-and-unmerged-commit",
        "commits-ahead-of-upstream",
        "detached-head-commit",
    ],
)
def test_done_keeps_a_session_whose_work_would_be_lost(
    tmp_path: Path,
    status: WorktreeStatus,
    branch: BranchStatus,
    expected_uncommitted: int,
    expected_unpushed: int,
) -> None:
    worktree_path = _pan_worktree(tmp_path)
    thread_map = _thread_map(tmp_path, _record(worktree_path=worktree_path))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    git = _git_with(status, branch)
    reporter = RecordingReporter()

    with pytest.raises(UnsavedWorkError):
        _done(tmp_path, thread_map, terminal_host, git, reporter)

    assert reporter.events() == [
        WorkKept(
            session_name="pan-7",
            uncommitted_files=expected_uncommitted,
            unpushed_commits=expected_unpushed,
        )
    ]
    assert git.calls == [("status", worktree_path), ("branch_status", _MAIN_REPO, "pan-7")]
    assert terminal_host.removed == []
    record = _tied_record(thread_map)
    assert record.tied is True
    assert record.status is WorkerStatus.IDLE


@pytest.mark.parametrize(
    "status, branch, agent_status, discard, expected_remove_force, expected_delete_force",
    [
        (_worktree_status(), BranchStatus(), AgentStatus.IDLE, False, True, True),
        (
            _worktree_status(untracked_paths=[]),
            BranchStatus(),
            AgentStatus.IDLE,
            False,
            False,
            True,
        ),
        (
            _worktree_status(),
            BranchStatus(upstream="origin/pan-7", commits_not_in_base=2),
            AgentStatus.IDLE,
            False,
            True,
            False,
        ),
        (
            _worktree_status(),
            BranchStatus(upstream="origin/pan-7", upstream_gone=True, commits_not_in_base=2),
            AgentStatus.IDLE,
            False,
            True,
            True,
        ),
        (
            _worktree_status(modified_paths=["app.py"]),
            BranchStatus(commits_not_in_base=4),
            AgentStatus.IDLE,
            True,
            True,
            True,
        ),
        (_worktree_status(), BranchStatus(), AgentStatus.WORKING, True, True, True),
        (_worktree_status(), BranchStatus(), AgentStatus.BLOCKED, True, True, True),
    ],
    ids=[
        "only-pan-settings",
        "clean",
        "pushed-not-merged",
        "upstream-gone-after-merge",
        "discard-unsaved-work",
        "discard-working-session",
        "discard-blocked-session",
    ],
)
def test_done_closes_the_session_and_removes_its_worktree_and_branch(
    tmp_path: Path,
    status: WorktreeStatus,
    branch: BranchStatus,
    agent_status: AgentStatus,
    discard: bool,
    expected_remove_force: bool,
    expected_delete_force: bool,
) -> None:
    worktree_path = _pan_worktree(tmp_path)
    thread_map = _thread_map(tmp_path, _record(worktree_path=worktree_path))
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=agent_status))
    git = _git_with(status, branch)
    reporter = RecordingReporter()

    _done(tmp_path, thread_map, terminal_host, git, reporter, discard=discard)

    assert terminal_host.removed == ["t7"]
    assert git.calls == [
        ("status", worktree_path),
        ("branch_status", _MAIN_REPO, "pan-7"),
        ("remove", worktree_path, expected_remove_force),
        ("delete_branch", _MAIN_REPO, "pan-7", expected_delete_force),
    ]
    # The status says Closed, then one line says what was removed.
    assert reporter.states() == [StatusState.CLOSED]
    assert reporter.events()[1:] == [_closed(worktree_removed=True, branch_deleted=True)]
    record = _tied_record(thread_map)
    assert record.status is WorkerStatus.DONE
    assert record.tied is False


@pytest.mark.parametrize(
    "terminals",
    [[], [make_terminal("t7", name="desk-other")]],
    ids=["terminal-gone", "address-reused-by-another-session"],
)
def test_done_cleans_the_worktree_when_the_terminal_is_gone(
    tmp_path: Path, terminals: list[object]
) -> None:
    worktree_path = _pan_worktree(tmp_path)
    thread_map = _thread_map(tmp_path, _record(worktree_path=worktree_path))
    terminal_host = _host(*terminals)
    git = _git_with()
    reporter = RecordingReporter()

    _done(tmp_path, thread_map, terminal_host, git, reporter)

    assert terminal_host.removed == []
    assert ("remove", worktree_path, True) in git.calls
    assert _line_events(reporter) == [_closed(worktree_removed=True, branch_deleted=True)]


def test_done_removes_the_worktree_when_its_branch_is_already_gone(tmp_path: Path) -> None:
    worktree_path = _pan_worktree(tmp_path)
    thread_map = _thread_map(tmp_path, _record(worktree_path=worktree_path))
    git = FakeGitWorktree(status=_worktree_status(branch=None))
    reporter = RecordingReporter()

    _done(tmp_path, thread_map, _host(), git, reporter)

    assert git.calls == [
        ("status", worktree_path),
        ("branch_status", _MAIN_REPO, "pan-7"),
        ("remove", worktree_path, True),
    ]
    assert _line_events(reporter) == [_closed(worktree_removed=True, branch_deleted=False)]


@pytest.mark.parametrize(
    "recorded_repo, repo_with_branch, expected_repo",
    [
        ("repos/app", "repos/app", "repos/app"),
        (None, "orchestrator", "orchestrator"),
        ("repos/app", None, "repos/app"),
        (None, None, None),
    ],
    ids=[
        "recorded-repo",
        "old-record-orchestrator-repo-has-branch",
        "branch-already-gone",
        "old-record-orchestrator-repo-lacks-branch",
    ],
)
def test_done_with_the_worktree_folder_gone_cleans_up_from_the_repo(
    tmp_path: Path,
    recorded_repo: str | None,
    repo_with_branch: str | None,
    expected_repo: str | None,
) -> None:
    for folder in ("repos/app", "orchestrator"):
        (tmp_path / folder).mkdir(parents=True)
    record = _record(
        worktree_path=tmp_path / "wt" / "pan-7",
        repo_path=tmp_path / recorded_repo if recorded_repo is not None else None,
    )
    thread_map = _thread_map(tmp_path, record)
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    branches = {tmp_path / repo_with_branch: BranchStatus()} if repo_with_branch else {}
    git = FakeGitWorktree(branches=branches)
    reporter = RecordingReporter()

    _done(tmp_path, thread_map, terminal_host, git, reporter)

    writes = [call for call in git.calls if call[0] in ("prune", "delete_branch")]
    if expected_repo is None:
        assert writes == []
    else:
        expected_writes: list[tuple[object, ...]] = [("prune", tmp_path / expected_repo)]
        if repo_with_branch is not None:
            expected_writes.append(("delete_branch", tmp_path / expected_repo, "pan-7", True))
        assert writes == expected_writes
    assert terminal_host.removed == ["t7"]
    assert _line_events(reporter) == [
        _closed(
            worktree_removed=False,
            branch_deleted=repo_with_branch is not None,
            repo_known=expected_repo is not None,
        )
    ]
    record = _tied_record(thread_map)
    assert record.status is WorkerStatus.DONE
    assert record.tied is False


def test_done_with_the_worktree_folder_gone_judges_the_branch_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repos" / "app"
    repo.mkdir(parents=True)
    thread_map = _thread_map(
        tmp_path, _record(worktree_path=tmp_path / "wt" / "pan-7", repo_path=repo)
    )
    git = FakeGitWorktree(branches={repo: BranchStatus(commits_not_in_base=1)})
    reporter = RecordingReporter()

    with pytest.raises(UnsavedWorkError):
        _done(tmp_path, thread_map, _host(), git, reporter)

    assert reporter.events() == [
        WorkKept(session_name="pan-7", uncommitted_files=0, unpushed_commits=1)
    ]
    assert git.calls == [("branch_status", repo, "pan-7")]


@pytest.mark.parametrize(
    "folder_name, status, git_errors, expected_error, expected_reason",
    [
        (
            "outside",
            _worktree_status(),
            {},
            UnsafeWorktreeError,
            SessionNoticeReason.FOLDER_OUTSIDE_WORKTREES,
        ),
        (
            "wt/pan-7",
            _worktree_status(is_main_worktree=True),
            {},
            UnsafeWorktreeError,
            SessionNoticeReason.FOLDER_IS_MAIN_REPOSITORY,
        ),
        (
            "wt/pan-7",
            None,
            {"status": GitWorktreeError("not a git repository")},
            GitWorktreeError,
            SessionNoticeReason.WORKTREE_UNREADABLE,
        ),
    ],
    ids=["outside-worktree-base", "main-worktree", "unreadable"],
)
def test_done_keeps_a_worktree_it_cannot_safely_remove(
    tmp_path: Path,
    folder_name: str,
    status: WorktreeStatus | None,
    git_errors: dict[str, PanError],
    expected_error: type[PanError],
    expected_reason: SessionNoticeReason,
) -> None:
    folder = tmp_path / folder_name
    folder.mkdir(parents=True)
    thread_map = _thread_map(tmp_path, _record(worktree_path=folder))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    git = FakeGitWorktree(status=status, errors=git_errors)
    reporter = RecordingReporter()

    with pytest.raises(expected_error):
        _done(tmp_path, thread_map, terminal_host, git, reporter)

    assert reporter.events() == [SessionNotice(reason=expected_reason, session_name="pan-7")]
    assert terminal_host.removed == []
    assert not any(call[0] in ("remove", "prune", "delete_branch") for call in git.calls)
    assert _tied_record(thread_map).tied is True


@pytest.mark.parametrize("failing_operation", ["remove", "delete_branch"])
def test_done_reports_a_failed_cleanup_and_keeps_the_thread_tied(
    tmp_path: Path, failing_operation: str
) -> None:
    worktree_path = _pan_worktree(tmp_path)
    thread_map = _thread_map(tmp_path, _record(worktree_path=worktree_path))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    git = _git_with()
    git.errors[failing_operation] = GitWorktreeError("git failed")
    reporter = RecordingReporter()

    with pytest.raises(GitWorktreeError):
        _done(tmp_path, thread_map, terminal_host, git, reporter)

    assert terminal_host.removed == ["t7"]
    assert reporter.events() == [CleanupFailed(session_name="pan-7", worktree_path=worktree_path)]
    record = _tied_record(thread_map)
    assert record.status is WorkerStatus.IDLE
    assert record.tied is True
    assert record.terminal_address == ""


def test_done_retry_after_a_failed_branch_delete_reports_only_the_branch(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repos" / "app"
    repo.mkdir(parents=True)
    worktree_path = _pan_worktree(tmp_path)
    thread_map = _thread_map(tmp_path, _record(worktree_path=worktree_path, repo_path=repo))
    git = _git_with(_worktree_status(main_repo=repo), repo=repo)
    git.errors["delete_branch"] = GitWorktreeError("git failed")
    with pytest.raises(GitWorktreeError):
        _done(
            tmp_path, thread_map, _host(make_terminal("t7", name="pan-7")), git, RecordingReporter()
        )
    worktree_path.rmdir()
    git.errors.clear()
    git.calls.clear()
    reporter = RecordingReporter()

    _done(tmp_path, thread_map, _host(), git, reporter)

    assert git.calls == [
        ("branch_status", repo, "pan-7"),
        ("prune", repo),
        ("delete_branch", repo, "pan-7", True),
    ]
    assert _line_events(reporter) == [_closed(worktree_removed=False, branch_deleted=True)]
    record = _tied_record(thread_map)
    assert record.status is WorkerStatus.DONE
    assert record.tied is False


@pytest.mark.parametrize(
    "reply_mode",
    [ReplyMode.SLACK, ReplyMode.ON_DEMAND],
    ids=["reply-in-slack-stop-hook-posted-it", "on-demand-check-posts-it"],
)
def test_check_never_reposts_the_answer_already_posted(
    tmp_path: Path, reply_mode: ReplyMode
) -> None:
    # In the reply-in-Slack mode the Stop hook posts the answer, so check only updates the
    # status; on demand nothing was posted, so check posts it. Either way it appears once.
    transcript = _transcript_answering(tmp_path, "Fixed the login page.")
    thread_map = _thread_map(tmp_path, _record(reply_mode=reply_mode))
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=AgentStatus.DONE))
    reporter = RecordingReporter()
    stop_payload = json.dumps(
        {"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(transcript)}
    )

    stop_hook(
        _OLD_CONVERSATION,
        thread_map,
        reporter,
        stdin=io.StringIO(stop_payload),
        session_name="pan-7",
        clock=FixedClock(),
    )
    thread_map.record_session(_OLD_CONVERSATION, "s1", transcript, session_name="pan-7")
    _check(thread_map, terminal_host, reporter)
    _check(thread_map, terminal_host, reporter)

    assert _line_events(reporter) == [AnswerReady(text="Fixed the login page.")]
    # hc's `done` on a live terminal is a finished turn, never Closed.
    assert set(reporter.states()) == {StatusState.FINISHED}


def test_check_posts_a_newer_answer_than_the_one_already_posted(tmp_path: Path) -> None:
    record = _record(
        transcript_path=_transcript_answering(tmp_path, "Second answer."),
        last_answer_digest=answer_digest("First answer."),
    )
    thread_map = _thread_map(tmp_path, record)
    reporter = RecordingReporter()

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), reporter)

    assert _line_events(reporter) == [AnswerReady(text="Second answer.")]
    stored = thread_map.get(_OLD_CONVERSATION)
    assert stored is not None
    assert stored.last_answer_digest == answer_digest("Second answer.")


def _stop(
    thread_map: FileThreadMap, terminal_host: FakeTerminalHost, reporter: RecordingReporter
) -> None:
    stop_thread(
        conversation=_OLD_CONVERSATION,
        thread_map=thread_map,
        terminal_host=terminal_host,
        open_reporter=lambda: reporter,
        clock=FixedClock(),
    )


@pytest.mark.parametrize("terminal_address", ["t7", ""], ids=["recorded", "never-launched"])
def test_stop_thread_removes_the_terminal_and_marks_failed(
    tmp_path: Path, terminal_address: str
) -> None:
    thread_map = _thread_map(tmp_path, _record(terminal_address=terminal_address))
    terminal_host = _host(make_terminal("t7", name="pan-7"))

    _stop(thread_map, terminal_host, RecordingReporter())

    assert terminal_host.removed == ([terminal_address] if terminal_address else [])
    stopped = thread_map.get(_OLD_CONVERSATION)
    assert stopped is not None
    assert stopped.status is WorkerStatus.FAILED


def test_stop_thread_edits_the_threads_status_message_to_stopped(tmp_path: Path) -> None:
    thread_map = _thread_map(
        tmp_path,
        _record(status_message_ts="1700000000.000900", status_label="fix the bug"),
    )
    reporter = RecordingReporter()

    _stop(thread_map, _host(make_terminal("t7", name="pan-7")), reporter)

    assert [
        (status.status_message_id, status.state, status.label)
        for status in reporter.of_type(StatusChanged)
    ] == [("1700000000.000900", StatusState.STOPPED, "fix the bug")]
    assert len(reporter.events()) == 1


def test_stop_thread_still_stops_when_slack_cannot_be_reached(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7"))

    def no_credentials() -> RecordingReporter:
        raise CredentialsError("no credentials")

    stop_thread(
        conversation=_OLD_CONVERSATION,
        thread_map=thread_map,
        terminal_host=terminal_host,
        open_reporter=no_credentials,
        clock=FixedClock(),
    )

    assert terminal_host.removed == ["t7"]
    stopped = thread_map.get(_OLD_CONVERSATION)
    assert stopped is not None
    assert stopped.status is WorkerStatus.FAILED


def test_stop_thread_never_stops_a_session_pan_did_not_start(tmp_path: Path) -> None:
    thread_map = FileThreadMap(tmp_path / "threads.json", FixedClock())
    thread_map.tie(_record(terminal_name="desk-api", terminal_address="t9", started_by_pan=False))
    terminal_host = _host(make_terminal("t9", name="desk-api"))
    reporter = RecordingReporter()

    with pytest.raises(NotPanSessionError):
        _stop(thread_map, terminal_host, reporter)

    assert terminal_host.removed == []
    kept = thread_map.get(_OLD_CONVERSATION)
    assert kept is not None
    assert kept.tied is False
    assert kept.status is WorkerStatus.IDLE
    assert reporter.events() == []


def test_stop_thread_without_a_record_raises(tmp_path: Path) -> None:
    with pytest.raises(ThreadNotFoundError):
        _stop(_thread_map(tmp_path), _host(), RecordingReporter())
