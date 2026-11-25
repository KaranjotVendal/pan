from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pan.backend.confirmations import FilePendingConfirmations, confirmations_path
from pan.backend.drain import DrainedEntry, drain_inbox
from pan.core.models import (
    ConfirmationCandidate,
    Conversation,
    InboxItem,
    PendingConfirmation,
    ThreadRecord,
    ThreadSource,
)
from pan.core.seams import DrainedLog
from pan.data.drained import FileDrainedLog, drained_log_path
from pan.data.inbox import FileInboxStore
from pan.data.threadmap import FileThreadMap
from pan.utils.clock import SystemClock

_THREAD = "1718000000.000200"
_CONVERSATION = Conversation.slack(_THREAD, "C1")


class _Stores:
    def __init__(self, tmp_path: Path) -> None:
        threads_path = tmp_path / "threads.json"
        self.inbox = FileInboxStore(tmp_path / "inbox")
        self.thread_map = FileThreadMap(threads_path, SystemClock())
        self.confirmations = FilePendingConfirmations(confirmations_path(threads_path))
        self.drained_log: DrainedLog = FileDrainedLog(drained_log_path(threads_path))

    def drain(self) -> list[DrainedEntry]:
        return drain_inbox(
            inbox_store=self.inbox,
            thread_map=self.thread_map,
            confirmations=self.confirmations,
            drained_log=self.drained_log,
            clock=SystemClock(),
        )

    def store_question(self, asked_minutes_ago: int, *, requester: str | None = "U1") -> None:
        self.confirmations.put(
            PendingConfirmation(
                conversation=_CONVERSATION,
                selector="pan docs",
                task="add the tests",
                requester=requester,
                candidates=[
                    ConfirmationCandidate(
                        address="t3", cwd=Path("/tmp/pan"), label="t3 (folder pan)"
                    )
                ],
                created_at=datetime.now(tz=UTC) - timedelta(minutes=asked_minutes_ago),
            )
        )

    def stored_question(self) -> PendingConfirmation | None:
        return self.confirmations.get(_CONVERSATION)


@pytest.fixture
def stores(tmp_path: Path) -> _Stores:
    return _Stores(tmp_path)


def _reply(event_id: str, author: str, raw_text: str = "yes") -> InboxItem:
    return InboxItem(
        id=event_id,
        author=author,
        conversation=_CONVERSATION,
        is_thread_reply=True,
        raw_text=raw_text,
        received_at=datetime.now(tz=UTC),
    )


@pytest.mark.parametrize(
    "author, requester, raw_text, expected_answer",
    [
        ("U1", "U1", "yes", "yes"),
        ("U1", "U1", "<@U000BOT> the second one please", "the second one please"),
        ("U1", "U1", "no wait, start something new", "no wait, start something new"),
        ("U000OTHER", "U1", "yes", None),
        ("U000OTHER", None, "<@U000BOT> 1", None),
    ],
    ids=[
        "requester-yes",
        "requester-by-meaning",
        "requester-other-text",
        "another-user",
        "stored-without-requester",
    ],
)
def test_drain_marks_the_question_answered_only_by_the_requester(
    stores: _Stores,
    author: str,
    requester: str | None,
    raw_text: str,
    expected_answer: str | None,
) -> None:
    stores.store_question(asked_minutes_ago=1, requester=requester)
    expected_answered = expected_answer is not None
    stores.inbox.append(_reply("Ev1", author, raw_text))

    (entry,) = stores.drain()

    question = entry.open_question
    assert question is not None
    assert question.selector == "pan docs"
    assert question.task == "add the tests"
    assert [
        (candidate.number, candidate.label, candidate.address) for candidate in question.candidates
    ] == [(1, "t3 (folder pan)", "t3")]
    assert question.answered_by_requester is expected_answered
    assert question.answer_text == expected_answer
    stored = stores.stored_question()
    assert stored is not None
    assert (stored.answered_at is not None) is expected_answered
    assert stored.answer_text == expected_answer


def test_drain_emits_when_the_question_was_asked_in_isoformat(stores: _Stores) -> None:
    stores.store_question(asked_minutes_ago=1)
    stores.inbox.append(_reply("Ev1", "U000OTHER"))
    stored = stores.stored_question()
    assert stored is not None

    (entry,) = stores.drain()

    emitted = entry.model_dump(mode="json")["open_question"]["asked_at"]
    assert emitted == stored.last_asked_at().isoformat()


def test_drain_shows_no_question_once_it_expired(stores: _Stores) -> None:
    stores.store_question(asked_minutes_ago=31)
    stores.inbox.append(_reply("Ev1", "U1"))

    (entry,) = stores.drain()

    assert entry.open_question is None


def test_drain_keeps_the_answer_for_later_items_in_the_batch(stores: _Stores) -> None:
    stores.store_question(asked_minutes_ago=1)
    stores.inbox.append(_reply("Ev1", "U1"))
    stores.inbox.append(_reply("Ev2", "U000OTHER"))

    entries = stores.drain()

    answers = [
        (entry.open_question.answered_by_requester, entry.open_question.answer_text)
        for entry in entries
        if entry.open_question is not None
    ]
    assert answers == [(True, "yes"), (True, "yes")]


def test_drain_keeps_the_latest_reply_from_the_requester(stores: _Stores) -> None:
    stores.store_question(asked_minutes_ago=1)
    stores.inbox.append(_reply("Ev1", "U1", "yes"))
    stores.inbox.append(_reply("Ev2", "U1", "no wait, not that one"))

    stores.drain()

    stored = stores.stored_question()
    assert stored is not None
    assert stored.answer_text == "no wait, not that one"


def test_drain_records_the_items_for_the_gates(stores: _Stores) -> None:
    stores.inbox.append(_reply("Ev1", "U1", "<@U000BOT> done --discard"))

    stores.drain()

    recorded = stores.drained_log.get("Ev1")
    assert recorded is not None
    assert recorded.raw_text == "<@U000BOT> done --discard"


@pytest.mark.parametrize(
    "contents",
    [b"{not json", b"[1, 2]", b"\xff\xfe"],
    ids=["not-json", "not-an-object", "not-text"],
)
def test_drain_with_a_damaged_confirmations_file_still_emits_every_item(
    tmp_path: Path, stores: _Stores, contents: bytes
) -> None:
    confirmations_path(tmp_path / "threads.json").write_bytes(contents)
    stores.inbox.append(_reply("Ev1", "U1", "yes"))
    stores.inbox.append(_reply("Ev2", "U1", "do it"))

    entries = stores.drain()

    assert sorted(entry.item.id for entry in entries) == ["Ev1", "Ev2"]
    assert all(entry.open_question is None for entry in entries)


class _UnwritableConfirmations(FilePendingConfirmations):
    def mark_answered(self, *args: object, **kwargs: object) -> None:
        raise OSError("read-only file system")


class _UnwritableDrainedLog:
    def get(self, item_id: str) -> InboxItem | None:
        return None

    def newest_in(self, conversation: Conversation, author: str) -> InboxItem | None:
        return None

    def record(self, items: list[InboxItem], now: datetime) -> None:
        raise OSError("read-only file system")


def test_drain_keeps_the_items_when_the_answer_cannot_be_saved(
    tmp_path: Path, stores: _Stores
) -> None:
    stores.store_question(asked_minutes_ago=1)
    stores.confirmations = _UnwritableConfirmations(confirmations_path(tmp_path / "threads.json"))
    stores.inbox.append(_reply("Ev1", "U1"))

    (entry,) = stores.drain()

    # Not saved, so not reported answered: pan confirm --choice would refuse it anyway.
    assert entry.open_question is not None
    assert entry.open_question.answered_by_requester is False


def test_drain_keeps_the_items_when_they_cannot_be_recorded(stores: _Stores) -> None:
    stores.drained_log = _UnwritableDrainedLog()
    stores.inbox.append(_reply("Ev1", "U1"))

    (entry,) = stores.drain()

    assert entry.item.id == "Ev1"


def test_drain_gives_an_x_post_no_tie_or_question_of_a_slack_thread_with_the_same_id(
    stores: _Stores,
) -> None:
    now = datetime.now(tz=UTC)
    stores.thread_map.tie(
        ThreadRecord(
            conversation=_CONVERSATION,
            terminal_name="pan-7",
            worktree_path=Path("/tmp/pan-7"),
            created_at=now,
            updated_at=now,
        )
    )
    stores.store_question(asked_minutes_ago=1)
    x_post = Conversation(source=ThreadSource.X, id=_THREAD, place="C1")
    stores.inbox.append(_reply("Ev1", "U1").model_copy(update={"conversation": x_post}))

    (entry,) = stores.drain()

    assert (entry.item.source, entry.item.thread_ts) == (ThreadSource.X, _THREAD)
    assert entry.tie is None
    assert entry.open_question is None
    stored = stores.stored_question()
    assert stored is not None
    assert stored.answered_at is None
