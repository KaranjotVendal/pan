from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from pan.errors import ThreadNotFoundError
from pan.logging import initialise_logger
from pan.models import ReplyMode, ThreadRecord, WorkerStatus
from pan.seams import Clock

logger = initialise_logger(__name__)


def record_key(record: ThreadRecord) -> str:
    # One record per (thread, session): a thread may hold several records (only one tied),
    # so a session that loses its thread to `new:` or `to <name>:` keeps its record, untied,
    # and its hooks still find it. The source keeps a Slack thread and an X post apart.
    return f"{record.source.value}:{record.thread_ts}:{record.terminal_name}"


def _current(candidates: list[ThreadRecord]) -> ThreadRecord | None:
    # The tied record wins; otherwise the most recently updated one.
    for record in candidates:
        if record.tied:
            return record
    return max(candidates, key=lambda record: record.updated_at) if candidates else None


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
        # Keys are rebuilt from each record (record_key), so a file written when records
        # were keyed by thread alone migrates on its next write.
        if not self._threads_path.exists():
            return {}
        raw_records = json.loads(self._threads_path.read_text())
        records: dict[str, ThreadRecord] = {}
        for stored_key, record in raw_records.items():
            try:
                parsed = ThreadRecord.model_validate(record)
            except ValidationError:
                logger.warning(f"threadmap skipping unparseable record key={stored_key}")
                continue
            records[record_key(parsed)] = parsed
        return records

    def _write_all(self, records: dict[str, ThreadRecord]) -> None:
        payload = {key: record.model_dump(mode="json") for key, record in records.items()}
        self._threads_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._threads_path.with_name(self._threads_path.name + ".tmp")
        temp_path.write_text(json.dumps(payload))
        temp_path.replace(self._threads_path)

    def _select(
        self, records: dict[str, ThreadRecord], thread_ts: str, session_name: str | None
    ) -> ThreadRecord | None:
        # The thread's record of the session `session_name` when named, else the thread's current record.
        return _current(
            [
                record
                for record in records.values()
                if record.thread_ts == thread_ts
                and (session_name is None or record.terminal_name == session_name)
            ]
        )

    def _require(
        self, records: dict[str, ThreadRecord], thread_ts: str, session_name: str | None
    ) -> ThreadRecord:
        record = self._select(records, thread_ts, session_name)
        if record is None:
            raise ThreadNotFoundError(f"no thread record for thread_ts={thread_ts}")
        return record

    def get(self, thread_ts: str, *, session_name: str | None = None) -> ThreadRecord | None:
        # The thread's tied record (else its newest), or the thread's record of `session_name`.
        return self._select(self._read_all(), thread_ts, session_name)

    def records(self) -> list[ThreadRecord]:
        # All current bindings, for the reconcile view (collect_sessions) to left-join
        # against live hc terminals. Read-only; the thread map stays authoritative (INV-7).
        return list(self._read_all().values())

    def get_by_worktree(self, worktree_path: Path) -> ThreadRecord | None:
        # The completion hooks resolve their thread from the worker's cwd; the thread
        # map stays the single source of truth (INV-7). Compare RESOLVED paths so a
        # symlinked prefix between the stored path and the hook's cwd still matches
        # (on macOS /tmp -> /private/tmp). Resolving both sides here keeps the CLI hook
        # boundary free of path normalization. Only sessions pan started run pan's hooks, so
        # desk-session records (whose folder is not pan's) never match. A tied record wins;
        # otherwise the newest.
        target = worktree_path.resolve()
        return _current(
            [
                record
                for record in self._read_all().values()
                if record.started_by_pan and record.worktree_path.resolve() == target
            ]
        )

    def put(self, record: ThreadRecord) -> None:
        records = self._read_all()
        records[record_key(record)] = record
        self._write_all(records)
        logger.info(f"threadmap put thread={record.thread_ts} status={record.status}")

    def tie(self, record: ThreadRecord) -> None:
        # Store the record as its thread's tie. One thread per session and one session per
        # thread: any other thread tied to the same session (same terminal name) is untied,
        # and so is the thread's previous session. Both keep their records, so their history
        # stays readable and their hooks still find them.
        records = self._read_all()
        now = self._clock.now()
        key = record_key(record)
        for other_key, other in records.items():
            if other_key == key or not other.tied:
                continue
            if other.terminal_name == record.terminal_name or other.thread_ts == record.thread_ts:
                other.tied = False
                other.updated_at = now
                logger.info(
                    f"threadmap untie thread={other.thread_ts} session={other.terminal_name}"
                )
        records[key] = record.model_copy(update={"tied": True})
        self._write_all(records)
        logger.info(f"threadmap tie thread={record.thread_ts} status={record.status}")

    def untie(self, thread_ts: str) -> None:
        # The thread stops routing follow-ups to its session. The session itself is never
        # touched.
        records = self._read_all()
        self._require(records, thread_ts, None)
        now = self._clock.now()
        for record in records.values():
            if record.thread_ts == thread_ts and record.tied:
                record.tied = False
                record.updated_at = now
        self._write_all(records)
        logger.info(f"threadmap untie thread={thread_ts}")

    def record_inbox_thread(
        self,
        thread_ts: str,
        inbox_thread_id: str,
        url: str | None,
        *,
        session_name: str | None = None,
    ) -> None:
        # Written by the worker's inbox-link hook once its first Inbox send returns, or by a
        # lookup by name (a desk session has no hooks); a lookup may find no link.
        records = self._read_all()
        record = self._require(records, thread_ts, session_name)
        record.inbox_thread_id = inbox_thread_id
        if url is not None:
            record.inbox_thread_url = url
        record.updated_at = self._clock.now()
        self._write_all(records)
        logger.info(f"threadmap inbox thread recorded thread={thread_ts}")

    def update_status(
        self, thread_ts: str, status: WorkerStatus, *, session_name: str | None = None
    ) -> None:
        records = self._read_all()
        record = self._require(records, thread_ts, session_name)
        record.status = status
        record.updated_at = self._clock.now()
        self._write_all(records)
        logger.info(f"threadmap update thread={thread_ts} status={status}")

    def set_reply_mode(
        self, thread_ts: str, reply_mode: ReplyMode, *, session_name: str | None = None
    ) -> None:
        records = self._read_all()
        record = self._require(records, thread_ts, session_name)
        record.reply_mode = reply_mode
        record.updated_at = self._clock.now()
        self._write_all(records)
        logger.info(f"threadmap update thread={thread_ts} reply_mode={reply_mode}")

    def record_session(
        self,
        thread_ts: str,
        session_id: str,
        transcript_path: Path | None,
        *,
        session_name: str | None = None,
    ) -> None:
        # Written by the worker's SessionStart hook: the Claude session id and transcript
        # path that `pan read --full` renders. A resumed or cleared session overwrites them.
        records = self._read_all()
        record = self._require(records, thread_ts, session_name)
        record.claude_session_id = session_id
        record.transcript_path = transcript_path
        record.updated_at = self._clock.now()
        self._write_all(records)
        logger.info(f"threadmap session thread={thread_ts}")


def tie_note(record: ThreadRecord | None) -> str | None:
    # The plain-words tie line `pan inbox drain` puts on an item, built at drain time
    # because the tie can change between the gateway writing the item and the drain.
    if record is None or not record.tied:
        return None
    return (
        f"this thread's work belongs to {record.terminal_name} ({record.status.value}); "
        "send follow-ups there; never start a second session for the same work"
    )
