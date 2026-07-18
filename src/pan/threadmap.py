from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from pan.errors import ThreadNotFoundError
from pan.logging import initialise_logger
from pan.models import ThreadRecord, WorkerStatus
from pan.seams import Clock

logger = initialise_logger(__name__)


class FileThreadMap:
    def __init__(self, threads_path: Path, clock: Clock) -> None:
        self._threads_path = threads_path
        self._clock = clock

    def _read_all(self) -> dict[str, ThreadRecord]:
        # Resilient read: a single legacy/malformed record (e.g. one written before
        # `channel` became required) must not fail the whole read and block thread
        # routing — validate each record individually and skip the ones that fail.
        # Skipped records are dropped on the next _write_all; that is acceptable
        # because they are invalid/legacy and can no longer be routed to anyway.
        if not self._threads_path.exists():
            return {}
        raw_records = json.loads(self._threads_path.read_text())
        records: dict[str, ThreadRecord] = {}
        for thread_ts, record in raw_records.items():
            try:
                records[thread_ts] = ThreadRecord.model_validate(record)
            except ValidationError:
                logger.warning(f"threadmap skipping unparseable record thread={thread_ts}")
        return records

    def _write_all(self, records: dict[str, ThreadRecord]) -> None:
        payload = {
            thread_ts: record.model_dump(mode="json") for thread_ts, record in records.items()
        }
        self._threads_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._threads_path.with_name(self._threads_path.name + ".tmp")
        temp_path.write_text(json.dumps(payload))
        temp_path.replace(self._threads_path)

    def get(self, thread_ts: str) -> ThreadRecord | None:
        return self._read_all().get(thread_ts)

    def get_by_worktree(self, worktree_path: Path) -> ThreadRecord | None:
        # The completion hooks resolve their thread from the worker's cwd; the thread
        # map stays the single source of truth (INV-7). First exact match wins.
        for record in self._read_all().values():
            if record.worktree_path == worktree_path:
                return record
        return None

    def put(self, record: ThreadRecord) -> None:
        records = self._read_all()
        records[record.thread_ts] = record
        self._write_all(records)
        logger.info(f"threadmap put thread={record.thread_ts} status={record.status}")

    def update_status(self, thread_ts: str, status: WorkerStatus) -> None:
        records = self._read_all()
        record = records.get(thread_ts)
        if record is None:
            raise ThreadNotFoundError(f"no thread record for thread_ts={thread_ts}")

        record.status = status
        record.updated_at = self._clock.now()
        records[thread_ts] = record
        self._write_all(records)
        logger.info(f"threadmap update thread={thread_ts} status={status}")
