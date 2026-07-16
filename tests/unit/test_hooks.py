from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pan.errors import ThreadNotFoundError
from pan.hooks.notification import notification_hook
from pan.hooks.stop import stop_hook
from pan.models import ThreadRecord, WorkerStatus


class FakeThreadMap:
    def __init__(self, *, seed_thread: str | None = "1718000000.000200") -> None:
        self.records: dict[str, ThreadRecord] = {}
        self.status_updates: list[tuple[str, WorkerStatus]] = []
        if seed_thread is not None:
            now = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
            self.records[seed_thread] = ThreadRecord(
                thread_ts=seed_thread,
                workspace_name="pan-task",
                workspace_id="ws1",
                worktree_path=Path("/tmp/wt"),
                created_at=now,
                updated_at=now,
            )

    def get(self, thread_ts: str) -> ThreadRecord | None:
        return self.records.get(thread_ts)

    def put(self, record: ThreadRecord) -> None:  # pragma: no cover
        self.records[record.thread_ts] = record

    def update_status(self, thread_ts: str, status: WorkerStatus) -> None:
        self.status_updates.append((thread_ts, status))


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


def test_stop_hook_posts_summary_and_marks_done() -> None:
    thread_map, slack = FakeThreadMap(), FakeSlack()
    payload = json.dumps({"session_id": "s1", "hook_event_name": "Stop", "transcript_path": None})

    stop_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts == [("C1", _THREAD, "Worker finished.")]
    assert thread_map.status_updates == [(_THREAD, WorkerStatus.DONE)]


def test_stop_hook_falls_back_to_default_when_transcript_missing(tmp_path: Path) -> None:
    thread_map, slack = FakeThreadMap(), FakeSlack()
    payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "transcript_path": str(tmp_path / "absent.jsonl"),
        }
    )

    stop_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts[0][2] == "Worker finished."


def test_notification_hook_falls_back_to_default_question() -> None:
    thread_map, slack = FakeThreadMap(), FakeSlack()
    payload = json.dumps({"session_id": "s1", "hook_event_name": "Notification"})

    notification_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts == [("C1", _THREAD, "Worker needs input.")]
    assert thread_map.status_updates == [(_THREAD, WorkerStatus.BLOCKED)]


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
    thread_map, slack = FakeThreadMap(), FakeSlack()
    payload = json.dumps(
        {"session_id": "s1", "hook_event_name": "Stop", "transcript_path": str(transcript)}
    )

    stop_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts[0][2] == "final answer"


def test_notification_hook_posts_question_and_marks_blocked() -> None:
    thread_map, slack = FakeThreadMap(), FakeSlack()
    payload = json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "Notification",
            "message": "Claude needs your approval to run rm",
        }
    )

    notification_hook(_THREAD, "C1", thread_map, slack, stdin=io.StringIO(payload))

    assert slack.posts == [("C1", _THREAD, "Claude needs your approval to run rm")]
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
