from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pan.errors import ThreadNotFoundError
from pan.hooks.inbox_link import InboxLink, inbox_link_hook, parse_inbox_link
from pan.hooks.notification import notification_hook
from pan.hooks.session_start import session_start_hook
from pan.hooks.stop import stop_hook
from pan.models import ReplyMode, ThreadRecord, WorkerStatus


class FakeThreadMap:
    def __init__(
        self,
        *,
        seed_thread: str | None = "1718000000.000200",
        reply_mode: ReplyMode = ReplyMode.ON_DEMAND,
    ) -> None:
        self.records: dict[str, ThreadRecord] = {}
        self.status_updates: list[tuple[str, WorkerStatus]] = []
        self.sessions: list[tuple[str, str, Path | None]] = []
        self.inbox_threads: list[tuple[str, str, str]] = []
        if seed_thread is not None:
            now = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
            self.records[seed_thread] = ThreadRecord(
                thread_ts=seed_thread,
                terminal_name="pan-task",
                terminal_address="t3",
                channel="C1",
                worktree_path=Path("/tmp/wt"),
                created_at=now,
                updated_at=now,
                reply_mode=reply_mode,
                inbox_thread_name="pan-task: do the thing",
            )

    def get(self, thread_ts: str) -> ThreadRecord | None:
        return self.records.get(thread_ts)

    def put(self, record: ThreadRecord) -> None:  # pragma: no cover
        self.records[record.thread_ts] = record

    def update_status(self, thread_ts: str, status: WorkerStatus) -> None:
        self.status_updates.append((thread_ts, status))

    def record_session(self, thread_ts: str, session_id: str, transcript_path: Path | None) -> None:
        self.sessions.append((thread_ts, session_id, transcript_path))

    def record_inbox_thread(self, thread_ts: str, inbox_thread_id: str, url: str) -> None:
        self.inbox_threads.append((thread_ts, inbox_thread_id, url))
        record = self.records[thread_ts]
        self.records[thread_ts] = record.model_copy(
            update={"inbox_thread_id": inbox_thread_id, "inbox_thread_url": url}
        )


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        self.posts.append((channel, thread_ts, text))

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError


_THREAD = "1718000000.000200"


def test_stop_hook_on_demand_marks_idle_and_posts_nothing() -> None:
    thread_map, slack = FakeThreadMap(), FakeSlack()
    payload = json.dumps({"session_id": "s1", "hook_event_name": "Stop", "transcript_path": None})

    stop_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts == []
    assert thread_map.status_updates == [(_THREAD, WorkerStatus.IDLE)]


def test_stop_hook_in_reply_in_slack_mode_posts_and_marks_idle() -> None:
    thread_map, slack = FakeThreadMap(reply_mode=ReplyMode.SLACK), FakeSlack()
    payload = json.dumps({"session_id": "s1", "hook_event_name": "Stop", "transcript_path": None})

    stop_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts == [("C1", _THREAD, "Worker finished.")]
    assert thread_map.status_updates == [(_THREAD, WorkerStatus.IDLE)]


def test_stop_hook_falls_back_to_default_when_transcript_missing(tmp_path: Path) -> None:
    thread_map, slack = FakeThreadMap(reply_mode=ReplyMode.SLACK), FakeSlack()
    payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "transcript_path": str(tmp_path / "absent.jsonl"),
        }
    )

    stop_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts[0][2] == "Worker finished."


def test_stop_hook_extracts_last_assistant_text_from_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "first"}]},
                    }
                ),
                json.dumps({"type": "user", "message": {"content": []}}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "final answer"}]},
                    }
                ),
            ]
        )
    )
    thread_map, slack = FakeThreadMap(reply_mode=ReplyMode.SLACK), FakeSlack()
    payload = json.dumps(
        {"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(transcript)}
    )

    stop_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts[0][2] == "final answer"


@pytest.mark.parametrize(
    "reply_mode, message, expected_posts",
    [
        (ReplyMode.ON_DEMAND, "Claude needs your approval to run rm", []),
        (
            ReplyMode.SLACK,
            "Claude needs your approval to run rm",
            [("C1", _THREAD, "Claude needs your approval to run rm")],
        ),
        (ReplyMode.SLACK, None, [("C1", _THREAD, "Worker needs input.")]),
    ],
    ids=["on-demand-quiet", "reply-in-slack-posts", "reply-in-slack-default-question"],
)
def test_notification_hook_marks_blocked_and_posts_only_in_reply_in_slack_mode(
    reply_mode: ReplyMode, message: str | None, expected_posts: list[tuple[str, str, str]]
) -> None:
    thread_map, slack = FakeThreadMap(reply_mode=reply_mode), FakeSlack()
    payload_fields: dict[str, str] = {"session_id": "s1", "hook_event_name": "Notification"}
    if message is not None:
        payload_fields["message"] = message

    notification_hook(
        _THREAD, "C1", thread_map, slack, stdin=io.StringIO(json.dumps(payload_fields))
    )

    assert slack.posts == expected_posts
    assert thread_map.status_updates == [(_THREAD, WorkerStatus.BLOCKED)]


@pytest.mark.parametrize(
    "hook, payload",
    [
        (stop_hook, {"hook_event_name": "Stop"}),
        (notification_hook, {"hook_event_name": "Notification", "message": "q"}),
    ],
    ids=["stop", "notification"],
)
def test_hook_raises_when_thread_unknown(hook: object, payload: dict) -> None:
    thread_map, slack = FakeThreadMap(seed_thread=None), FakeSlack()

    with pytest.raises(ThreadNotFoundError):
        hook(  # type: ignore[operator]
            _THREAD, "C1", thread_map, slack, stdin=io.StringIO(json.dumps(payload))
        )

    # Nothing is posted or transitioned when the thread is unknown.
    assert slack.posts == []
    assert thread_map.status_updates == []


def test_session_start_hook_records_session_id_and_transcript_path() -> None:
    thread_map = FakeThreadMap()
    payload = json.dumps(
        {
            "session_id": "s-42",
            "transcript_path": "/home/.claude/projects/wt/s-42.jsonl",
            "hook_event_name": "SessionStart",
            "source": "startup",
        }
    )

    session_start_hook(_THREAD, thread_map, stdin=io.StringIO(payload))

    assert thread_map.sessions == [(_THREAD, "s-42", Path("/home/.claude/projects/wt/s-42.jsonl"))]
    assert thread_map.status_updates == []


def test_session_start_hook_without_session_id_records_nothing() -> None:
    thread_map = FakeThreadMap()

    session_start_hook(_THREAD, thread_map, stdin=io.StringIO(json.dumps({})))

    assert thread_map.sessions == []


_INBOX_RESULT = {
    "message_id": "msg_1",
    "thread_id": "thr_1",
    "new_thread": True,
    "cursor": "c1",
    "url": "https://inbox.example/t/thr_1",
}


def _post_tool_use(tool_response: object, thread: str | None = "pan-task: do the thing") -> str:
    tool_input: dict[str, str] = {"action": "send_message", "body": "starting"}
    if thread is not None:
        tool_input["thread"] = thread
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__plannotator__plannotator_inbox",
            "tool_input": tool_input,
            "tool_response": tool_response,
        }
    )


@pytest.mark.parametrize(
    "tool_response",
    [
        _INBOX_RESULT,
        json.dumps(_INBOX_RESULT),
        [{"type": "text", "text": json.dumps(_INBOX_RESULT)}],
        {"content": [{"type": "text", "text": json.dumps(_INBOX_RESULT)}]},
        {"structuredContent": _INBOX_RESULT, "content": []},
        [{"type": "text", "text": "Sent."}, {"type": "text", "text": json.dumps(_INBOX_RESULT)}],
    ],
    ids=[
        "dict",
        "json-string",
        "content-blocks",
        "content-key",
        "structured-content",
        "second-block",
    ],
)
def test_parse_inbox_link_finds_thread_id_and_url(tool_response: object) -> None:
    link = parse_inbox_link(_post_tool_use(tool_response))

    assert link == InboxLink(thread_id="thr_1", url="https://inbox.example/t/thr_1")


@pytest.mark.parametrize(
    "raw_payload",
    [
        "not json",
        json.dumps(["a", "list"]),
        json.dumps({"tool_name": "x"}),
        _post_tool_use(None),
        _post_tool_use("plain text result"),
        _post_tool_use({"thread_id": "thr_1"}),
        _post_tool_use({"thread_id": "thr_1", "url": ""}),
        _post_tool_use({"thread_id": 7, "url": "https://inbox.example/t/thr_1"}),
        _post_tool_use([{"type": "text", "text": "{broken json"}]),
    ],
    ids=[
        "unparseable",
        "not-an-object",
        "no-response",
        "null-response",
        "plain-text",
        "no-url",
        "empty-url",
        "non-string-id",
        "broken-json-block",
    ],
)
def test_parse_inbox_link_returns_none_without_a_link(raw_payload: str) -> None:
    assert parse_inbox_link(raw_payload) is None


def test_inbox_link_hook_records_the_first_link_and_posts_nothing() -> None:
    thread_map = FakeThreadMap()

    inbox_link_hook(_THREAD, thread_map, stdin=io.StringIO(_post_tool_use(_INBOX_RESULT)))

    assert thread_map.inbox_threads == [(_THREAD, "thr_1", "https://inbox.example/t/thr_1")]
    assert thread_map.status_updates == []


@pytest.mark.parametrize(
    "sent_thread_name, expected_recorded",
    [
        ("pan-task: do the thing", [(_THREAD, "thr_2", "https://inbox.example/t/thr_2")]),
        ("some other thread", []),
        (None, []),
    ],
    ids=["own-thread-updates", "other-thread-kept", "reply-without-name-kept"],
)
def test_inbox_link_hook_keeps_a_recorded_link_unless_the_send_names_its_own_thread(
    sent_thread_name: str | None, expected_recorded: list[tuple[str, str, str]]
) -> None:
    thread_map = FakeThreadMap()
    thread_map.records[_THREAD] = thread_map.records[_THREAD].model_copy(
        update={"inbox_thread_id": "thr_1", "inbox_thread_url": "https://inbox.example/t/thr_1"}
    )
    second_result = {
        **_INBOX_RESULT,
        "thread_id": "thr_2",
        "url": "https://inbox.example/t/thr_2",
    }

    inbox_link_hook(
        _THREAD, thread_map, stdin=io.StringIO(_post_tool_use(second_result, sent_thread_name))
    )

    assert thread_map.inbox_threads == expected_recorded


def test_inbox_link_hook_without_a_link_records_nothing() -> None:
    thread_map = FakeThreadMap()

    inbox_link_hook(_THREAD, thread_map, stdin=io.StringIO(_post_tool_use("Sent.")))

    assert thread_map.inbox_threads == []
