from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from git_worktree_fakes import FakeGitWorktree
from inbox_fakes import FakeInboxDirectory
from slack_fakes import RecordingSlack
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.backend.interface import INCOMPLETE_READ_NOTICE
from pan.backend.session_control import (
    NO_SESSION_REPLY,
    UNTIED_THREAD_REPLY,
    check_thread,
    clean_screen_text,
    done_thread,
    follow_up_to_session,
    forget_thread,
    from_slack_text,
    send_to_session,
    stop_thread,
)
from pan.backend.spawn import RELAYED_FOLLOW_UP_SENTENCE, desk_session_ended_reply
from pan.backend.status_message import answer_digest
from pan.core.errors import (
    CredentialsError,
    GitWorktreeError,
    NoExactMatchError,
    NotPanSessionError,
    PanError,
    SessionBusyError,
    SlackPostError,
    TargetBlockedError,
    TerminalGoneError,
    ThreadNotFoundError,
    UnsafeWorktreeError,
    UnsavedWorkError,
)
from pan.core.models import (
    AgentStatus,
    BranchStatus,
    InboxThread,
    ReplyMode,
    TerminalRead,
    ThreadRecord,
    WorkerStatus,
    WorktreeStatus,
)
from pan.data.threadmap import FileThreadMap
from pan.frontend.hooks.stop import stop_hook
from pan.frontend.slack_format import to_slack_mrkdwn

_FIXED_NOW = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)
_OLD_THREAD = "1700000000.000100"
_NEW_THREAD = "1700000000.000200"


class FixedClock:
    def now(self) -> datetime:
        return _FIXED_NOW


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
    "reply_in_slack, inbox_url, expected_inbox_line",
    [
        (False, None, 'Inbox thread: "pan-7: fix the login page"'),
        (
            True,
            "http://localhost:19441/#thread=abc",
            "Inbox thread: <http://localhost:19441/#thread=abc|pan-7: fix the login page>",
        ),
    ],
    ids=["inbox-name", "inbox-link-and-slack-replies"],
)
def test_send_to_session_sends_ties_and_posts_a_status(
    tmp_path: Path, reply_in_slack: bool, inbox_url: str | None, expected_inbox_line: str
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
    # One status message in the new thread, nothing else in it; the Inbox link (on this
    # machine) and the terminal go only to the requester.
    assert slack.posts == []
    (status_post,) = slack.status_posts
    assert (status_post.thread_ts, status_post.text) == (_NEW_THREAD, "Working: add the tests")
    assert "localhost" not in str(status_post.blocks)
    assert tied.status_message_ts == "status-1"
    assert tied.status_label == "add the tests"
    assert slack.ephemerals == [
        (
            "C0TEST",
            _NEW_THREAD,
            "U0TEST",
            f"Details for pan-7 (only visible to you)\n{expected_inbox_line}\nTerminal: t7",
        )
    ]


def test_send_to_a_session_from_another_thread_never_edits_that_threads_status(
    tmp_path: Path,
) -> None:
    thread_map = _thread_map(tmp_path, _record(status_message_ts="1700000000.000900"))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    slack = RecordingSlack()

    tied = _send(terminal_host, thread_map, slack)

    assert slack.status_updates == []
    assert [post.thread_ts for post in slack.status_posts] == [_NEW_THREAD]
    assert tied.status_message_ts == "status-1"


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
    assert slack.texts() == []
    assert slack.status_texts() == ["Working: add the tests"]
    assert slack.ephemerals[0][3].split("\n")[1] == (
        'Inbox thread: its existing one, or "desk-api: add the tests"'
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
    assert slack.texts() == []
    assert slack.ephemerals[0][3].split("\n")[1] == (
        "Inbox thread: <https://inbox.example/threads/thr_9|desk-api: fix the build>"
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
        clock=FixedClock(),
        screen_lines=50,
    )


@pytest.mark.parametrize(
    "live_terminal_name, expected_state",
    [("pan-7", "Finished"), ("pan-8", "Stopped")],
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

    assert slack.status_texts() == [expected_state]


@pytest.mark.parametrize(
    "agent_status, expected_state",
    [
        (AgentStatus.IDLE, "Finished: fix the login page"),
        (AgentStatus.WORKING, "Working: fix the login page"),
        (AgentStatus.BLOCKED, "Waiting for you: fix the login page"),
    ],
    ids=["idle", "working", "blocked"],
)
def test_check_edits_the_status_posts_the_clean_answer_and_sends_the_link_privately(
    tmp_path: Path, agent_status: AgentStatus, expected_state: str
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
    slack = RecordingSlack()

    _check(thread_map, terminal_host, slack)

    # The answer alone, in mrkdwn: no session name, state line or Inbox link around it.
    assert slack.posts == [("C0TEST", _OLD_THREAD, "Fixed the *login* page.")]
    assert slack.status_posts == []
    assert [(ts, text) for _channel, ts, _blocks, text in slack.status_updates] == [
        ("1700000000.000900", expected_state)
    ]
    assert slack.ephemerals == [
        (
            "C0TEST",
            _OLD_THREAD,
            "U0TEST",
            "Details for pan-7 (only visible to you)\n"
            "Inbox thread: <http://localhost:19441/#thread=abc|pan-7: fix the login page>\n"
            "Terminal: t7",
        )
    ]
    assert terminal_host.reads == []


def test_check_posts_a_new_status_when_the_old_one_cannot_be_edited(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record(status_message_ts="1700000000.000900"))
    slack = RecordingSlack()
    slack.update_error = SlackPostError("message_not_found")

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), slack)

    assert slack.status_texts() == ["Finished"]
    stored = thread_map.get(_OLD_THREAD)
    assert stored is not None
    assert stored.status_message_ts == "status-1"


def test_check_without_a_transcript_posts_screen_text(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    terminal_host.read_result = TerminalRead(text="last screen lines", complete=True)
    slack = RecordingSlack()

    _check(thread_map, terminal_host, slack)

    assert slack.texts() == ["Screen text (no transcript yet):\n```\nlast screen lines\n```"]
    assert terminal_host.reads == [("t7", 50)]


def test_check_on_an_ended_session_with_no_answer_only_updates_the_status(
    tmp_path: Path,
) -> None:
    thread_map = _thread_map(tmp_path, _record(inbox_thread_name=None, requester="U0TEST"))
    slack = RecordingSlack()

    _check(thread_map, _host(), slack)

    assert slack.texts() == []
    assert slack.status_texts() == ["Stopped"]
    assert slack.ephemerals[0][3] == (
        "Details for pan-7 (only visible to you)\nInbox thread: none yet\nTerminal: t7"
    )


def test_check_splits_a_long_answer_into_labelled_parts(tmp_path: Path) -> None:
    long_answer = "\n".join(f"line {number} of the answer" for number in range(400))
    record = _record(transcript_path=_transcript_answering(tmp_path, long_answer))
    thread_map = _thread_map(tmp_path, record)
    slack = RecordingSlack()

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), slack)

    texts = slack.texts()
    assert len(texts) > 1
    assert texts[0].startswith(f"(1/{len(texts)})\nline 0 of the answer")
    assert texts[-1].endswith("line 399 of the answer")


@pytest.mark.parametrize("records", [[], [_record(tied=False)]], ids=["no-record", "untied"])
def test_check_on_an_untied_thread_replies_one_line(
    tmp_path: Path, records: list[ThreadRecord]
) -> None:
    thread_map = _thread_map(tmp_path, *records)
    slack = RecordingSlack()

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), slack)

    assert slack.posts == [("C0TEST", _OLD_THREAD, UNTIED_THREAD_REPLY)]
    assert slack.status_posts == []
    assert slack.ephemerals == []


@pytest.mark.parametrize(
    "found, expected_inbox_line",
    [
        (
            InboxThread(thread_id="thr_9", name="desk-api: fix the build", url=None),
            'Inbox thread: "desk-api: fix the build"',
        ),
        (
            InboxThread(
                thread_id="thr_9",
                name="desk-api: fix the build",
                url="https://inbox.example/threads/thr_9",
            ),
            "Inbox thread: <https://inbox.example/threads/thr_9|desk-api: fix the build>",
        ),
        (None, 'Inbox thread: its existing one, or "desk-api: fix the build"'),
    ],
    ids=["found-without-link", "found-with-link", "not-found"],
)
def test_check_on_a_desk_session_looks_its_inbox_thread_up_by_name(
    tmp_path: Path, found: InboxThread | None, expected_inbox_line: str
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
    slack = RecordingSlack()

    _check(thread_map, _host(make_terminal("t9", name="desk-api")), slack, inbox_directory)

    assert slack.ephemerals[0][3].split("\n")[1] == expected_inbox_line
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
    # The follow-up is the thread's task now, and its status message says Working.
    assert slack.status_texts() == ["Working: and the docs"]
    stored = thread_map.get(_OLD_THREAD)
    assert stored is not None
    assert stored.status_label == "and the docs"
    assert stored.task_started_at == _FIXED_NOW


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
    assert slack.status_posts == []


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
        thread_ts=_OLD_THREAD,
        channel="C0TEST",
        text="and the docs",
        terminal_host=terminal_host,
        thread_map=thread_map,
        slack=RecordingSlack(),
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
    slack = RecordingSlack()

    _check(thread_map, terminal_host, slack)

    assert slack.texts() == [
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


_MAIN_REPO = Path("/repos/app")
_PAN_SETTINGS = ".claude/settings.json"
_KEPT_HINT = "Push or commit them, or send `@pan done --discard`."


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
    slack: RecordingSlack,
    *,
    discard: bool = False,
) -> None:
    done_thread(
        thread_ts=_OLD_THREAD,
        channel="C0TEST",
        discard=discard,
        worktree_base=tmp_path / "wt",
        fallback_repo=tmp_path / "orchestrator",
        thread_map=thread_map,
        terminal_host=terminal_host,
        git=git,
        slack=slack,
        clock=FixedClock(),
    )


def _tied_record(thread_map: FileThreadMap) -> ThreadRecord:
    record = thread_map.get(_OLD_THREAD)
    assert record is not None
    return record


@pytest.mark.parametrize("records", [[], [_record(tied=False)]], ids=["no-record", "untied"])
def test_done_in_a_thread_with_no_session_does_nothing(
    tmp_path: Path, records: list[ThreadRecord]
) -> None:
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    git = _git_with()
    slack = RecordingSlack()

    _done(tmp_path, _thread_map(tmp_path, *records), terminal_host, git, slack)

    assert slack.texts() == [NO_SESSION_REPLY]
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
    slack = RecordingSlack()

    _done(tmp_path, thread_map, terminal_host, git, slack, discard=True)

    assert slack.texts() == [
        "Untied this thread from desk-x; pan never closes sessions it didn't start."
    ]
    record = _tied_record(thread_map)
    assert record.tied is False
    assert record.status is WorkerStatus.IDLE
    assert terminal_host.removed == []
    assert git.calls == []


@pytest.mark.parametrize(
    "agent_status, expected_error, expected_reply",
    [
        (
            AgentStatus.WORKING,
            SessionBusyError,
            "pan-7 is still working; send `@pan done` when it finishes, "
            "or `@pan done --discard` to stop it now.",
        ),
        (
            AgentStatus.BLOCKED,
            TargetBlockedError,
            "pan-7 is waiting on a prompt; answer it at the terminal and send `@pan done` "
            "again, or `@pan done --discard` to stop it now.",
        ),
    ],
    ids=["working", "waiting-on-a-prompt"],
)
def test_done_refuses_a_session_that_is_not_finished(
    tmp_path: Path,
    agent_status: AgentStatus,
    expected_error: type[Exception],
    expected_reply: str,
) -> None:
    thread_map = _thread_map(tmp_path, _record(worktree_path=_pan_worktree(tmp_path)))
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=agent_status))
    git = _git_with()
    slack = RecordingSlack()

    with pytest.raises(expected_error):
        _done(tmp_path, thread_map, terminal_host, git, slack)

    assert slack.texts() == [expected_reply]
    assert _tied_record(thread_map).tied is True
    assert terminal_host.removed == []
    assert git.calls == []


@pytest.mark.parametrize(
    "status, branch, expected_counts",
    [
        (_worktree_status(modified_paths=["README.md"]), BranchStatus(), "1 uncommitted file"),
        (
            _worktree_status(untracked_paths=[], modified_paths=[_PAN_SETTINGS]),
            BranchStatus(),
            "1 uncommitted file",
        ),
        (
            _worktree_status(untracked_paths=[_PAN_SETTINGS, "notes.txt"], modified_paths=["a"]),
            BranchStatus(commits_not_in_base=1),
            "2 uncommitted files and 1 unpushed commit",
        ),
        (
            _worktree_status(),
            BranchStatus(upstream="origin/pan-7", commits_not_on_upstream=2, commits_not_in_base=3),
            "2 unpushed commits",
        ),
        (_worktree_status(branch=None, detached_commits=1), BranchStatus(), "1 unpushed commit"),
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
    tmp_path: Path, status: WorktreeStatus, branch: BranchStatus, expected_counts: str
) -> None:
    worktree_path = _pan_worktree(tmp_path)
    thread_map = _thread_map(tmp_path, _record(worktree_path=worktree_path))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    git = _git_with(status, branch)
    slack = RecordingSlack()

    with pytest.raises(UnsavedWorkError):
        _done(tmp_path, thread_map, terminal_host, git, slack)

    assert slack.texts() == [f"pan-7 has {expected_counts}; kept it. {_KEPT_HINT}"]
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
    slack = RecordingSlack()

    _done(tmp_path, thread_map, terminal_host, git, slack, discard=discard)

    assert terminal_host.removed == ["t7"]
    assert git.calls == [
        ("status", worktree_path),
        ("branch_status", _MAIN_REPO, "pan-7"),
        ("remove", worktree_path, expected_remove_force),
        ("delete_branch", _MAIN_REPO, "pan-7", expected_delete_force),
    ]
    assert slack.texts() == ["Closed pan-7 and removed its worktree and branch."]
    assert slack.status_texts() == ["Closed"]
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
    slack = RecordingSlack()

    _done(tmp_path, thread_map, terminal_host, git, slack)

    assert terminal_host.removed == []
    assert ("remove", worktree_path, True) in git.calls
    assert slack.texts() == ["Closed pan-7 and removed its worktree and branch."]


def test_done_removes_the_worktree_when_its_branch_is_already_gone(tmp_path: Path) -> None:
    worktree_path = _pan_worktree(tmp_path)
    thread_map = _thread_map(tmp_path, _record(worktree_path=worktree_path))
    git = FakeGitWorktree(status=_worktree_status(branch=None))
    slack = RecordingSlack()

    _done(tmp_path, thread_map, _host(), git, slack)

    assert git.calls == [
        ("status", worktree_path),
        ("branch_status", _MAIN_REPO, "pan-7"),
        ("remove", worktree_path, True),
    ]
    assert slack.texts() == ["Closed pan-7 and removed its worktree."]


@pytest.mark.parametrize(
    "recorded_repo, repo_with_branch, expected_repo, expected_reply",
    [
        (
            "repos/app",
            "repos/app",
            "repos/app",
            "Closed pan-7 and removed its branch; its worktree was already gone.",
        ),
        (
            None,
            "orchestrator",
            "orchestrator",
            "Closed pan-7 and removed its branch; its worktree was already gone.",
        ),
        (
            "repos/app",
            None,
            "repos/app",
            "Closed pan-7; its worktree and branch were already gone.",
        ),
        (None, None, None, "Closed pan-7; its worktree was already gone."),
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
    expected_reply: str,
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
    slack = RecordingSlack()

    _done(tmp_path, thread_map, terminal_host, git, slack)

    writes = [call for call in git.calls if call[0] in ("prune", "delete_branch")]
    if expected_repo is None:
        assert writes == []
    else:
        expected_writes: list[tuple[object, ...]] = [("prune", tmp_path / expected_repo)]
        if repo_with_branch is not None:
            expected_writes.append(("delete_branch", tmp_path / expected_repo, "pan-7", True))
        assert writes == expected_writes
    assert terminal_host.removed == ["t7"]
    assert slack.texts() == [expected_reply]
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
    slack = RecordingSlack()

    with pytest.raises(UnsavedWorkError):
        _done(tmp_path, thread_map, _host(), git, slack)

    assert slack.texts() == [f"pan-7 has 1 unpushed commit; kept it. {_KEPT_HINT}"]
    assert git.calls == [("branch_status", repo, "pan-7")]


@pytest.mark.parametrize(
    "folder_name, status, git_errors, expected_error, expected_reply",
    [
        (
            "outside",
            _worktree_status(),
            {},
            UnsafeWorktreeError,
            "pan-7's folder is not in pan's worktree folder; pan never removes it.",
        ),
        (
            "wt/pan-7",
            _worktree_status(is_main_worktree=True),
            {},
            UnsafeWorktreeError,
            "pan-7's folder is a main repository; pan never removes it.",
        ),
        (
            "wt/pan-7",
            None,
            {"status": GitWorktreeError("not a git repository")},
            GitWorktreeError,
            "Could not read pan-7's worktree; kept it.",
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
    expected_reply: str,
) -> None:
    folder = tmp_path / folder_name
    folder.mkdir(parents=True)
    thread_map = _thread_map(tmp_path, _record(worktree_path=folder))
    terminal_host = _host(make_terminal("t7", name="pan-7"))
    git = FakeGitWorktree(status=status, errors=git_errors)
    slack = RecordingSlack()

    with pytest.raises(expected_error):
        _done(tmp_path, thread_map, terminal_host, git, slack)

    assert slack.texts() == [expected_reply]
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
    slack = RecordingSlack()

    with pytest.raises(GitWorktreeError):
        _done(tmp_path, thread_map, terminal_host, git, slack)

    assert terminal_host.removed == ["t7"]
    assert slack.texts() == [
        "Closed pan-7, but could not remove its worktree or branch; "
        f"send `@pan done` to try again, or remove {worktree_path} by hand."
    ]
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
        _done(tmp_path, thread_map, _host(make_terminal("t7", name="pan-7")), git, RecordingSlack())
    worktree_path.rmdir()
    git.errors.clear()
    git.calls.clear()
    slack = RecordingSlack()

    _done(tmp_path, thread_map, _host(), git, slack)

    assert git.calls == [
        ("branch_status", repo, "pan-7"),
        ("prune", repo),
        ("delete_branch", repo, "pan-7", True),
    ]
    assert slack.texts() == ["Closed pan-7 and removed its branch; its worktree was already gone."]
    record = _tied_record(thread_map)
    assert record.status is WorkerStatus.DONE
    assert record.tied is False


@pytest.mark.parametrize(
    "reply_mode, expected_posts_after_check",
    [
        (ReplyMode.SLACK, ["Fixed the login page."]),
        (ReplyMode.ON_DEMAND, ["Fixed the login page."]),
    ],
    ids=["reply-in-slack-stop-hook-posted-it", "on-demand-check-posts-it"],
)
def test_check_never_reposts_the_answer_already_posted(
    tmp_path: Path, reply_mode: ReplyMode, expected_posts_after_check: list[str]
) -> None:
    # In the reply-in-Slack mode the Stop hook posts the answer, so check only updates the
    # status; on demand nothing was posted, so check posts it. Either way it appears once.
    transcript = _transcript_answering(tmp_path, "Fixed the login page.")
    thread_map = _thread_map(tmp_path, _record(reply_mode=reply_mode))
    terminal_host = _host(make_terminal("t7", name="pan-7", agent_status=AgentStatus.DONE))
    slack = RecordingSlack()
    stop_payload = json.dumps(
        {"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(transcript)}
    )

    stop_hook(
        _OLD_THREAD,
        "C0TEST",
        thread_map,
        slack,
        stdin=io.StringIO(stop_payload),
        session_name="pan-7",
        clock=FixedClock(),
    )
    thread_map.record_session(_OLD_THREAD, "s1", transcript, session_name="pan-7")
    _check(thread_map, terminal_host, slack)
    _check(thread_map, terminal_host, slack)

    assert slack.texts() == expected_posts_after_check
    # hc's `done` on a live terminal is a finished turn, never Closed.
    assert set(slack.status_texts()) == {"Finished"}


def test_check_posts_a_newer_answer_than_the_one_already_posted(tmp_path: Path) -> None:
    record = _record(
        transcript_path=_transcript_answering(tmp_path, "Second answer."),
        last_answer_digest=answer_digest("First answer."),
    )
    thread_map = _thread_map(tmp_path, record)
    slack = RecordingSlack()

    _check(thread_map, _host(make_terminal("t7", name="pan-7")), slack)

    assert slack.texts() == ["Second answer."]
    stored = thread_map.get(_OLD_THREAD)
    assert stored is not None
    assert stored.last_answer_digest == answer_digest("Second answer.")


def _stop(
    thread_map: FileThreadMap, terminal_host: FakeTerminalHost, slack: RecordingSlack
) -> None:
    stop_thread(
        thread_ts=_OLD_THREAD,
        thread_map=thread_map,
        terminal_host=terminal_host,
        open_slack=lambda: slack,
        clock=FixedClock(),
    )


@pytest.mark.parametrize("terminal_address", ["t7", ""], ids=["recorded", "never-launched"])
def test_stop_thread_removes_the_terminal_and_marks_failed(
    tmp_path: Path, terminal_address: str
) -> None:
    thread_map = _thread_map(tmp_path, _record(terminal_address=terminal_address))
    terminal_host = _host(make_terminal("t7", name="pan-7"))

    _stop(thread_map, terminal_host, RecordingSlack())

    assert terminal_host.removed == ([terminal_address] if terminal_address else [])
    stopped = thread_map.get(_OLD_THREAD)
    assert stopped is not None
    assert stopped.status is WorkerStatus.FAILED


def test_stop_thread_edits_the_threads_status_message_to_stopped(tmp_path: Path) -> None:
    thread_map = _thread_map(
        tmp_path,
        _record(status_message_ts="1700000000.000900", status_label="fix the bug"),
    )
    slack = RecordingSlack()

    _stop(thread_map, _host(make_terminal("t7", name="pan-7")), slack)

    assert [(ts, text) for _channel, ts, _blocks, text in slack.status_updates] == [
        ("1700000000.000900", "Stopped: fix the bug")
    ]
    assert slack.posts == []


def test_stop_thread_still_stops_when_slack_cannot_be_reached(tmp_path: Path) -> None:
    thread_map = _thread_map(tmp_path, _record())
    terminal_host = _host(make_terminal("t7", name="pan-7"))

    def no_credentials() -> RecordingSlack:
        raise CredentialsError("no credentials")

    stop_thread(
        thread_ts=_OLD_THREAD,
        thread_map=thread_map,
        terminal_host=terminal_host,
        open_slack=no_credentials,
        clock=FixedClock(),
    )

    assert terminal_host.removed == ["t7"]
    stopped = thread_map.get(_OLD_THREAD)
    assert stopped is not None
    assert stopped.status is WorkerStatus.FAILED


def test_stop_thread_never_stops_a_session_pan_did_not_start(tmp_path: Path) -> None:
    thread_map = FileThreadMap(tmp_path / "threads.json", FixedClock())
    thread_map.tie(_record(terminal_name="desk-api", terminal_address="t9", started_by_pan=False))
    terminal_host = _host(make_terminal("t9", name="desk-api"))
    slack = RecordingSlack()

    with pytest.raises(NotPanSessionError):
        _stop(thread_map, terminal_host, slack)

    assert terminal_host.removed == []
    kept = thread_map.get(_OLD_THREAD)
    assert kept is not None
    assert kept.tied is False
    assert kept.status is WorkerStatus.IDLE
    assert slack.status_updates == []


def test_stop_thread_without_a_record_raises(tmp_path: Path) -> None:
    with pytest.raises(ThreadNotFoundError):
        _stop(_thread_map(tmp_path), _host(), RecordingSlack())
