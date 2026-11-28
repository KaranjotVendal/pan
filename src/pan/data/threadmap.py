from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from pan.core.errors import ThreadNotFoundError
from pan.core.models import Conversation, ReplyMode, ThreadRecord, ThreadSource, WorkerStatus
from pan.core.seams import Clock, ThreadMap
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# Every read-modify-write of the thread map holds an exclusive advisory lock (flock) on this
# file next to it, so two pan processes (the watcher's sweep and a `pan handoff`, say) never
# overwrite each other's change. Reads take no lock: each write replaces the file atomically.
THREAD_MAP_LOCK_SUFFIX = ".lock"


def thread_map_lock_path(threads_path: Path) -> Path:
    return threads_path.with_name(threads_path.name + THREAD_MAP_LOCK_SUFFIX)


def record_key(record: ThreadRecord) -> str:
    # One record per (conversation, session): a conversation may hold several records (only
    # one tied), so a session that loses its thread to `new:` or `to <name>:` keeps its
    # record, untied, and its hooks still find it. The conversation key holds the source, so a
    # Slack thread and an X post with the same id stay apart.
    return f"{record.conversation.key}:{record.terminal_name}"


def _end_wait(record: ThreadRecord) -> None:
    # A record no longer tied waits for no project's orchestrator.
    if record.awaiting_handoff:
        record.handoff = None


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
        # were keyed by thread alone migrates on its next write; a record in the flat shape
        # from before Conversation existed loads (ThreadRecord) and is rewritten in the new one.
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

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # Held around one read-modify-write; never nested (a second flock on a new open file
        # would wait on the first, even in the same process).
        lock_path = thread_map_lock_path(self._threads_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def _write_all(self, records: dict[str, ThreadRecord]) -> None:
        payload = {key: record.model_dump(mode="json") for key, record in records.items()}
        self._threads_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._threads_path.with_name(self._threads_path.name + ".tmp")
        temp_path.write_text(json.dumps(payload))
        temp_path.replace(self._threads_path)

    def _select(
        self,
        records: dict[str, ThreadRecord],
        conversation: Conversation,
        session_name: str | None,
    ) -> ThreadRecord | None:
        # The conversation's record of the session `session_name` when named, else its current
        # record. Matched on the key (source and id); the place takes no part.
        return _current(
            [
                record
                for record in records.values()
                if record.conversation.key == conversation.key
                and (session_name is None or record.terminal_name == session_name)
            ]
        )

    def _require(
        self,
        records: dict[str, ThreadRecord],
        conversation: Conversation,
        session_name: str | None,
    ) -> ThreadRecord:
        record = self._select(records, conversation, session_name)
        if record is None:
            raise ThreadNotFoundError(f"no thread record for conversation={conversation.key}")
        return record

    def get(
        self, conversation: Conversation, *, session_name: str | None = None
    ) -> ThreadRecord | None:
        # The conversation's tied record (else its newest), or its record of `session_name`.
        return self._select(self._read_all(), conversation, session_name)

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
        with self._locked():
            records = self._read_all()
            records[record_key(record)] = record
            self._write_all(records)
            logger.info(
                f"threadmap put conversation={record.conversation.key} status={record.status}"
            )

    def tie(self, record: ThreadRecord) -> None:
        # Store the record as its thread's tie. One thread per session and one session per
        # thread: any other thread tied to the same session (same terminal name) is untied,
        # and so is the thread's previous session. Both keep their records, so their history
        # stays readable and their hooks still find them. A thread waiting for a project's
        # orchestrator is tied to the project, not to that session: several threads may wait
        # on one orchestrator, and a session tie elsewhere leaves them waiting.
        with self._locked():
            records = self._read_all()
            now = self._clock.now()
            key = record_key(record)
            for other_key, other in records.items():
                if other_key == key or not other.tied:
                    continue
                same_session = (
                    other.terminal_name == record.terminal_name
                    and not other.awaiting_handoff
                    and not record.awaiting_handoff
                )
                if same_session or other.conversation.key == record.conversation.key:
                    # Untied, it waits for nothing: a handoff still pending ends here.
                    _end_wait(other)
                    other.tied = False
                    other.updated_at = now
                    logger.info(
                        f"threadmap untie conversation={other.conversation.key} "
                        f"session={other.terminal_name}"
                    )
            records[key] = record.model_copy(update={"tied": True})
            self._write_all(records)
            logger.info(
                f"threadmap tie conversation={record.conversation.key} status={record.status}"
            )

    def untie(self, conversation: Conversation) -> None:
        # The conversation stops routing follow-ups to its session. The session itself is
        # never touched.
        with self._locked():
            records = self._read_all()
            self._require(records, conversation, None)
            now = self._clock.now()
            for record in records.values():
                if record.conversation.key == conversation.key and record.tied:
                    _end_wait(record)
                    record.tied = False
                    record.updated_at = now
            self._write_all(records)
            logger.info(f"threadmap untie conversation={conversation.key}")

    def record_inbox_thread(
        self,
        conversation: Conversation,
        inbox_thread_id: str,
        url: str | None,
        *,
        session_name: str | None = None,
    ) -> None:
        # Written by the worker's inbox-link hook once its first Inbox send returns, or by a
        # lookup by name (a desk session has no hooks); a lookup may find no link.
        with self._locked():
            records = self._read_all()
            record = self._require(records, conversation, session_name)
            record.inbox_thread_id = inbox_thread_id
            if url is not None:
                record.inbox_thread_url = url
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap inbox thread recorded conversation={conversation.key}")

    def update_status(
        self, conversation: Conversation, status: WorkerStatus, *, session_name: str | None = None
    ) -> None:
        with self._locked():
            records = self._read_all()
            record = self._require(records, conversation, session_name)
            record.status = status
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap update conversation={conversation.key} status={status}")

    def set_reply_mode(
        self, conversation: Conversation, reply_mode: ReplyMode, *, session_name: str | None = None
    ) -> None:
        with self._locked():
            records = self._read_all()
            record = self._require(records, conversation, session_name)
            record.reply_mode = reply_mode
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap update conversation={conversation.key} reply_mode={reply_mode}")

    def record_status_message(
        self, conversation: Conversation, message_ts: str, *, session_name: str | None = None
    ) -> None:
        with self._locked():
            records = self._read_all()
            record = self._require(records, conversation, session_name)
            record.status_message_ts = message_ts
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap status message recorded conversation={conversation.key}")

    def record_posted_answer(
        self, conversation: Conversation, answer_digest: str, *, session_name: str | None = None
    ) -> None:
        with self._locked():
            records = self._read_all()
            record = self._require(records, conversation, session_name)
            record.last_answer_digest = answer_digest
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap posted answer recorded conversation={conversation.key}")

    def record_task(
        self,
        conversation: Conversation,
        label: str,
        started_at: datetime,
        *,
        session_name: str | None = None,
    ) -> None:
        with self._locked():
            records = self._read_all()
            record = self._require(records, conversation, session_name)
            record.status_label = label
            record.task_started_at = started_at
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap task recorded conversation={conversation.key}")

    def mark_handoff_nudged(
        self, conversation: Conversation, *, session_name: str, nudged: bool = True
    ) -> bool:
        # Under the lock, so a thread handed on (or untied) since the caller looked is left
        # alone: only a tied record still waiting for its project's orchestrator is changed,
        # and only when its mark differs (`nudged` False clears it after a failed post). True
        # when it was changed.
        with self._locked():
            records = self._read_all()
            record = self._select(records, conversation, session_name)
            if (
                record is None
                or not record.tied
                or record.handoff is None
                or not record.handoff.awaiting
                or record.handoff.nudged is nudged
            ):
                return False
            record.handoff = record.handoff.model_copy(update={"nudged": nudged})
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap handoff nudged={nudged} conversation={conversation.key}")
            return True

    def end_handoff_wait(self, conversation: Conversation, *, session_name: str) -> bool:
        # The orchestrator the thread waits for has ended: untie the waiting record and drop
        # its handoff, only while it is still tied and waiting (so one caller wins). True when
        # it was.
        with self._locked():
            records = self._read_all()
            record = self._select(records, conversation, session_name)
            if record is None or not record.tied or not record.awaiting_handoff:
                return False
            _end_wait(record)
            record.tied = False
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap handoff wait ended conversation={conversation.key}")
            return True

    def record_session(
        self,
        conversation: Conversation,
        session_id: str,
        transcript_path: Path | None,
        *,
        session_name: str | None = None,
    ) -> None:
        # Written by the worker's SessionStart hook: the Claude session id and transcript
        # path that `pan read --full` renders. A resumed or cleared session overwrites them.
        with self._locked():
            records = self._read_all()
            record = self._require(records, conversation, session_name)
            record.claude_session_id = session_id
            record.transcript_path = transcript_path
            record.updated_at = self._clock.now()
            self._write_all(records)
            logger.info(f"threadmap session conversation={conversation.key}")


def conversation_for_id(thread_map: ThreadMap, conversation_id: str) -> Conversation:
    # The conversation a command that takes only --thread means: the Slack thread with that id
    # when pan has a record of it, else the X post with that id when pan has one, else the
    # Slack thread (which then finds nothing).
    slack_thread = Conversation.slack(conversation_id)
    if thread_map.get(slack_thread) is not None:
        return slack_thread
    x_post = Conversation(source=ThreadSource.X, id=conversation_id)
    return x_post if thread_map.get(x_post) is not None else slack_thread


def tie_note(record: ThreadRecord | None) -> str | None:
    # The plain-words tie line `pan inbox drain` puts on an item, built at drain time
    # because the tie can change between the gateway writing the item and the drain.
    if record is None or not record.tied:
        return None
    if record.handoff is not None and record.handoff.awaiting:
        return (
            f"this thread waits for the {record.handoff.project} project's orchestrator to pick "
            "a stream; send follow-ups there with pan send (no --to); never start a second "
            "session for the same work"
        )
    return (
        f"this thread's work belongs to {record.terminal_name} ({record.status.value}); "
        "send follow-ups there; never start a second session for the same work"
    )
