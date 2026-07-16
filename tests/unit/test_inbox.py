from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pan.errors import InboxError
from pan.inbox import FileInboxStore
from pan.models import InboxItem

_BASE_TIME = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)


def _item(event_id: str, received_at: datetime, channel: str = "C1") -> InboxItem:
    return InboxItem(
        id=event_id,
        slack_user="U1",
        channel=channel,
        thread_ts="1718000000.000100",
        is_thread_reply=False,
        raw_text="do the thing",
        received_at=received_at,
    )


def test_append_then_drain_returns_in_append_order(tmp_path: Path) -> None:
    store = FileInboxStore(tmp_path / "inbox")
    store.append(_item("Ev1", _BASE_TIME))
    store.append(_item("Ev2", _BASE_TIME + timedelta(seconds=1)))

    drained = store.drain()

    assert [item.id for item in drained] == ["Ev1", "Ev2"]


def test_drain_empties_the_store(tmp_path: Path) -> None:
    store = FileInboxStore(tmp_path / "inbox")
    store.append(_item("Ev1", _BASE_TIME))

    assert len(store.drain()) == 1
    assert store.drain() == []


def test_duplicate_event_id_is_drained_once(tmp_path: Path) -> None:
    store = FileInboxStore(tmp_path / "inbox")
    store.append(_item("Ev1", _BASE_TIME))
    store.append(_item("Ev1", _BASE_TIME + timedelta(seconds=5)))

    drained = store.drain()

    assert [item.id for item in drained] == ["Ev1"]


def test_drain_orders_by_received_at_not_filename(tmp_path: Path) -> None:
    store = FileInboxStore(tmp_path / "inbox")
    # "Evzzz" sorts after "Evaaa" lexically but was received first.
    store.append(_item("Evzzz", _BASE_TIME))
    store.append(_item("Evaaa", _BASE_TIME + timedelta(seconds=1)))

    drained = store.drain()

    assert [item.id for item in drained] == ["Evzzz", "Evaaa"]


def test_malformed_entry_raises_inbox_error(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    (inbox_dir / "EvBad.json").write_text("{ not valid json")
    store = FileInboxStore(inbox_dir)

    with pytest.raises(InboxError):
        store.drain()


def test_drain_on_empty_or_absent_dir_returns_empty(tmp_path: Path) -> None:
    store = FileInboxStore(tmp_path / "inbox")

    assert store.drain() == []


@pytest.mark.parametrize("unsafe_id", ["../evil", "/etc/passwd", "a/b", "..", ""])
def test_append_rejects_unsafe_event_id(tmp_path: Path, unsafe_id: str) -> None:
    inbox_dir = tmp_path / "inbox"
    store = FileInboxStore(inbox_dir)

    with pytest.raises(InboxError):
        store.append(_item(unsafe_id, _BASE_TIME))

    # Nothing escaped the inbox dir (and nothing landed inside it either).
    assert not (tmp_path / "etc").exists()
    if inbox_dir.exists():
        assert list(inbox_dir.iterdir()) == []


def test_malformed_entry_does_not_lose_valid_siblings(tmp_path: Path) -> None:
    inbox_dir = tmp_path / "inbox"
    store = FileInboxStore(inbox_dir)
    store.append(_item("EvGood", _BASE_TIME))
    (inbox_dir / "EvBad.json").write_text("{ not valid json")

    with pytest.raises(InboxError):
        store.drain()

    # The corrupt entry is quarantined; the valid sibling survives for a retry.
    drained = store.drain()
    assert [item.id for item in drained] == ["EvGood"]
