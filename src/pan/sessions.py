from __future__ import annotations

from pan.config import SESSION_LAST_ASK_MAX_LENGTH, WORKER_BRIEF_TASK_MARKER
from pan.logging import initialise_logger
from pan.models import (
    AgentStatus,
    SessionSummary,
    Terminal,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.seams import TerminalHost, ThreadMap
from pan.transcript import last_user_text

logger = initialise_logger(__name__)

# The host's live agent status normalized to the pan WorkerStatus it implies. Between turns an
# agent is idle but still a live worker (IDLE, as the Stop hook records it). NONE and
# UNKNOWN are resolved in worker_status_for, since they depend on the recorded status.
_AGENT_TO_WORKER: dict[AgentStatus, WorkerStatus] = {
    AgentStatus.IDLE: WorkerStatus.IDLE,
    AgentStatus.WORKING: WorkerStatus.RUNNING,
    AgentStatus.BLOCKED: WorkerStatus.BLOCKED,
    AgentStatus.DONE: WorkerStatus.DONE,
    AgentStatus.ERROR: WorkerStatus.FAILED,
}


def worker_status_for(terminal: Terminal | None, recorded: WorkerStatus) -> WorkerStatus:
    # The WorkerStatus a worker's live terminal implies, given what pan recorded.
    # A missing or finished terminal means the worker is gone: it stays DONE if it
    # finished, otherwise it failed (killed, crashed, or lost to a reboot). A live
    # terminal reporting no status is still booting while the record says SPAWNING, and
    # otherwise has dropped back to a shell. An unrecognized status keeps the record.
    if terminal is None or terminal.state is TerminalState.FINISHED:
        return WorkerStatus.DONE if recorded is WorkerStatus.DONE else WorkerStatus.FAILED
    if terminal.agent_status is AgentStatus.NONE:
        return WorkerStatus.SPAWNING if recorded is WorkerStatus.SPAWNING else WorkerStatus.FAILED
    return _AGENT_TO_WORKER.get(terminal.agent_status, recorded)


# A live session moves between working and idle on every turn, and pan records only the
# turn ends (a relayed follow-up does not mark it RUNNING), so the two never count as drift.
_LIVE_TURN_STATUSES = frozenset({WorkerStatus.RUNNING, WorkerStatus.IDLE})


def session_drift(recorded: WorkerStatus, terminal: Terminal) -> bool:
    # True when pan's recorded status disagrees with the status the live terminal implies.
    # SPAWNING is excluded (a worker still coming up legitimately has no status yet).
    if recorded is WorkerStatus.SPAWNING:
        return False
    implied = worker_status_for(terminal, recorded)
    if recorded in _LIVE_TURN_STATUSES and implied in _LIVE_TURN_STATUSES:
        return False
    return implied is not recorded


def _preferred(candidates: list[ThreadRecord]) -> ThreadRecord | None:
    # A tied record wins (a session keeps untied records of threads it left), then the newest.
    for record in candidates:
        if record.tied:
            return record
    return max(candidates, key=lambda record: record.updated_at) if candidates else None


def _match_record(terminal: Terminal, records: list[ThreadRecord]) -> ThreadRecord | None:
    # The join to pan's records, most precise first: the terminal address recorded at spawn
    # (or at a `to <name>:` tie), then the terminal name (the stream name), then, for pan's
    # own workers only, the resolved worktree path against the terminal's launch folder
    # (symlink-safe, mirroring FileThreadMap.get_by_worktree). A desk session's record holds
    # a folder pan does not own, so another terminal in that folder never joins to it.
    by_address = [
        record
        for record in records
        if record.terminal_address and record.terminal_address == terminal.address
    ]
    if by_address:
        return _preferred(by_address)
    if terminal.name is not None:
        by_name = [record for record in records if record.terminal_name == terminal.name]
        if by_name:
            return _preferred(by_name)
    target_cwd = terminal.cwd.resolve()
    return _preferred(
        [
            record
            for record in records
            if record.started_by_pan and record.worktree_path.resolve() == target_cwd
        ]
    )


def last_ask_for(record: ThreadRecord | None) -> str | None:
    # The last thing the session was asked, on one short line, when pan knows its transcript.
    # A worker's first ask is its brief, so the Inbox preamble is cut off and only the task
    # shows. Sessions pan has no transcript for show nothing.
    if record is None or record.transcript_path is None:
        return None
    text = last_user_text(record.transcript_path)
    if text is None:
        return None
    _preamble, marker, task = text.partition(WORKER_BRIEF_TASK_MARKER)
    one_line = " ".join((task if marker else text).split())
    if len(one_line) > SESSION_LAST_ASK_MAX_LENGTH:
        one_line = one_line[: SESSION_LAST_ASK_MAX_LENGTH - 3].rstrip() + "..."
    return one_line or None


def collect_sessions(terminal_host: TerminalHost, thread_map: ThreadMap) -> list[SessionSummary]:
    # Pure reconcile over the injected seams: enumerate every live terminal, join the
    # pan thread map to identify pan-owned ones, and flag status drift. Ownership comes
    # only from the thread map, never from host labels, so a host without labels works. The thread map is
    # read, never rewritten (INV-7); drift is reported, not healed.
    records = thread_map.records()
    summaries: list[SessionSummary] = []
    for terminal in terminal_host.list_terminals():
        record = _match_record(terminal, records)
        # A desk session (a record with started_by_pan False) is not pan's: it has a thread
        # but no pan status, since no pan hooks report its turns.
        pan_record = record if record is not None and record.started_by_pan else None
        summaries.append(
            SessionSummary(
                address=terminal.address,
                name=terminal.name,
                where=terminal.where,
                cwd=terminal.cwd,
                agent=terminal.agent,
                agent_status=terminal.agent_status,
                thread_ts=record.thread_ts if record is not None else None,
                pan_status=pan_record.status if pan_record is not None else None,
                is_pan_owned=pan_record is not None,
                drift=pan_record is not None and session_drift(pan_record.status, terminal),
                last_ask=last_ask_for(record),
            )
        )
    logger.info(f"sessions collected count={len(summaries)}")
    return summaries
