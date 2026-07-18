from __future__ import annotations

import json
import logging
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


def _record(
    thread_ts: str = "1718000000.000100",
    worktree_path: Path = Path("/Users/me/dev/pan-worktrees/task-3"),
) -> ThreadRecord:
    created = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    return ThreadRecord(
        thread_ts=thread_ts,
        workspace_name="pan-task-3",
        workspace_id="ws_abc",
        channel="C0001",
        pane_ids=["%1", "%2"],
        worktree_path=worktree_path,
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


def test_get_by_worktree_returns_matching_record(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.put(_record("t1", worktree_path=Path("/wt/pan-a")))
    thread_map.put(_record("t2", worktree_path=Path("/wt/pan-b")))

    found = thread_map.get_by_worktree(Path("/wt/pan-b"))

    assert found is not None
    assert found.thread_ts == "t2"


def test_get_by_worktree_matches_across_symlinked_prefix(tmp_path: Path) -> None:
    # A worker's cwd delivered by a Claude Code hook can differ from the stored
    # worktree_path only by a symlinked prefix (on macOS /tmp -> /private/tmp). The
    # lookup must still resolve to the record. Build a real symlink so resolve()
    # actually collapses it rather than relying on a platform-specific path.
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    real_dir = tmp_path / "real"
    worktree = real_dir / "pan-a"
    worktree.mkdir(parents=True)
    linked_prefix = tmp_path / "linked"
    linked_prefix.symlink_to(real_dir)

    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    # Stored via the symlinked prefix, looked up via the resolved real path.
    thread_map.put(_record("t1", worktree_path=linked_prefix / "pan-a"))

    found = thread_map.get_by_worktree(worktree)

    assert found is not None
    assert found.thread_ts == "t1"


def test_get_by_worktree_returns_none_when_no_match(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.put(_record("t1", worktree_path=Path("/wt/pan-a")))

    assert thread_map.get_by_worktree(Path("/wt/nope")) is None


def test_read_tolerates_unparseable_legacy_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A record written before `channel` became required must not poison the whole
    # read: the valid neighbour is still routable, the bad one is skipped.
    valid_ts = "t-valid"
    legacy_ts = "t-legacy"
    valid_record = _record(valid_ts, worktree_path=Path("/wt/pan-valid"))
    legacy_payload = _record(legacy_ts).model_dump(mode="json")
    del legacy_payload["channel"]

    threads_path = tmp_path / "threads.json"
    threads_path.write_text(
        json.dumps(
            {
                valid_ts: valid_record.model_dump(mode="json"),
                legacy_ts: legacy_payload,
            }
        )
    )

    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(threads_path, clock)

    # propagate=False on this logger, so attach caplog's handler directly.
    threadmap_logger = logging.getLogger("pan.threadmap")
    threadmap_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="pan.threadmap"):
            loaded_valid = thread_map.get(valid_ts)
            loaded_legacy = thread_map.get(legacy_ts)
            found = thread_map.get_by_worktree(Path("/wt/pan-valid"))
    finally:
        threadmap_logger.removeHandler(caplog.handler)

    assert loaded_valid is not None
    assert loaded_valid.thread_ts == valid_ts
    assert loaded_legacy is None
    assert found is not None
    assert found.thread_ts == valid_ts
    assert any(legacy_ts in record.message for record in caplog.records)
