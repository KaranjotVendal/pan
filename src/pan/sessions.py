from __future__ import annotations

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

logger = initialise_logger(__name__)

# The host's live agent status normalized to the pan WorkerStatus it implies. Between turns an
# agent is idle but still a live worker, so idle and working both mean RUNNING. NONE and
# UNKNOWN are resolved in worker_status_for, since they depend on the recorded status.
_AGENT_TO_WORKER: dict[AgentStatus, WorkerStatus] = {
    AgentStatus.IDLE: WorkerStatus.RUNNING,
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


def session_drift(recorded: WorkerStatus, terminal: Terminal) -> bool:
    # True when pan's recorded status disagrees with the status the live terminal implies.
    # SPAWNING is excluded (a worker still coming up legitimately has no status yet).
    if recorded is WorkerStatus.SPAWNING:
        return False
    return worker_status_for(terminal, recorded) is not recorded


def _match_pan_record(terminal: Terminal, records: list[ThreadRecord]) -> ThreadRecord | None:
    # Pan-ownership join, most precise first: the terminal address recorded at spawn, then
    # the terminal name (the stream name), then the resolved worktree path against the
    # terminal's launch folder (symlink-safe, mirroring FileThreadMap.get_by_worktree).
    for record in records:
        if record.terminal_address and record.terminal_address == terminal.address:
            return record
    if terminal.name is not None:
        for record in records:
            if record.terminal_name == terminal.name:
                return record
    target_cwd = terminal.cwd.resolve()
    for record in records:
        if record.worktree_path.resolve() == target_cwd:
            return record
    return None


def collect_sessions(terminal_host: TerminalHost, thread_map: ThreadMap) -> list[SessionSummary]:
    # Pure reconcile over the injected seams: enumerate every live terminal, join the
    # pan thread map to identify pan-owned ones, and flag status drift. Ownership comes
    # only from the thread map, never from host labels, so a host without labels works. The thread map is
    # read, never rewritten (INV-7); drift is reported, not healed.
    records = thread_map.records()
    summaries: list[SessionSummary] = []
    for terminal in terminal_host.list_terminals():
        record = _match_pan_record(terminal, records)
        summaries.append(
            SessionSummary(
                address=terminal.address,
                name=terminal.name,
                where=terminal.where,
                cwd=terminal.cwd,
                agent=terminal.agent,
                agent_status=terminal.agent_status,
                thread_ts=record.thread_ts if record is not None else None,
                pan_status=record.status if record is not None else None,
                is_pan_owned=record is not None,
                drift=record is not None and session_drift(record.status, terminal),
            )
        )
    logger.info(f"sessions collected count={len(summaries)}")
    return summaries
