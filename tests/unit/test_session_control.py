from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from inbox_fakes import FakeInboxDirectory
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.errors import (
    NoExactMatchError,
    PanError,
    SessionBusyError,
    TargetBlockedError,
    TerminalGoneError,
    ThreadNotFoundError,
)
from pan.interface import INCOMPLETE_READ_NOTICE
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
    clean_screen_text,
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
        (
            "t7",
            from_slack_text(
                "pan-7: fix the login page",
                "add the tests",
                ReplyMode.SLACK if reply_in_slack else ReplyMode.ON_DEMAND,
            ),
        )
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
    tied = thread_map.get(_NEW_THREAD)
    assert tied is not None
    assert tied.tied is True
    assert tied.started_by_pan is False
    assert tied.terminal_name == "desk-api"
    assert tied.terminal_address == "t9"
    assert tied.worktree_path == desk_folder
    assert tied.inbox_thread_name == "desk-api: add the tests"
    assert slack.texts() == [
        'Sent to desk-api · Inbox thread: its existing one, or "desk-api: add the tests"'
    ]


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

    _send(terminal_host, thread_map, RecordingSlack(), "desk-api")

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
    slack = RecordingSlack()

    _send(terminal_host, thread_map, slack, "desk-api", inbox_directory=inbox_directory)

    assert terminal_host.sent == [
        ("t9", from_slack_text("desk-api: fix the build", "add the tests", ReplyMode.ON_DEMAND))
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

    _send(terminal_host, thread_map, RecordingSlack(), "desk-test")

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
    "live_terminal_name, expected_state",
    [("pan-7", "pan-7 is idle."), ("pan-8", "pan-7 has ended.")],
    ids=["same-terminal", "address-reused-by-another-name"],
)
def test_check_finds_the_session_terminal_by_address_only_when_names_agree(
    tmp_path: Path, live_terminal_name: str, expected_state: str
) -> None:
    thread_map = _thread_map(
        tmp_path, _record(transcript_path=_transcript_answering(tmp_path, "Done."))
    )
    terminal_host = _host(make_terminal("t7", name=live_terminal_name))
    slack = RecordingSlack()

    _check(thread_map, terminal_host, slack)

    assert expected_state in slack.texts()[0]


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
        (None, 'desk-api · Inbox thread: its existing one, or "desk-api: fix the build"'),
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
    terminal_host: FakeTerminalHost,
    thread_map: FileThreadMap,
    slack: RecordingSlack,
    *,
    reply_in_slack: bool = False,
) -> ThreadRecord:
    return follow_up_to_session(
        thread_ts=_OLD_THREAD,
        channel="C0TEST",
        text="and the docs",
        terminal_host=terminal_host,
        thread_map=thread_map,
        slack=slack,
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
        terminal_host, thread_map, RecordingSlack(), reply_in_slack=reply_in_slack
    )

    stored = thread_map.get(_OLD_THREAD)
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
    assert header.endswith(expected_sentence)


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
        thread_ts=_OLD_THREAD,
        channel="C0TEST",
        text="and the docs",
        terminal_host=terminal_host,
        thread_map=thread_map,
        slack=RecordingSlack(),
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
    slack = RecordingSlack()

    _check(thread_map, terminal_host, slack)

    assert slack.texts() == [
        'pan-7 · Inbox thread: "pan-7: fix the login page"\n'
        "pan-7 is idle.\n\n"
        f"Screen text (no transcript yet):\n```\n{INCOMPLETE_READ_NOTICE}\nlast screen lines\n```"
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
        RecordingSlack(),
        "desk-api",
        inbox_directory=FakeInboxDirectory({(desk_folder, "desk-api: fix the build"): found}),
    )

    assert terminal_host.sent == [
        ("t9", from_slack_text("desk-api: fix the build", "add the tests", ReplyMode.ON_DEMAND))
    ]
    tied = thread_map.get(_NEW_THREAD)
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
    slack = RecordingSlack()

    with pytest.raises(NoExactMatchError):
        _send(terminal_host, thread_map, slack, selector)

    assert terminal_host.sent == []
    assert terminal_host.runs == []
    assert thread_map.get(_NEW_THREAD) is None
    assert slack.texts() == []


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
        RecordingSlack(),
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
        _follow_up(terminal_host, thread_map, RecordingSlack(), reply_in_slack=True)

    stored = thread_map.get(_OLD_THREAD)
    assert stored is not None
    assert stored.reply_mode is ReplyMode.ON_DEMAND
    assert terminal_host.sent == []
