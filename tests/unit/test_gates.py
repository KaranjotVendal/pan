from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pan.backend.gates import (
    UNUSABLE_ITEM_REPLY,
    X_TO_ROUTE_NOTICE,
    newest_drained_item,
    refuse_x_source,
    require_discard_request,
    require_named_target,
    require_requesters_message,
)
from pan.core.errors import (
    CredentialsError,
    DiscardNotRequestedError,
    NoExactMatchError,
    PanError,
    SlackPostError,
    XSourceRefusedError,
)
from pan.core.models import InboxItem, ThreadRecord, ThreadSource
from pan.data.drained import FileDrainedLog
from pan.data.threadmap import FileThreadMap
from pan.utils.clock import SystemClock

_THREAD = "1700000000.000100"
_CHANNEL = "C000TEST"
_REQUESTER = "U000TEST"
_RECEIVED_AT = datetime(2026, 7, 16, 10, 0, tzinfo=UTC)


def _item(
    raw_text: str = "<@U000BOT> to pan-7: add the tests",
    *,
    event_id: str = "Ev1",
    slack_user: str = _REQUESTER,
    thread_ts: str = _THREAD,
    channel: str = _CHANNEL,
    source: ThreadSource = ThreadSource.SLACK,
    minutes_later: int = 0,
) -> InboxItem:
    return InboxItem(
        id=event_id,
        slack_user=slack_user,
        channel=channel,
        thread_ts=thread_ts,
        is_thread_reply=True,
        raw_text=raw_text,
        received_at=_RECEIVED_AT + timedelta(minutes=minutes_later),
        source=source,
    )


def _record(**updates: object) -> ThreadRecord:
    return ThreadRecord(
        thread_ts=_THREAD,
        terminal_name="pan-task",
        terminal_address="t12",
        channel=_CHANNEL,
        worktree_path=Path("/tmp/wt"),
        requester=_REQUESTER,
        created_at=_RECEIVED_AT,
        updated_at=_RECEIVED_AT,
    ).model_copy(update=updates)


class _RecordingReply:
    def __init__(self, error: PanError | None = None) -> None:
        self.lines: list[str] = []
        self._error = error

    def __call__(self, text: str) -> None:
        if self._error is not None:
            raise self._error
        self.lines.append(text)


@pytest.mark.parametrize(
    "drained, item_id, expected_id",
    [
        ([_item()], "Ev1", "Ev1"),
        ([_item()], None, None),
        ([_item()], "Ev9", None),
        ([_item(thread_ts="1700000000.000999")], "Ev1", None),
        ([_item(channel="C000OTHER")], "Ev1", None),
        ([_item(), _item("never mind", event_id="Ev2", minutes_later=1)], "Ev1", None),
        (
            [_item(), _item("+1", event_id="Ev2", slack_user="U000OTHER", minutes_later=1)],
            "Ev1",
            "Ev1",
        ),
    ],
    ids=[
        "newest-from-its-sender",
        "no-item",
        "unknown-item",
        "other-thread",
        "other-channel",
        "sender-wrote-later",
        "someone-else-wrote-later",
    ],
)
def test_newest_drained_item_is_only_the_senders_newest_in_the_thread(
    tmp_path: Path, drained: list[InboxItem], item_id: str | None, expected_id: str | None
) -> None:
    drained_log = FileDrainedLog(tmp_path / "drained.jsonl")
    drained_log.record(drained, _RECEIVED_AT)

    item = newest_drained_item(drained_log, item_id, thread_ts=_THREAD, channel=_CHANNEL)

    assert (item.id if item is not None else None) == expected_id


@pytest.mark.parametrize(
    "item_source, record_source, refused",
    [
        (ThreadSource.X, None, True),
        (None, ThreadSource.X, True),
        (ThreadSource.SLACK, ThreadSource.X, True),
        (ThreadSource.SLACK, ThreadSource.SLACK, False),
        (None, None, False),
    ],
    ids=["item-from-x", "thread-from-x", "slack-item-in-x-thread", "slack", "nothing-known"],
)
def test_refuse_x_source_refuses_an_item_or_a_thread_from_x(
    tmp_path: Path,
    item_source: ThreadSource | None,
    record_source: ThreadSource | None,
    refused: bool,
) -> None:
    thread_map = FileThreadMap(tmp_path / "threads.json", SystemClock())
    if record_source is not None:
        thread_map.tie(_record(source=record_source))
    item = _item(source=item_source) if item_source is not None else None

    if refused:
        with pytest.raises(XSourceRefusedError, match="only start new sessions") as raised:
            refuse_x_source(thread_map, _THREAD, item)
        assert str(raised.value) == X_TO_ROUTE_NOTICE
    else:
        refuse_x_source(thread_map, _THREAD, item)


@pytest.mark.parametrize(
    "item, requester",
    [
        (_item(), _REQUESTER),
        (_item(), None),
        (_item(slack_user="U000OTHER"), None),
    ],
    ids=["from-the-requester", "no-requester-given", "any-sender-without-requester"],
)
def test_require_named_target_returns_a_usable_item_that_names_the_target(
    item: InboxItem, requester: str | None
) -> None:
    reply = _RecordingReply()

    usable = require_named_target(
        item,
        action="send",
        item_id="Ev1",
        target="pan-7",
        requester=requester,
        thread_ts=_THREAD,
        reply=reply,
    )

    assert usable is item
    assert reply.lines == []


@pytest.mark.parametrize(
    "item, requester",
    [(None, None), (None, _REQUESTER), (_item(slack_user="U000OTHER"), _REQUESTER)],
    ids=["no-item", "no-item-with-requester", "other-sender"],
)
def test_require_named_target_refuses_an_unusable_item_in_the_thread(
    item: InboxItem | None, requester: str | None
) -> None:
    reply = _RecordingReply()

    with pytest.raises(NoExactMatchError, match="item Ev1 cannot be used"):
        require_named_target(
            item,
            action="send",
            item_id="Ev1",
            target="pan-7",
            requester=requester,
            thread_ts=_THREAD,
            reply=reply,
        )

    assert reply.lines == [UNUSABLE_ITEM_REPLY]


def test_require_named_target_refuses_a_target_not_named_without_a_reply() -> None:
    reply = _RecordingReply()

    with pytest.raises(NoExactMatchError, match="'pan-7' is not named"):
        require_named_target(
            _item("<@U000BOT> to pan-77: add the tests"),
            action="send",
            item_id="Ev1",
            target="pan-7",
            requester=_REQUESTER,
            thread_ts=_THREAD,
            reply=reply,
        )

    assert reply.lines == []


@pytest.mark.parametrize(
    "post_error",
    [SlackPostError("not delivered"), CredentialsError("no credentials")],
    ids=["post-failed", "no-credentials"],
)
def test_a_refusal_stands_when_its_reply_cannot_be_posted(post_error: PanError) -> None:
    with pytest.raises(NoExactMatchError):
        require_named_target(
            None,
            action="relay",
            item_id="Ev1",
            target="pan-7",
            requester=None,
            thread_ts=_THREAD,
            reply=_RecordingReply(post_error),
        )


@pytest.mark.parametrize(
    "item, refused",
    [(_item(), False), (None, True), (_item(slack_user="U000OTHER"), True)],
    ids=["requesters-message", "no-item", "other-sender"],
)
def test_require_requesters_message(item: InboxItem | None, refused: bool) -> None:
    reply = _RecordingReply()

    if refused:
        with pytest.raises(NoExactMatchError, match=f"no message from {_REQUESTER}"):
            require_requesters_message(item, requester=_REQUESTER, thread_ts=_THREAD, reply=reply)
        assert reply.lines == [UNUSABLE_ITEM_REPLY]
    else:
        assert (
            require_requesters_message(item, requester=_REQUESTER, thread_ts=_THREAD, reply=reply)
            is item
        )
        assert reply.lines == []


_DISCARD = "<@U000BOT> done --discard"


@pytest.mark.parametrize(
    "item, record, expected_replies",
    [
        (None, _record(), [UNUSABLE_ITEM_REPLY]),
        (_item(_DISCARD), None, [UNUSABLE_ITEM_REPLY]),
        (_item(_DISCARD), _record(requester=None), [UNUSABLE_ITEM_REPLY]),
        (_item(_DISCARD, slack_user="U000OTHER"), _record(), [UNUSABLE_ITEM_REPLY]),
        (_item("<@U000BOT> done"), _record(), []),
        (_item("<@U000BOT> just close it, throw the changes away"), _record(), []),
    ],
    ids=[
        "no-item",
        "no-thread-record",
        "thread-without-requester",
        "not-the-requester",
        "plain-done",
        "by-meaning",
    ],
)
def test_require_discard_request_refuses_what_the_requester_did_not_send(
    item: InboxItem | None, record: ThreadRecord | None, expected_replies: list[str]
) -> None:
    reply = _RecordingReply()

    with pytest.raises(DiscardNotRequestedError):
        require_discard_request(item, record, thread_ts=_THREAD, reply=reply)

    assert reply.lines == expected_replies


@pytest.mark.parametrize(
    "raw_text", [_DISCARD, "<@U000BOT> Done  —discard"], ids=["typed", "phone-dash"]
)
def test_require_discard_request_allows_the_requesters_done_discard(raw_text: str) -> None:
    reply = _RecordingReply()

    require_discard_request(_item(raw_text), _record(), thread_ts=_THREAD, reply=reply)

    assert reply.lines == []
