from __future__ import annotations

from pan.errors import MorcliError
from pan.logging import initialise_logger
from pan.models import AgentStatus, LiveSession, SessionSummary, ThreadRecord, WorkerStatus
from pan.seams import HerdrAdapter, MorcliAdapter, ThreadMap

logger = initialise_logger(__name__)

# herdr's live agent status normalized to the pan WorkerStatus it implies, reusing the
# same intent as morcli._STATUS_MAP (idle/working -> RUNNING, blocked -> BLOCKED,
# done -> DONE). AgentStatus.UNKNOWN has no clean pan equivalent, so it is left out and
# treated as "cannot determine drift" below.
_AGENT_TO_WORKER: dict[AgentStatus, WorkerStatus] = {
    AgentStatus.IDLE: WorkerStatus.RUNNING,
    AgentStatus.WORKING: WorkerStatus.RUNNING,
    AgentStatus.BLOCKED: WorkerStatus.BLOCKED,
    AgentStatus.DONE: WorkerStatus.DONE,
}


def session_drift(pan_status: WorkerStatus, agent_status: AgentStatus) -> bool:
    # True when pan's recorded status disagrees with the WorkerStatus that herdr's live
    # agent_status implies. SPAWNING is excluded (a worker still coming up legitimately
    # has no herdr agent status yet), and an UNKNOWN agent_status cannot be reconciled,
    # so both yield "no drift".
    if pan_status is WorkerStatus.SPAWNING:
        return False
    implied_status = _AGENT_TO_WORKER.get(agent_status)
    if implied_status is None:
        return False
    return implied_status is not pan_status


def _match_pan_record(
    live_session: LiveSession, records: list[ThreadRecord]
) -> ThreadRecord | None:
    # Pan-ownership join. Primary key: workspace_name (pan labels are pan-<slug>). Fallback:
    # the resolved worktree path equals the pane's resolved cwd (symlink-safe, mirroring
    # FileThreadMap.get_by_worktree). First match wins.
    for record in records:
        if record.workspace_name == live_session.workspace_name:
            return record
    target_cwd = live_session.cwd.resolve()
    for record in records:
        if record.worktree_path.resolve() == target_cwd:
            return record
    return None


def collect_sessions(
    herdr: HerdrAdapter,
    thread_map: ThreadMap,
    morcli: MorcliAdapter | None,
) -> list[SessionSummary]:
    # Pure reconcile over the injected seams: enumerate every live herdr session, join
    # the pan thread map to identify pan-owned ones, flag status drift, and best-effort
    # enrich with morcli (tolerating MorcliError so a morcli hiccup never drops a row).
    # The thread map is read, never rewritten (INV-7); drift is reported, not healed.
    records = thread_map.records()
    summaries: list[SessionSummary] = []
    for live_session in herdr.list_workspaces():
        record = _match_pan_record(live_session, records)
        if record is None:
            summaries.append(
                SessionSummary(
                    workspace_name=live_session.workspace_name,
                    workspace_id=live_session.workspace_id,
                    pane_id=live_session.pane_id,
                    cwd=live_session.cwd,
                    agent_status=live_session.agent_status,
                )
            )
            continue

        drift = session_drift(record.status, live_session.agent_status)
        morcli_session = _resolve_morcli_session(morcli, record, live_session.workspace_id)
        summaries.append(
            SessionSummary(
                workspace_name=live_session.workspace_name,
                workspace_id=live_session.workspace_id,
                pane_id=live_session.pane_id,
                cwd=live_session.cwd,
                agent_status=live_session.agent_status,
                thread_ts=record.thread_ts,
                pan_status=record.status,
                morcli_session=morcli_session,
                is_pan_owned=True,
                drift=drift,
            )
        )
    return summaries


def _resolve_morcli_session(
    morcli: MorcliAdapter | None, record: ThreadRecord, workspace_id: str
) -> str | None:
    # Best-effort morcli enrichment: confirm the handle is live and, when it is, record
    # it. A MorcliError (morcli down or the session not indexed yet) is tolerated — the
    # handle degrades to whatever the thread record already holds and the session is
    # still listed.
    if morcli is None:
        return record.morcli_session
    handle = record.morcli_session or workspace_id
    try:
        morcli.session_status(handle)
    except MorcliError:
        logger.info(f"morcli enrich tolerated handle={handle} thread={record.thread_ts}")
        return record.morcli_session
    return handle
