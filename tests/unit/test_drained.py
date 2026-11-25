from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pan.core.models import Conversation, InboxItem, ThreadSource
from pan.data.drained import FileDrainedLog, drained_log_path

_NOW = datetime(2026, 11, 24, 10, 0, 0, tzinfo=UTC)
_THREAD = Conversation.slack("1700000000.000100", "C000TEST")


def _item(event_id: str, hours_ago: float = 0) -> InboxItem:
    return InboxItem(
        id=event_id,
        author="U000TEST",
        conversation=_THREAD,
        is_thread_reply=False,
        raw_text=f"message {event_id}",
        received_at=_NOW - timedelta(hours=hours_ago),
    )


def _log(tmp_path: Path) -> FileDrainedLog:
    return FileDrainedLog(drained_log_path(tmp_path / "threads.json"))


def test_drained_log_sits_next_to_the_thread_map(tmp_path: Path) -> None:
    assert drained_log_path(tmp_path / "threads.json") == tmp_path / "drained.jsonl"


def test_drained_log_finds_a_recorded_item_and_drops_old_ones(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.record([_item("EvOld", hours_ago=25), _item("EvRecent", hours_ago=23)], _NOW)
    log.record([_item("EvNew")], _NOW)

    assert log.get("EvOld") is None
    recent = log.get("EvRecent")
    assert recent is not None
    assert recent.raw_text == "message EvRecent"
    assert log.get("EvNew") is not None
    assert log.get("EvMissing") is None


def test_drained_log_keeps_only_the_newest_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pan.data.drained.DRAINED_LOG_MAX_ITEMS", 2)
    log = _log(tmp_path)
    log.record([_item("Ev1"), _item("Ev2"), _item("Ev3")], _NOW)

    assert [log.get(event_id) is not None for event_id in ("Ev1", "Ev2", "Ev3")] == [
        False,
        True,
        True,
    ]


@pytest.mark.parametrize(
    "contents",
    [b"{not json\n", b"\xff\xfe", b'{"id": 5}\n'],
    ids=["not-json", "not-text", "malformed-line"],
)
def test_drained_log_reads_a_damaged_file_as_empty(tmp_path: Path, contents: bytes) -> None:
    log = _log(tmp_path)
    (tmp_path / "drained.jsonl").write_bytes(contents)

    assert log.get("Ev1") is None
    log.record([_item("Ev1")], _NOW)
    assert log.get("Ev1") is not None


@pytest.mark.parametrize(
    "conversation, author, expected_id",
    [
        (_THREAD, "U000TEST", "Ev3"),
        (_THREAD, "U000OTHER", "Ev4"),
        (Conversation.slack("1700000000.000200", "C000TEST"), "U000TEST", "Ev2"),
        (Conversation.slack("1700000000.000100", "C000OTHER"), "U000TEST", None),
        (_THREAD, "U000NOBODY", None),
        (
            Conversation(source=ThreadSource.X, id="1700000000.000100", place="C000TEST"),
            "U000TEST",
            "Ev5",
        ),
    ],
    ids=[
        "senders-newest",
        "another-sender",
        "other-thread",
        "other-channel",
        "no-such-sender",
        "x-post-with-the-same-id",
    ],
)
def test_drained_log_newest_in_a_thread_from_a_sender(
    tmp_path: Path, conversation: Conversation, author: str, expected_id: str | None
) -> None:
    log = _log(tmp_path)
    other_thread = Conversation.slack("1700000000.000200", "C000TEST")
    log.record([_item("Ev1"), _item("Ev2").model_copy(update={"conversation": other_thread})], _NOW)
    log.record([_item("Ev3"), _item("Ev4").model_copy(update={"author": "U000OTHER"})], _NOW)
    x_post = _THREAD.model_copy(update={"source": ThreadSource.X})
    log.record([_item("Ev5").model_copy(update={"conversation": x_post})], _NOW)

    newest = log.newest_in(conversation, author)

    assert (newest.id if newest is not None else None) == expected_id


def test_drained_log_records_under_a_lock_and_keeps_every_drain(tmp_path: Path) -> None:
    first_log, second_log = _log(tmp_path), _log(tmp_path)

    first_log.record([_item("Ev1")], _NOW)
    second_log.record([_item("Ev2")], _NOW)

    assert (tmp_path / "drained.jsonl.lock").exists()
    assert first_log.get("Ev1") is not None
    assert first_log.get("Ev2") is not None


def test_a_flat_drained_line_from_before_conversation_loads_and_is_rewritten(
    tmp_path: Path,
) -> None:
    flat_line = {
        "id": "EvOld",
        "slack_user": "U000TEST",
        "channel": "C000TEST",
        "thread_ts": "1700000000.000100",
        "is_thread_reply": False,
        "raw_text": "message EvOld",
        "received_at": _NOW.isoformat(),
        "source": "slack",
        "mentioned": True,
    }
    (tmp_path / "drained.jsonl").write_text(f"{json.dumps(flat_line)}\n")
    log = _log(tmp_path)

    newest = log.newest_in(_THREAD, "U000TEST")
    log.record([_item("EvNew")], _NOW)

    assert newest is not None
    assert newest.id == "EvOld"
    stored_lines = [
        json.loads(line) for line in (tmp_path / "drained.jsonl").read_text().splitlines()
    ]
    assert [line["id"] for line in stored_lines] == ["EvOld", "EvNew"]
    assert stored_lines[0]["conversation"] == _THREAD.model_dump(mode="json")
    assert stored_lines[0]["author"] == "U000TEST"
    assert not {"thread_ts", "channel", "source", "slack_user"} & set(stored_lines[0])
    assert log.get("EvOld") == newest
