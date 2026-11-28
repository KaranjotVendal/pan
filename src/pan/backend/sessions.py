from __future__ import annotations

from pathlib import Path

from pan.backend.project_map import (
    ProjectPlace,
    is_live_agent_session,
    is_remote,
    map_projects,
)
from pan.core.errors import PrivateMessageError, SlackPostError
from pan.core.events import SessionsListed
from pan.core.models import (
    AgentStatus,
    Conversation,
    SessionRole,
    SessionSummary,
    Terminal,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.core.seams import GitWorktreeAdapter, Reporter, TerminalHost, ThreadMap
from pan.data.transcript import last_user_text
from pan.data.workset import read_workset_goal
from pan.utils.config import (
    SESSION_LAST_ASK_MAX_LENGTH,
    STREAM_NAME_PREFIX,
    WORKER_BRIEF_TASK_MARKER,
    WORKSET_RELATIVE_PATH,
)
from pan.utils.logging import initialise_logger

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


def _names_agree(record: ThreadRecord, terminal: Terminal) -> bool:
    # A record's terminal name and a terminal's name agree unless both are set and differ.
    return (
        not record.terminal_name or terminal.name is None or record.terminal_name == terminal.name
    )


def _in_own_worktree(record: ThreadRecord, terminal: Terminal) -> bool:
    # A pan worker's terminal running in the worker's worktree (resolved, symlink-safe,
    # mirroring FileThreadMap.get_by_worktree). A desk session's record holds a folder pan
    # does not own, so another terminal in that folder never qualifies.
    return record.started_by_pan and record.worktree_path.resolve() == terminal.cwd.resolve()


def joins_by_address(record: ThreadRecord, terminal: Terminal) -> bool:
    # The terminal address recorded at spawn (or at a `to <name>:` tie), with names that
    # agree: hc can reuse an address for a new terminal of another name. hc cannot rename a
    # terminal, so a different name at the same address means a different terminal.
    return (
        bool(record.terminal_address)
        and record.terminal_address == terminal.address
        and _names_agree(record, terminal)
    )


def joins_by_name(record: ThreadRecord, terminal: Terminal) -> bool:
    # The terminal name, for a record with no address (legacy records), or for a pan worker
    # whose terminal came back at a new address (e.g. after a Herdr restart) in its own
    # worktree. A record whose address names another terminal otherwise never joins by
    # name: it belongs to an earlier terminal of the same name.
    if terminal.name is None or record.terminal_name != terminal.name:
        return False
    return not record.terminal_address or _in_own_worktree(record, terminal)


def joins_terminal(record: ThreadRecord, terminal: Terminal) -> bool:
    return joins_by_address(record, terminal) or joins_by_name(record, terminal)


def _match_record(terminal: Terminal, records: list[ThreadRecord]) -> ThreadRecord | None:
    # The join to pan's records, most precise first: by address, then by name (see
    # joins_by_address / joins_by_name), then, for pan's own workers with no recorded address
    # only, the worktree path against the terminal's launch folder. A record whose address
    # names another terminal never joins by worktree alone, so a new, differently named
    # terminal in a killed worker's worktree does not inherit its record.
    by_address = [record for record in records if joins_by_address(record, terminal)]
    if by_address:
        return _preferred(by_address)
    by_name = [record for record in records if joins_by_name(record, terminal)]
    if by_name:
        return _preferred(by_name)
    return _preferred(
        [
            record
            for record in records
            if _in_own_worktree(record, terminal) and not record.terminal_address
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


def inbox_thread_known(record: ThreadRecord) -> bool:
    # pan knows the session's Inbox thread when it named it (its own workers) or found it (an
    # id is recorded). A desk session given only a suggested name may report elsewhere.
    return record.started_by_pan or record.inbox_thread_id is not None


def _known_inbox_thread_name(record: ThreadRecord | None) -> str | None:
    # The session's Inbox thread when pan knows it; a name pan only suggested is not shown.
    if record is None or record.inbox_thread_name is None or not inbox_thread_known(record):
        return None
    return record.inbox_thread_name


def _workset_goal(terminal: Terminal, root: Terminal) -> str | None:
    # The stream's workset file, looked for under its project root's folder and then its own
    # (the same folder for a root). Folders on another machine are not read.
    if terminal.name is None:
        return None
    stream = terminal.name.removeprefix(STREAM_NAME_PREFIX)
    if not stream or "/" in stream or stream in {".", ".."}:
        # hc names never hold these; refuse rather than read outside docs/streams.
        return None
    relative_path = WORKSET_RELATIVE_PATH.format(stream=stream)
    folders: list[Path] = []
    for candidate in (root, terminal):
        if not is_remote(candidate.address) and candidate.cwd not in folders:
            folders.append(candidate.cwd)
    for folder in folders:
        goal = read_workset_goal(folder / relative_path)
        if goal is not None:
            return goal
    return None


class _BranchReader:
    # One git read per distinct local folder of a live agent session; other terminals (shells,
    # dev servers, finished ones) and folders on another machine get no branch.
    def __init__(self, git: GitWorktreeAdapter) -> None:
        self._git = git
        self._branches: dict[Path, str | None] = {}

    def branch_of(self, terminal: Terminal) -> str | None:
        if is_remote(terminal.address) or not is_live_agent_session(terminal):
            return None
        if terminal.cwd not in self._branches:
            self._branches[terminal.cwd] = self._git.current_branch(terminal.cwd)
        return self._branches[terminal.cwd]


def _summary(
    terminal: Terminal,
    record: ThreadRecord | None,
    place: ProjectPlace,
    *,
    branch: str | None,
    workset_goal: str | None,
) -> SessionSummary:
    # A desk session (a record with started_by_pan False) is not pan's: it has a thread but no
    # pan status, since no pan hooks report its turns.
    pan_record = record if record is not None and record.started_by_pan else None
    return SessionSummary(
        address=terminal.address,
        name=terminal.name,
        where=terminal.where,
        cwd=terminal.cwd,
        agent=terminal.agent,
        agent_status=terminal.agent_status,
        thread_ts=record.conversation.id if record is not None else None,
        thread_channel=record.conversation.place if record is not None else None,
        pan_status=pan_record.status if pan_record is not None else None,
        is_pan_owned=pan_record is not None,
        drift=pan_record is not None and session_drift(pan_record.status, terminal),
        last_ask=last_ask_for(record),
        inbox_thread_name=_known_inbox_thread_name(record),
        label=record.status_label if record is not None else None,
        updated_at=record.updated_at if record is not None else None,
        parent=place.parent,
        role=place.role,
        project=place.project,
        project_root=place.project_root,
        branch=branch,
        workset_goal=workset_goal,
    )


def collect_sessions(
    terminal_host: TerminalHost, thread_map: ThreadMap, *, git: GitWorktreeAdapter
) -> list[SessionSummary]:
    # Pure reconcile over the injected seams: enumerate every live terminal, join the
    # pan thread map to identify pan-owned ones, and flag status drift. Ownership comes
    # only from the thread map, never from host labels, so a host without labels works. The
    # thread map is read, never rewritten (INV-7); drift is reported, not healed. Each session
    # also gets its place in the project map, its folder's branch and its workset goal.
    records = thread_map.records()
    terminals = terminal_host.list_terminals()
    by_address = {terminal.address: terminal for terminal in terminals}
    places = map_projects(terminals)
    branch_reader = _BranchReader(git)
    summaries: list[SessionSummary] = []
    for terminal in terminals:
        place = places[terminal.address]
        summaries.append(
            _summary(
                terminal,
                _match_record(terminal, records),
                place,
                branch=branch_reader.branch_of(terminal),
                workset_goal=(
                    _workset_goal(terminal, by_address[place.project_root])
                    if place.role is SessionRole.STREAM
                    else None
                ),
            )
        )
    logger.info(f"sessions collected count={len(summaries)}")
    return summaries


def send_sessions_privately(
    reporter: Reporter,
    *,
    conversation: Conversation,
    requester: str,
    sessions: list[SessionSummary],
    any_running: bool,
) -> None:
    """Send the sessions list to the requester alone; on failure nothing is posted to the
    thread and PrivateMessageError (exit 32) is raised."""
    listed = SessionsListed(requester=requester, sessions=sessions, any_running=any_running)
    try:
        reporter.report(conversation, listed)
    except SlackPostError as e:
        logger.exception(
            f"sessions list could not be sent privately conversation={conversation.key}"
        )
        raise PrivateMessageError(
            f"sessions list could not be sent privately to thread {conversation.id}"
        ) from e
    logger.info(
        f"sessions list sent privately conversation={conversation.key} count={len(sessions)}"
    )
