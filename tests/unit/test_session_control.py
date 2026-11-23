from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.errors import (
    PanError,
    SessionBusyError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
    TerminalGoneError,
    ThreadNotFoundError,
)
from pan.models import (
    AgentStatus,
    InboxThread,
    ReplyMode,
    TerminalRead,
    ThreadRecord,
    WorkerStatus,
)
from pan.session_control import (
    UNTIED_THREAD_REPLY,
    check_thread,
    follow_up_to_session,
    forget_thread,
    from_slack_text,
    send_to_session,
)
from pan.slack_format import to_slack_mrkdwn
from pan.spawn import desk_session_ended_reply
from pan.threadmap import FileThreadMap

_FIXED_NOW = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)
_OLD_THREAD = "1700000000.000100"
_NEW_THREAD = "1700000000.000200"


class FixedClock:
    def now(self) -> datetime:
        return _FIXED_NOW


class RecordingSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        self.posts.append((channel, thread_ts, text))

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def texts(self) -> list[str]:
        return [text for _channel, _thread, text in self.posts]


class FakeInboxDirectory:
    # Finds the threads it was given, by (project folder, thread name); records each lookup.
    def __init__(self, threads: dict[tuple[Path, str], InboxThread] | None = None) -> None:
        self._threads = threads or {}
        self.lookups: list[tuple[Path, str]] = []

    def find_thread(self, project_path: Path, thread_name: str) -> InboxThread | None:
        self.lookups.append((project_path, thread_name))
        return self._threads.get((project_path, thread_name))


def _record(
    thread_ts: str = _OLD_THREAD,
    *,
    terminal_name: str = "pan-7",
    terminal_address: str = "t7",
    started_by_pan: bool = True,
    **updates: object,
) -> ThreadRecord:
    record = ThreadRecord(
        thread_ts=thread_ts,
        terminal_name=terminal_name,
        terminal_address=terminal_address,
        channel="C0TEST",
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
    slack: RecordingSlack,
    selector: str = "pan-7",
    *,
    reply_in_slack: bool = False,
    inbox_directory: FakeInboxDirectory | None = None,
) -> ThreadRecord:
    return send_to_session(
        thread_ts=_NEW_THREAD,
        channel="C0TEST",
        selector=selector,
        text="add the tests",
        requester="U0TEST",
        reply_in_slack=reply_in_slack,
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=inbox_directory if inbox_directory is not None else FakeInboxDirectory(),
        slack=slack,
        clock=FixedClock(),
    )


@pytest.mark.parametrize(
    "reply_in_slack, inbox_url, expected_notice",
    [
        (False, None, 'Sent to pan-7 · Inbox thread: "pan-7: fix the login page"'),
        (
            True,
            "https://inbox.example/threads/abc",
            "Sent to pan-7 · Inbox: https://inbox.example/threads/abc"
            " · answers will also be posted here",
        ),
    ],
    ids=["inbox-name", "inbox-link-and-slack-replies"],
)
def test_send_to_session_sends_ties_and_posts_a_notice(
    tmp_path: Path, reply_in_slack: bool, inbox_url: str | None, expected_notice: str
) -> None:
    thread_map = _thread_map(tmp_path, _record(inbox_thread_url=inbox_url))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    slack = RecordingSlack()

    _send(terminal_host, thread_map, slack, reply_in_slack=reply_in_slack)

    assert terminal_host.sent == [
        ("t7", from_slack_text("pan-7: fix the login page", "add the tests"))
    ]
    tied = thread_map.get(_NEW_THREAD)
    assert tied is not None
    assert tied.tied is True
    assert tied.terminal_name == "pan-7"
    assert tied.requester == "U0TEST"
    assert tied.reply_mode is (ReplyMode.SLACK if reply_in_slack else ReplyMode.ON_DEMAND)
    old = thread_map.get(_OLD_THREAD)
    assert old is not None
    assert old.tied is False
    assert slack.posts == [("C0TEST", _NEW_THREAD, expected_notice)]


@pytest.mark.parametrize(
    "selector, terminals, records, expected_error, expected_reply",
    [
        (
            "pan-8",
            [make_terminal("t7", name="pan-7"), make_terminal("t9", name="desk-api")],
            [_record()],
            TargetNotFoundError,
            "no live session named pan-8; close matches: pan-7",
        ),
        (
            "nothing-like-it",
            [make_terminal("t7", name="pan-7")],
            [_record()],
            TargetNotFoundError,
            "no live session named nothing-like-it; @pan sessions lists them",
        ),
        (
            "web",
            [
                make_terminal("t3", where="web / one"),
                make_terminal("t4", where="web / two"),
            ],
            [],
            TargetAmbiguousError,
            "web matches more than one session: t3 (web / one), t4 (web / two); name one exactly",
        ),
        (
            "desk-ap",
            [make_terminal("t9", name="desk-api")],
            [],
            TargetNotFoundError,
            "no live session named desk-ap; close matches: desk-api",
        ),
        (
            "pan-7",
            [make_terminal("t7", name="pan-7", agent_status=AgentStatus.WORKING)],
            [_record()],
            SessionBusyError,
            "pan-7 is busy; try again, or @pan check",
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent_status=AgentStatus.WORKING)],
            [],
            SessionBusyError,
            "desk-api is busy; try again, or @pan check",
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent_status=AgentStatus.NONE, turn=4)],
            [],
            SessionBusyError,
            "desk-api is busy; try again, or @pan check",
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
            "desk-api is busy; try again, or @pan check",
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent=None, agent_status=AgentStatus.NONE)],
            [],
            SessionBusyError,
            "desk-api is busy; try again, or @pan check",
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent_status=AgentStatus.UNKNOWN)],
            [],
            SessionBusyError,
            "desk-api is busy; try again, or @pan check",
        ),
        (
            "desk-api",
            [make_terminal("t9", name="desk-api", agent_status=AgentStatus.BLOCKED)],
            [],
            TargetBlockedError,
            "desk-api is waiting on a prompt at the terminal; nothing was sent",
        ),
    ],
    ids=[
        "unknown-close-match",
        "unknown-no-match",
        "ambiguous",
        "desk-session-close-match",
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
    expected_reply: str,
) -> None:
    thread_map = _thread_map(tmp_path, *records)
    terminal_host = _host(*terminals)
    slack = RecordingSlack()

    with pytest.raises(expected_error):
        _send(terminal_host, thread_map, slack, selector)

    assert terminal_host.sent == []
    assert thread_map.get(_NEW_THREAD) is None
    assert slack.texts() == [expected_reply]


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
    slack = RecordingSlack()

    _send(terminal_host, thread_map, slack, "desk-api")

    assert [address for address, _ in terminal_host.sent] == ["t9"]


def test_send_to_a_desk_session_ties_it_with_a_from_slack_line(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path)
    desk_folder = Path("/tmp/desk/api")
    terminal_host = _host(make_terminal("t9", name="desk-api", cwd=desk_folder))
    slack = RecordingSlack()

    _send(terminal_host, thread_map, slack, "desk-api")

    assert terminal_host.sent == [
        ("t9", from_slack_text("desk-api: add the tests", "add the tests"))
    ]
    assert terminal_host.sent[0][1].startswith("From Slack via pan.")
    tied = thread_map.get(_NEW_THREAD)
    assert tied is not None
    assert tied.tied is True
    assert tied.started_by_pan is False
    assert tied.terminal_name == "desk-api"
    assert tied.terminal_address == "t9"
    assert tied.worktree_path == desk_folder
    assert tied.inbox_thread_name == "desk-api: add the tests"
    assert slack.texts() == ['Sent to desk-api · Inbox thread: "desk-api: add the tests"']


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
    slack = RecordingSlack()

    _send(terminal_host, thread_map, slack, "desk-api", inbox_directory=inbox_directory)

    assert terminal_host.sent == [
        ("t9", from_slack_text("desk-api: fix the build", "add the tests"))
    ]
    tied = thread_map.get(_NEW_THREAD)
    assert tied is not None
    assert tied.inbox_thread_id == "thr_9"
    assert tied.inbox_thread_url == "https://inbox.example/threads/thr_9"
    old = thread_map.get(_OLD_THREAD)
    assert old is not None
    assert old.tied is False
    assert slack.texts() == ["Sent to desk-api · Inbox: https://inbox.example/threads/thr_9"]


@pytest.mark.parametrize(
    "earlier, terminal, expected_inbox_thread_name",
    [
        (
            _record(terminal_name="desk-test", terminal_address="", started_by_pan=False),
            make_terminal("t10", name="desk-test"),
            "pan-7: fix the login page",
        ),
        (
            _record(terminal_name="desk-test", terminal_address="t9", started_by_pan=False),
            make_terminal("t10", name="desk-test"),
            "desk-test: add the tests",
        ),
        (
            _record(terminal_name="desk-test", terminal_address="t10", started_by_pan=False),
            make_terminal("t10", name="desk-test"),
            "pan-7: fix the login page",
        ),
        (
            _record(terminal_name="desk-old", terminal_address="t10", started_by_pan=False),
            make_terminal("t10", name="desk-test"),
            "desk-test: add the tests",
        ),
        (
            _record(terminal_name="desk-test", terminal_address="t9"),
            make_terminal("t10", name="desk-test", cwd=Path("/tmp/wt/pan-7")),
            "pan-7: fix the login page",
        ),
        (
            _record(terminal_name="desk-test", terminal_address="t9"),
            make_terminal("t10", name="desk-test", cwd=Path("/tmp/elsewhere")),
            "desk-test: add the tests",
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
) -> None:
    # A joined record keeps its Inbox thread name; a new session gets one of its own.
    thread_map = _thread_map(tmp_path, earlier)
    terminal_host = _host(terminal)

    _send(terminal_host, thread_map, RecordingSlack(), "desk-test")

    assert terminal_host.sent == [
        ("t10", from_slack_text(expected_inbox_thread_name, "add the tests"))
    ]
    tied = thread_map.get(_NEW_THREAD)
    assert tied is not None
    assert tied.terminal_address == "t10"
    assert tied.inbox_thread_name == expected_inbox_thread_name


def test_send_to_a_blocked_session_types_nothing_and_ties_nothing(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    terminal_host.errors["send"] = TargetBlockedError("blocked")
    slack = RecordingSlack()

    with pytest.raises(TargetBlockedError):
        _send(terminal_host, thread_map, slack)

    assert thread_map.get(_NEW_THREAD) is None
    assert slack.texts() == ["pan-7 is waiting on a prompt at the terminal; nothing was sent"]


@pytest.mark.parametrize(
    "records, expected_reply, expect_untied",
    [
        (
            [_record()],
            "untied this thread from pan-7; the session was not stopped",
            True,
        ),
        ([_record(tied=False)], UNTIED_THREAD_REPLY, True),
        ([], UNTIED_THREAD_REPLY, False),
    ],
    ids=["tied", "already-untied", "no-record"],
)
def test_forget_thread(
    tmp_path: Path, records: list[ThreadRecord], expected_reply: str, expect_untied: bool
) -> None:
    thread_map = _thread_map(tmp_path, *records)
    slack = RecordingSlack()

    forget_thread(thread_ts=_OLD_THREAD, channel="C0TEST", thread_map=thread_map, slack=slack)

    assert slack.posts == [("C0TEST", _OLD_THREAD, expected_reply)]
    record = thread_map.get(_OLD_THREAD)
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
    slack: RecordingSlack,
    inbox_directory: FakeInboxDirectory | None = None,
) -> None:
    check_thread(
        thread_ts=_OLD_THREAD,
        channel="C0TEST",
        thread_map=thread_map,
        terminal_host=terminal_host,
        inbox_directory=inbox_directory if inbox_directory is not None else FakeInboxDirectory(),
        slack=slack,
        screen_lines=50,
    )


@pytest.mark.parametrize(
    "agent_status, expected_state",
    [
        (AgentStatus.IDLE, "pan-7 is idle."),
        (AgentStatus.WORKING, "pan-7 is still working, so this answer may not be final."),
        (AgentStatus.BLOCKED, "pan-7 is waiting on a prompt at the terminal."),
    ],
    ids=["idle", "working", "blocked"],
)
def test_check_posts_the_last_answer_with_the_inbox_link_and_state(
    tmp_path: Path, agent_status: AgentStatus, expected_state: str
) -> None:
    record = _record(
        transcript_path=_transcript_answering(tmp_path, "Fixed the login page."),
        inbox_thread_url="https://inbox.example/threads/abc",
    )
    thread_map = _thread_map(tmp_path, record)
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=agent_status))
    slack = RecordingSlack()

    _check(thread_map, terminal_host, slack)

    assert slack.posts == [
        (
            "C0TEST",
            _OLD_THREAD,
            "pan-7 · Inbox: https://inbox.example/threads/abc\n"
            f"{expected_state}\n\nFixed the login page.",
        )
    ]
    assert terminal_host.reads == []


def test_check_without_a_transcript_posts_screen_text(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    terminal_host.read_result = TerminalRead(text="last screen lines", complete=True)
    slack = RecordingSlack()

    _check(thread_map, terminal_host, slack)

    assert slack.texts() == [
        'pan-7 · Inbox thread: "pan-7: fix the login page"\n'
        "pan-7 is idle.\n\n"
        "Screen text (no transcript yet):\n```\nlast screen lines\n```"
    ]
    assert terminal_host.reads == [("t7", 50)]


def test_check_on_an_ended_session_says_so(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record(inbox_thread_name=None))
    slack = RecordingSlack()

    _check(thread_map, _host(), slack)

    assert slack.texts() == ["pan-7 · no Inbox thread yet\npan-7 has ended.\n\nNo answer recorded."]


def test_check_splits_a_long_answer_into_labelled_parts(tmp_path: Path) -> None:
    long_answer = "\n".join(f"line {number} of the answer" for number in range(400))
    record = _record(transcript_path=_transcript_answering(tmp_path, long_answer))
    thread_map = _thread_map(tmp_path, record)
    slack = RecordingSlack()

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), slack)

    texts = slack.texts()
    assert len(texts) > 1
    assert texts[0].startswith(f"(1/{len(texts)})\npan-7 · ")
    assert texts[-1].endswith("line 399 of the answer")


@pytest.mark.parametrize("records", [[], [_record(tied=False)]], ids=["no-record", "untied"])
def test_check_on_an_untied_thread_replies_one_line(
    tmp_path: Path, records: list[ThreadRecord]
) -> None:
    thread_map = _thread_map(tmp_path, *records)
    slack = RecordingSlack()

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), slack)

    assert slack.posts == [("C0TEST", _OLD_THREAD, UNTIED_THREAD_REPLY)]


@pytest.mark.parametrize(
    "found, expected_header",
    [
        (
            InboxThread(thread_id="thr_9", name="desk-api: fix the build", url=None),
            'desk-api · Inbox thread: "desk-api: fix the build"',
        ),
        (
            InboxThread(
                thread_id="thr_9",
                name="desk-api: fix the build",
                url="https://inbox.example/threads/thr_9",
            ),
            "desk-api · Inbox: https://inbox.example/threads/thr_9",
        ),
        (None, 'desk-api · Inbox thread: "desk-api: fix the build"'),
    ],
    ids=["found-without-link", "found-with-link", "not-found"],
)
def test_check_on_a_desk_session_looks_its_inbox_thread_up_by_name(
    tmp_path: Path, found: InboxThread | None, expected_header: str
) -> None:
    desk_folder = Path("/tmp/desk/api")
    record = _record(
        terminal_name="desk-api",
        terminal_address="t9",
        started_by_pan=False,
        worktree_path=desk_folder,
        inbox_thread_name="desk-api: fix the build",
    )
    thread_map = _thread_map(tmp_path, record)
    known_threads = {(desk_folder, "desk-api: fix the build"): found} if found else {}
    inbox_directory = FakeInboxDirectory(known_threads)
    slack = RecordingSlack()

    _check(thread_map, _host(make_terminal("t9", name="desk-api")), slack, inbox_directory)

    assert slack.texts()[0].startswith(f"{expected_header}\n")
    assert inbox_directory.lookups == [(desk_folder, "desk-api: fix the build")]
    stored = thread_map.get(_OLD_THREAD)
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

    _check(thread_map, terminal_host, RecordingSlack(), FakeInboxDirectory())
    created = InboxThread(thread_id="msg_1", name=thread_name)
    _check(
        thread_map,
        terminal_host,
        RecordingSlack(),
        FakeInboxDirectory({(desk_folder, thread_name): created}),
    )

    stored = thread_map.get(_OLD_THREAD)
    assert stored is not None
    assert stored.inbox_thread_id == "msg_1"


def test_check_does_not_look_up_a_session_whose_inbox_thread_is_known(tmp_path: Path) -> None:
    record = _record(inbox_thread_id="thr_1", inbox_thread_url="https://inbox.example/t/thr_1")
    thread_map = _thread_map(tmp_path, record)
    inbox_directory = FakeInboxDirectory()

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), RecordingSlack(), inbox_directory)

    assert inbox_directory.lookups == []


def _follow_up(
    terminal_host: FakeTerminalHost, thread_map: FileThreadMap, slack: RecordingSlack
) -> ThreadRecord:
    return follow_up_to_session(
        thread_ts=_OLD_THREAD,
        channel="C0TEST",
        text="and the docs",
        terminal_host=terminal_host,
        thread_map=thread_map,
        slack=slack,
    )


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
            from_slack_text("desk-api: fix the build", "and the docs"),
        ),
    ],
    ids=["pan-session-verbatim", "desk-session-with-from-slack-line"],
)
def test_follow_up_types_into_the_tied_idle_session(
    tmp_path: Path, record: ThreadRecord, terminal: object, expected_text: str
) -> None:
    thread_map = _thread_map(tmp_path, record)
    terminal_host = _host(terminal)
    slack = RecordingSlack()

    _follow_up(terminal_host, thread_map, slack)

    assert terminal_host.sent == [(record.terminal_address, expected_text)]
    assert slack.posts == []


@pytest.mark.parametrize(
    "agent_status, expected_error, expected_reply",
    [
        (AgentStatus.WORKING, SessionBusyError, "pan-7 is busy; try again, or @pan check"),
        (AgentStatus.ERROR, SessionBusyError, "pan-7 is busy; try again, or @pan check"),
        (
            AgentStatus.BLOCKED,
            TargetBlockedError,
            "pan-7 is waiting on a prompt at the terminal; nothing was sent",
        ),
    ],
    ids=["working", "error-counts-as-busy", "blocked"],
)
def test_follow_up_to_a_session_not_ready_types_nothing(
    tmp_path: Path,
    agent_status: AgentStatus,
    expected_error: type[PanError],
    expected_reply: str,
) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=agent_status))
    slack = RecordingSlack()

    with pytest.raises(expected_error):
        _follow_up(terminal_host, thread_map, slack)

    assert terminal_host.sent == []
    assert slack.texts() == [expected_reply]


@pytest.mark.parametrize(
    "started_by_pan, expected_replies",
    # Slack egress escapes the reply's angle brackets (to_slack_mrkdwn); Slack shows them.
    [(True, []), (False, [to_slack_mrkdwn(desk_session_ended_reply("pan-7"))])],
    ids=["pan-session-left-to-reopen", "desk-session-never-reopened"],
)
def test_follow_up_to_an_ended_session(
    tmp_path: Path, started_by_pan: bool, expected_replies: list[str]
) -> None:
    thread_map = _thread_map(tmp_path, _record(started_by_pan=started_by_pan))
    slack = RecordingSlack()

    with pytest.raises(TerminalGoneError):
        _follow_up(_host(), thread_map, slack)

    assert slack.texts() == expected_replies


def test_follow_up_in_an_untied_thread_replies_one_line(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record(tied=False))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    slack = RecordingSlack()

    with pytest.raises(ThreadNotFoundError):
        _follow_up(terminal_host, thread_map, slack)

    assert terminal_host.sent == []
    assert slack.texts() == [UNTIED_THREAD_REPLY]
