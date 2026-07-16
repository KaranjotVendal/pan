from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pan.errors import ThreadNotFoundError
from pan.models import Agent, ThreadRecord, WorkerStatus
from pan.threadmap import FileThreadMap


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def set(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _record(thread_ts: str = "1718000000.000100") -> ThreadRecord:
    created = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    return ThreadRecord(
        thread_ts=thread_ts,
        workspace_name="pan-task-3",
        workspace_id="ws_abc",
        pane_ids=["%1", "%2"],
        worktree_path=Path("/Users/me/dev/pan-worktrees/task-3"),
        agent=Agent.CLAUDE,
        morcli_session="mor_123",
        status=WorkerStatus.SPAWNING,
        created_at=created,
        updated_at=created,
    )


def test_put_then_get_round_trips_through_disk(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    record = _record()

    thread_map.put(record)
    loaded = thread_map.get(record.thread_ts)

    assert loaded is not None
    assert loaded.workspace_id == "ws_abc"
    assert loaded.pane_ids == ["%1", "%2"]
    assert loaded.worktree_path == Path("/Users/me/dev/pan-worktrees/task-3")
    assert loaded.agent is Agent.CLAUDE
    assert loaded.morcli_session == "mor_123"
    assert loaded.status is WorkerStatus.SPAWNING
    assert loaded.created_at == record.created_at


def test_update_status_transitions_and_bumps_updated_at(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    record = _record()
    thread_map.put(record)

    later = datetime(2026, 7, 16, 11, 30, 0, tzinfo=UTC)
    clock.set(later)
    thread_map.update_status(record.thread_ts, WorkerStatus.RUNNING)

    updated = thread_map.get(record.thread_ts)
    assert updated is not None
    assert updated.status is WorkerStatus.RUNNING
    assert updated.updated_at == later
    # Only status and updated_at change — every other field survives the
    # read-modify-write cycle unchanged.
    assert updated.created_at == record.created_at
    assert updated.pane_ids == record.pane_ids
    assert updated.morcli_session == record.morcli_session
    assert updated.worktree_path == record.worktree_path
    assert updated.workspace_id == record.workspace_id
    assert updated.agent is record.agent


def test_get_unknown_thread_returns_none(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )

    assert thread_map.get("nope") is None


def test_update_status_unknown_thread_raises(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )

    with pytest.raises(ThreadNotFoundError):
        thread_map.update_status("nope", WorkerStatus.DONE)


def test_put_is_idempotent_upsert(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.put(_record("t1"))
    thread_map.put(_record("t2"))

    replacement = _record("t1")
    replacement.workspace_id = "ws_replaced"
    thread_map.put(replacement)

    fetched = thread_map.get("t1")
    assert fetched is not None
    assert fetched.workspace_id == "ws_replaced"
    assert thread_map.get("t2") is not None
