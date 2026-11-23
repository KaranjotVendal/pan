from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.errors import (
    NotPanSessionError,
    PanError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
)
from pan.models import AgentStatus, ReplyMode, TerminalRead, ThreadRecord, WorkerStatus
from pan.session_control import (
    UNTIED_THREAD_REPLY,
    check_thread,
    forget_thread,
    send_to_session,
)
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

    assert terminal_host.sent == [("t7", "add the tests")]
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
            "desk-api",
            [make_terminal("t9", name="desk-api")],
            [_record()],
            NotPanSessionError,
            "desk-api is not a pan session; reaching your own sessions comes next",
        ),
        (
            "pan-7",
            [make_terminal("t7", name="pan-7")],
            [_record(started_by_pan=False)],
            NotPanSessionError,
            "pan-7 is not a pan session; reaching your own sessions comes next",
        ),
    ],
    ids=["unknown-close-match", "unknown-no-match", "ambiguous", "desk-session", "not-pan-record"],
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
    thread_map: FileThreadMap, terminal_host: FakeTerminalHost, slack: RecordingSlack
) -> None:
    check_thread(
        thread_ts=_OLD_THREAD,
        channel="C0TEST",
        thread_map=thread_map,
        terminal_host=terminal_host,
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
