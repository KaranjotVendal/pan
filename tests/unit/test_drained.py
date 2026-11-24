from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pan.drained import FileDrainedLog, drained_log_path
from pan.models import InboxItem

_NOW = datetime(2026, 11, 24, 10, 0, 0, tzinfo=UTC)


def _item(event_id: str, hours_ago: float = 0) -> InboxItem:
    return InboxItem(
        id=event_id,
        slack_user="U000TEST",
        channel="C000TEST",
        thread_ts="1700000000.000100",
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
    monkeypatch.setattr("pan.drained.DRAINED_LOG_MAX_ITEMS", 2)
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
    "thread_ts, channel, slack_user, expected_id",
    [
        ("1700000000.000100", "C000TEST", "U000TEST", "Ev3"),
        ("1700000000.000100", "C000TEST", "U000OTHER", "Ev4"),
        ("1700000000.000200", "C000TEST", "U000TEST", "Ev2"),
        ("1700000000.000100", "C000OTHER", "U000TEST", None),
        ("1700000000.000100", "C000TEST", "U000NOBODY", None),
    ],
    ids=["senders-newest", "another-sender", "other-thread", "other-channel", "no-such-sender"],
)
def test_drained_log_newest_in_a_thread_from_a_sender(
    tmp_path: Path, thread_ts: str, channel: str, slack_user: str, expected_id: str | None
) -> None:
    log = _log(tmp_path)
    log.record(
        [_item("Ev1"), _item("Ev2").model_copy(update={"thread_ts": "1700000000.000200"})], _NOW
    )
    log.record([_item("Ev3"), _item("Ev4").model_copy(update={"slack_user": "U000OTHER"})], _NOW)

    newest = log.newest_in(thread_ts, channel, slack_user)

    assert (newest.id if newest is not None else None) == expected_id


def test_drained_log_records_under_a_lock_and_keeps_every_drain(tmp_path: Path) -> None:
    first_log, second_log = _log(tmp_path), _log(tmp_path)

    first_log.record([_item("Ev1")], _NOW)
    second_log.record([_item("Ev2")], _NOW)

    assert (tmp_path / "drained.jsonl.lock").exists()
    assert first_log.get("Ev1") is not None
    assert first_log.get("Ev2") is not None
