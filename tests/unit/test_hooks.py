from __future__ import annotations

import io
import json
from collections.abc import Callable
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
        self.inbox_threads: list[tuple[str, str, str | None]] = []
        # The session name each update named, so a test can see which record was picked.
        self.updated_session_names: list[str | None] = []
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

    def get(self, thread_ts: str, *, session_name: str | None = None) -> ThreadRecord | None:
        record = self.records.get(thread_ts)
        if record is None or (session_name is not None and record.terminal_name != session_name):
            return None
        return record

    def put(self, record: ThreadRecord) -> None:  # pragma: no cover
        self.records[record.thread_ts] = record

    def update_status(
        self, thread_ts: str, status: WorkerStatus, *, session_name: str | None = None
    ) -> None:
        self.status_updates.append((thread_ts, status))
        self.updated_session_names.append(session_name)

    def record_session(
        self,
        thread_ts: str,
        session_id: str,
        transcript_path: Path | None,
        *,
        session_name: str | None = None,
    ) -> None:
        self.sessions.append((thread_ts, session_id, transcript_path))
        self.updated_session_names.append(session_name)

    def record_inbox_thread(
        self,
        thread_ts: str,
        inbox_thread_id: str,
        url: str | None,
        *,
        session_name: str | None = None,
    ) -> None:
        self.inbox_threads.append((thread_ts, inbox_thread_id, url))
        self.updated_session_names.append(session_name)
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


@pytest.mark.parametrize(
    "hook, payload",
    [
        (stop_hook, {"hook_event_name": "Stop", "transcript_path": None}),
        (
            notification_hook,
            {
                "hook_event_name": "Notification",
                "notification_type": "permission_prompt",
                "message": "q",
            },
        ),
    ],
    ids=["stop", "notification"],
)
def test_hook_of_a_session_whose_thread_moved_on_only_updates_its_own_record(
    hook: object, payload: dict
) -> None:
    # The thread was tied to another session (new: or to <name>:), so this session's record
    # is untied: its hooks still reach it by session name but post nothing to the thread.
    thread_map, slack = FakeThreadMap(reply_mode=ReplyMode.SLACK), FakeSlack()
    thread_map.records[_THREAD] = thread_map.records[_THREAD].model_copy(update={"tied": False})

    hook(  # type: ignore[operator]
        _THREAD,
        "C1",
        thread_map,
        slack,
        stdin=io.StringIO(json.dumps(payload)),
        session_name="pan-task",
    )

    assert slack.posts == []
    assert thread_map.updated_session_names == ["pan-task"]


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
    payload_fields: dict[str, str] = {
        "session_id": "s1",
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
    }
    if message is not None:
        payload_fields["message"] = message

    notification_hook(
        _THREAD, "C1", thread_map, slack, stdin=io.StringIO(json.dumps(payload_fields))
    )

    assert slack.posts == expected_posts
    assert thread_map.status_updates == [(_THREAD, WorkerStatus.BLOCKED)]


@pytest.mark.parametrize(
    "notification_type, message, expected_statuses",
    [
        ("permission_prompt", "Claude needs your permission to use Bash", [WorkerStatus.BLOCKED]),
        ("elicitation_dialog", "Claude Code needs your input", [WorkerStatus.BLOCKED]),
        ("idle_prompt", "Claude is waiting for your input", []),
        ("auth_success", "Signed in", []),
        (None, "Claude needs your permission to use Bash", [WorkerStatus.BLOCKED]),
        (None, "Claude needs your approval to run rm", [WorkerStatus.BLOCKED]),
        (None, "Claude is waiting for your input", []),
        (None, None, []),
        ("some_new_type", "Claude needs your permission to use Bash", [WorkerStatus.BLOCKED]),
        ("some_new_type", "Claude is waiting for your input", []),
    ],
    ids=[
        "permission-prompt",
        "elicitation-dialog",
        "idle-prompt",
        "auth-success",
        "no-type-permission-text",
        "no-type-approval-text",
        "no-type-idle-text",
        "no-type-no-text",
        "unknown-type-permission-text",
        "unknown-type-idle-text",
    ],
)
def test_notification_hook_marks_blocked_only_for_a_prompt_waiting_on_the_person(
    notification_type: str | None,
    message: str | None,
    expected_statuses: list[WorkerStatus],
) -> None:
    thread_map, slack = FakeThreadMap(reply_mode=ReplyMode.SLACK), FakeSlack()
    payload_fields: dict[str, str] = {"session_id": "s1", "hook_event_name": "Notification"}
    if notification_type is not None:
        payload_fields["notification_type"] = notification_type
    if message is not None:
        payload_fields["message"] = message

    notification_hook(
        _THREAD, "C1", thread_map, slack, stdin=io.StringIO(json.dumps(payload_fields))
    )

    assert [status for _, status in thread_map.status_updates] == expected_statuses
    assert len(slack.posts) == len(expected_statuses)


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


# The Inbox's real send_message text (ids and host replaced): a prose line naming the thread,
# then the structured result as JSON.
_INBOX_RESULT_TEXT = (
    "Sent to the Plannotator Inbox (thread thr_1, https://inbox.example/t/thr_1). The person's "
    "reply arrives in this session by itself, as a new message once the session is idle: end "
    "your turn, or go on with other work.\n\n"
    + json.dumps(
        {
            "message_id": "msg_1",
            "thread_id": "thr_1",
            "new_thread": True,
            "thread_name": "pan-task: do the thing",
            "project": {"id": "prj_1", "name": "repo", "root": "/repo"},
            "questions": [],
            "attachments": [],
            "superseded": [],
            "replayed": False,
            "cursor": 14786,
            "url": "https://inbox.example/t/thr_1",
        }
    )
)


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
        _INBOX_RESULT_TEXT,
        [{"type": "text", "text": _INBOX_RESULT_TEXT}],
        {"content": [{"type": "text", "text": _INBOX_RESULT_TEXT}]},
        "[draft] " + _INBOX_RESULT_TEXT,
    ],
    ids=[
        "dict",
        "json-string",
        "content-blocks",
        "content-key",
        "structured-content",
        "second-block",
        "real-text",
        "real-text-block",
        "real-text-content-key",
        "real-text-after-a-bracket",
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


_PROSE_THREAD_ID = "msg_TEST0001"
_PROSE_URL = "http://localhost:19441/#thread=msg_TEST0001"
_PROSE_LINK = InboxLink(thread_id=_PROSE_THREAD_ID, url=_PROSE_URL)
_SENT_PROSE = f"Sent to the Plannotator Inbox (thread {_PROSE_THREAD_ID}, {_PROSE_URL})."
_PROSE_REPLY_NOTE = " The person's reply arrives in this session by itself."
_OTHER_THREAD_ID = "msg_TEST0002"
_OTHER_URL = "http://localhost:19441/#thread=msg_TEST0002"
_OTHER_SENT_PROSE = f"Sent to the Plannotator Inbox (thread {_OTHER_THREAD_ID}, {_OTHER_URL})."
_REPL_INBOX_CODE = (
    'await tools.mcp__plannotator__plannotator_inbox({action: "send_message", body: "hi"})'
)


def _repl_post_tool_use(tool_response: object, code: str = _REPL_INBOX_CODE) -> str:
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "REPL",
            "tool_input": {"code": code},
            "tool_response": tool_response,
        }
    )


@pytest.mark.parametrize(
    "prose",
    [
        _SENT_PROSE,
        f"Already sent to the Plannotator Inbox (thread {_PROSE_THREAD_ID}, {_PROSE_URL}).",
        f"Sent to the Plannotator Inbox asking 1 question (thread {_PROSE_THREAD_ID}, "
        f"{_PROSE_URL}).",
        f"result:\n{_SENT_PROSE}",
    ],
    ids=["sent", "already-sent", "asking-a-question", "on-a-later-line"],
)
@pytest.mark.parametrize(
    "wrap",
    [
        lambda text: text,
        lambda text: [{"type": "text", "text": text}],
        lambda text: {"content": [{"type": "text", "text": text}]},
        lambda text: {"result": text},
    ],
    ids=["string", "content-blocks", "content-key", "repl-result-object"],
)
@pytest.mark.parametrize(
    "build_payload",
    [_repl_post_tool_use, _post_tool_use],
    ids=["repl-call", "inbox-tool-call"],
)
def test_parse_inbox_link_reads_the_prose_send_line(
    prose: str, wrap: Callable[[str], object], build_payload: Callable[[object], str]
) -> None:
    link = parse_inbox_link(build_payload(wrap(prose + _PROSE_REPLY_NOTE)))

    assert link == _PROSE_LINK


@pytest.mark.parametrize(
    "tool_response",
    [
        f"The person wrote: {_SENT_PROSE}",
        {"messages": [{"body": f"quoting > {_SENT_PROSE}"}]},
        f"sent to the plannotator inbox (thread {_PROSE_THREAD_ID}, {_PROSE_URL}).",
    ],
    ids=["mid-line-quote", "quoted-in-a-message-body", "wrong-case"],
)
def test_parse_inbox_link_ignores_a_prose_line_that_is_not_at_a_line_start(
    tool_response: object,
) -> None:
    assert parse_inbox_link(_repl_post_tool_use(tool_response)) is None


@pytest.mark.parametrize(
    "tool_response",
    [
        {"thread_id": _OTHER_THREAD_ID, "url": _OTHER_URL},
        [{"type": "text", "text": json.dumps({"thread_id": _OTHER_THREAD_ID, "url": _OTHER_URL})}],
        {"threads": [{"thread_id": _OTHER_THREAD_ID, "url": _OTHER_URL}]},
    ],
    ids=["json-result", "json-content-block", "thread-listing"],
)
def test_parse_inbox_link_takes_no_json_link_from_a_repl_call(tool_response: object) -> None:
    # A REPL call may read or list threads; only its prose send line is a link to record.
    assert parse_inbox_link(_repl_post_tool_use(tool_response)) is None


def test_parse_inbox_link_takes_the_first_prose_line() -> None:
    link = parse_inbox_link(_repl_post_tool_use([_SENT_PROSE, _OTHER_SENT_PROSE]))

    assert link == _PROSE_LINK


def test_parse_inbox_link_prefers_the_json_result_over_the_prose_line() -> None:
    # The direct tool's real send result has both; the JSON result is read first.
    assert parse_inbox_link(_post_tool_use(_INBOX_RESULT_TEXT)) == InboxLink(
        thread_id="thr_1", url="https://inbox.example/t/thr_1"
    )


@pytest.mark.parametrize(
    "code",
    [
        _REPL_INBOX_CODE,
        'await tools.plannotator.plannotator_inbox({action: "send_message", body: "hi"})',
        "const result = await helper(); return result",
    ],
    ids=["flat-tool-name", "namespaced-accessor", "prose-result-only"],
)
def test_inbox_link_hook_records_the_link_from_a_code_mode_repl_call(code: str) -> None:
    thread_map = FakeThreadMap()

    inbox_link_hook(_THREAD, thread_map, stdin=io.StringIO(_repl_post_tool_use(_SENT_PROSE, code)))

    assert thread_map.inbox_threads == [(_THREAD, _PROSE_THREAD_ID, _PROSE_URL)]


def test_inbox_link_hook_ignores_a_repl_call_that_does_not_use_the_inbox() -> None:
    # A JSON object with thread_id and url from unrelated REPL code is not an Inbox link.
    thread_map = FakeThreadMap()
    unrelated_response = {"thread_id": "gh_thread_1", "url": "https://example.invalid/t/1"}

    inbox_link_hook(
        _THREAD,
        thread_map,
        stdin=io.StringIO(_repl_post_tool_use(unrelated_response, "return fetchIssues()")),
    )

    assert thread_map.inbox_threads == []


@pytest.mark.parametrize(
    "code, expected_recorded",
    [
        (
            'await tools.mcp__plannotator__plannotator_inbox({action: "send_message", '
            'thread: "pan-task: do the thing", body: "hi"})',
            [(_THREAD, _PROSE_THREAD_ID, _PROSE_URL)],
        ),
        (
            'await tools.mcp__plannotator__plannotator_inbox({action: "send_message", '
            'thread: "some other thread", body: "hi"})',
            [],
        ),
        (_REPL_INBOX_CODE, []),
    ],
    ids=["own-thread-updates", "other-thread-kept", "reply-without-name-kept"],
)
def test_inbox_link_hook_keeps_a_recorded_link_unless_a_repl_send_names_its_own_thread(
    code: str, expected_recorded: list[tuple[str, str, str]]
) -> None:
    thread_map = FakeThreadMap()
    thread_map.records[_THREAD] = thread_map.records[_THREAD].model_copy(
        update={"inbox_thread_id": "thr_1", "inbox_thread_url": "https://inbox.example/t/thr_1"}
    )

    inbox_link_hook(_THREAD, thread_map, stdin=io.StringIO(_repl_post_tool_use(_SENT_PROSE, code)))

    assert thread_map.inbox_threads == expected_recorded
