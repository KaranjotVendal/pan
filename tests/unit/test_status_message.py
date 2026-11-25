from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from slack_fakes import RecordingSlack

from pan.backend.status_message import publish_status, send_private_details
from pan.core.errors import SlackPostError
from pan.core.models import Conversation, ReplyMode, StatusState, ThreadRecord

_NOW = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
_THREAD = "1700000000.000100"


class FixedClock:
    def now(self) -> datetime:
        return _NOW


class RecordingThreadMap:
    def __init__(self) -> None:
        self.status_messages: list[tuple[str, str, str | None]] = []

    def record_status_message(
        self, conversation: Conversation, message_ts: str, *, session_name: str | None = None
    ) -> None:
        self.status_messages.append((conversation.id, message_ts, session_name))


def _record(**updates: object) -> ThreadRecord:
    record = ThreadRecord(
        conversation=Conversation.slack(_THREAD, "C0TEST"),
        terminal_name="pan-login",
        terminal_address="t7",
        worktree_path=Path("/tmp/wt/pan-login"),
        created_at=_NOW,
        updated_at=_NOW,
        requester="U0TEST",
        status_label="fix the login page",
        task_started_at=_NOW,
    )
    return record.model_copy(update=updates)


def _publish(
    slack: RecordingSlack, thread_map: RecordingThreadMap, record: ThreadRecord
) -> ThreadRecord:
    return publish_status(
        slack=slack,
        thread_map=thread_map,  # type: ignore[arg-type]
        record=record,
        state=StatusState.FINISHED,
        clock=FixedClock(),
    )


def test_publish_status_posts_a_new_message_and_stores_its_ts() -> None:
    slack, thread_map = RecordingSlack(), RecordingThreadMap()

    published = _publish(slack, thread_map, _record())

    assert [(post.channel, post.thread_ts, post.text) for post in slack.status_posts] == [
        ("C0TEST", _THREAD, "Finished: fix the login page")
    ]
    assert thread_map.status_messages == [(_THREAD, "status-1", "pan-login")]
    assert published.status_message_ts == "status-1"
    assert slack.posts == []


def test_publish_status_edits_the_stored_message_in_place() -> None:
    slack, thread_map = RecordingSlack(), RecordingThreadMap()
    record = _record(status_message_ts="1700000000.000900", reply_mode=ReplyMode.SLACK)

    published = _publish(slack, thread_map, record)

    ((channel, ts, blocks, text),) = slack.status_updates
    assert (channel, ts, text) == ("C0TEST", "1700000000.000900", "Finished: fix the login page")
    assert blocks[1]["elements"][0]["text"].endswith("answers posted here")
    assert slack.status_posts == []
    assert thread_map.status_messages == []
    assert published is record


def test_publish_status_posts_a_new_message_when_the_edit_fails() -> None:
    slack, thread_map = RecordingSlack(), RecordingThreadMap()
    slack.update_error = SlackPostError("message_not_found")

    published = _publish(slack, thread_map, _record(status_message_ts="1700000000.000900"))

    assert len(slack.status_posts) == 1
    assert published.status_message_ts == "status-1"
    assert thread_map.status_messages == [(_THREAD, "status-1", "pan-login")]


def test_publish_status_never_raises_when_slack_cannot_be_reached() -> None:
    slack, thread_map = RecordingSlack(), RecordingThreadMap()
    slack.update_error = SlackPostError("down")
    slack.post_blocks_error = SlackPostError("down")
    record = _record(status_message_ts="1700000000.000900")

    published = _publish(slack, thread_map, record)

    assert published is record
    assert thread_map.status_messages == []


@pytest.mark.parametrize("requester", [None, ""], ids=["none", "empty"])
def test_send_private_details_is_skipped_without_a_requester(requester: str | None) -> None:
    slack = RecordingSlack()

    send_private_details(slack=slack, record=_record(requester=requester))

    assert slack.ephemerals == []


def test_send_private_details_goes_to_the_requester_only() -> None:
    slack = RecordingSlack()

    send_private_details(
        slack=slack,
        record=_record(
            inbox_thread_name="pan-login: fix the login page",
            inbox_thread_url="http://localhost:19441/#thread=abc",
        ),
    )

    assert slack.ephemerals == [
        (
            "C0TEST",
            _THREAD,
            "U0TEST",
            "Details for pan-login (only visible to you)\n"
            "Inbox thread: <http://localhost:19441/#thread=abc|pan-login: fix the login page>\n"
            "Terminal: t7",
        )
    ]
    assert slack.posts == []


def test_send_private_details_never_raises() -> None:
    slack = RecordingSlack()
    slack.ephemeral_error = SlackPostError("user_not_in_channel")

    send_private_details(slack=slack, record=_record())

    assert slack.ephemerals == []
