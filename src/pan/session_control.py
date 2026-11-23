from __future__ import annotations

import difflib

from pan.config import CLAUDE_AGENT_NAMES, WORKER_BRIEF_TASK_MARKER
from pan.errors import (
    SessionBusyError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
    TerminalGoneError,
    TerminalHostError,
    ThreadNotFoundError,
)
from pan.gateway.slack_post import slack_post, slack_post_parts
from pan.interface import read_session, resolve_target
from pan.logging import initialise_logger
from pan.models import (
    AgentStatus,
    ReplyMode,
    Terminal,
    TerminalState,
    ThreadRecord,
    ThreadSource,
    WorkerStatus,
)
from pan.seams import Clock, InboxDirectory, SlackAdapter, TerminalHost, ThreadMap
from pan.sessions import worker_status_for
from pan.spawn import desk_session_ended_reply, inbox_thread_name
from pan.transcript import last_assistant_text

logger = initialise_logger(__name__)

UNTIED_THREAD_REPLY = "this thread isn't tied to a session; @pan sessions lists them"
# The line on top of text typed into a session from Slack (see from_slack_text).
FROM_SLACK_HEADER = (
    "From Slack via pan. Put questions and progress in the Plannotator Inbox thread named "
    '"{inbox_thread_name}"; Slack gets a reply only when someone asks for one.'
)
# The agent states in which a session is waiting for input, so text can be typed into it. Any
# other state (working, an error, no status, an unknown one) counts as busy, except a fresh
# Claude session (see _is_ready).
_READY_STATUSES = frozenset({AgentStatus.IDLE, AgentStatus.DONE})
_CLOSE_MATCH_LIMIT = 3
_CLOSE_MATCH_CUTOFF = 0.6

# What `check` says about the session's state, from the status its terminal implies.
_STATE_SENTENCES: dict[WorkerStatus, str] = {
    WorkerStatus.SPAWNING: "{name} is starting, so there may be no answer yet.",
    WorkerStatus.RUNNING: "{name} is still working, so this answer may not be final.",
    WorkerStatus.IDLE: "{name} is idle.",
    WorkerStatus.BLOCKED: "{name} is waiting on a prompt at the terminal.",
    WorkerStatus.DONE: "{name} has finished.",
    WorkerStatus.FAILED: "{name} has ended.",
}


def inbox_reference(record: ThreadRecord) -> str | None:
    # The session's Inbox thread: its link once the inbox-link hook captured it, else the
    # name pan gave it.
    if record.inbox_thread_url:
        return f"Inbox: {record.inbox_thread_url}"
    if record.inbox_thread_name:
        return f'Inbox thread: "{record.inbox_thread_name}"'
    return None


def _session_name(terminal: Terminal) -> str:
    return terminal.name or terminal.address


def _session_record_for(terminal: Terminal, records: list[ThreadRecord]) -> ThreadRecord | None:
    # The record pan keeps for the session in this terminal (pan's own or a desk session),
    # joined by address, then by terminal name; a tied record wins, then the newest.
    matches = [
        record
        for record in records
        if (record.terminal_address and record.terminal_address == terminal.address)
        or (terminal.name is not None and record.terminal_name == terminal.name)
    ]
    for record in matches:
        if record.tied:
            return record
    return max(matches, key=lambda record: record.updated_at) if matches else None


def _not_found_reply(selector: str, terminals: list[Terminal]) -> str:
    names = [_session_name(terminal) for terminal in terminals]
    close = [name for name in names if selector.lower() in name.lower()]
    for name in difflib.get_close_matches(
        selector, names, n=_CLOSE_MATCH_LIMIT, cutoff=_CLOSE_MATCH_CUTOFF
    ):
        if name not in close:
            close.append(name)
    if close:
        return f"no live session named {selector}; close matches: {', '.join(close[:_CLOSE_MATCH_LIMIT])}"
    return f"no live session named {selector}; @pan sessions lists them"


def _ambiguous_reply(selector: str, candidates: list[Terminal]) -> str:
    labels = ", ".join(
        f"{_session_name(candidate)} ({candidate.where})"
        if candidate.where
        else _session_name(candidate)
        for candidate in candidates
    )
    return f"{selector} matches more than one session: {labels}; name one exactly"


def sent_notice(record: ThreadRecord) -> str:
    notice = f"Sent to {record.terminal_name}"
    reference = inbox_reference(record)
    if reference is not None:
        notice = f"{notice} · {reference}"
    if record.reply_mode is ReplyMode.SLACK:
        notice = f"{notice} · answers will also be posted here"
    return notice


def from_slack_text(inbox_thread: str, text: str) -> str:
    # The text typed into a session, under one line saying where it came from and which Inbox
    # thread to report in. The brief's task marker separates them, so `pan sessions` shows only
    # the task as the session's last ask.
    header = FROM_SLACK_HEADER.format(inbox_thread_name=inbox_thread)
    return f"{header}{WORKER_BRIEF_TASK_MARKER}{text}"


def _busy_reply(name: str) -> str:
    return f"{name} is busy; try again, or @pan check"


def _blocked_reply(name: str) -> str:
    return f"{name} is waiting on a prompt at the terminal; nothing was sent"


def _is_ready(terminal: Terminal) -> bool:
    # A Claude session reports no status until its first turn ends, so a brand-new one reads
    # NONE while it waits at its prompt. NONE counts as ready only for a known Claude agent
    # that has not finished a turn (or whose host does not count turns); a shell or other
    # program with no status, UNKNOWN and WORKING stay refused.
    if terminal.agent_status in _READY_STATUSES:
        return True
    return (
        terminal.agent_status is AgentStatus.NONE
        and terminal.agent in CLAUDE_AGENT_NAMES
        and (terminal.turn is None or terminal.turn == 0)
    )


def _deliver(
    terminal_host: TerminalHost,
    terminal: Terminal,
    text: str,
    *,
    slack: SlackAdapter,
    channel: str,
    thread_ts: str,
) -> None:
    # Type the text only into a session that is ready for it. Busy, or a state pan cannot
    # read, is refused (typing could land in the middle of someone's work there); waiting on
    # a person is refused too, by pan here or by the host at the send. A refusal posts a
    # short reply and raises, with nothing typed.
    name = _session_name(terminal)
    if terminal.agent_status is AgentStatus.BLOCKED:
        slack_post(slack, channel, thread_ts, _blocked_reply(name))
        raise TargetBlockedError(f"{terminal.address} is waiting on a person")
    if not _is_ready(terminal):
        slack_post(slack, channel, thread_ts, _busy_reply(name))
        raise SessionBusyError(f"{terminal.address} is {terminal.agent_status.value}")
    try:
        terminal_host.send(terminal.address, text)
    except TargetBlockedError:
        slack_post(slack, channel, thread_ts, _blocked_reply(name))
        raise


def _with_inbox_link(
    record: ThreadRecord, inbox_directory: InboxDirectory, thread_map: ThreadMap
) -> ThreadRecord:
    # A session pan has no hooks in (a desk session, or a worker whose hook missed the link)
    # gets its Inbox thread looked up by the name pan gave it, in the session's folder, each
    # time until it is found (the session creates the thread only on its first Inbox send,
    # so the lookup at send time usually misses and `check` finds it later). The id and any
    # link found are stored; with nothing found the name alone is shown.
    if record.inbox_thread_name is None or record.inbox_thread_id is not None:
        return record
    found = inbox_directory.find_thread(record.worktree_path, record.inbox_thread_name)
    if found is None:
        return record
    thread_map.record_inbox_thread(
        record.thread_ts, found.thread_id, found.url, session_name=record.terminal_name
    )
    return record.model_copy(
        update={
            "inbox_thread_id": found.thread_id,
            "inbox_thread_url": found.url or record.inbox_thread_url,
        }
    )


def send_to_session(
    *,
    thread_ts: str,
    channel: str,
    selector: str,
    text: str,
    requester: str | None,
    reply_in_slack: bool,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    inbox_directory: InboxDirectory,
    slack: SlackAdapter,
    clock: Clock,
) -> ThreadRecord:
    # `to <name>: <task>`: send the task to the live session under that name, pan's own or a
    # desk session, and tie the thread to it (the session's previous thread is untied). The
    # task goes under the "from Slack" line naming its Inbox thread. Every refusal (unknown,
    # ambiguous, busy, waiting on a prompt) posts a short reply and raises, with nothing sent.
    terminals = terminal_host.list_terminals()
    try:
        target = resolve_target(selector, terminals)
    except TargetNotFoundError:
        slack_post(slack, channel, thread_ts, _not_found_reply(selector, terminals))
        raise
    except TargetAmbiguousError as error:
        slack_post(slack, channel, thread_ts, _ambiguous_reply(selector, error.candidates))
        raise

    name = _session_name(target)
    session_record = _session_record_for(target, thread_map.records())
    inbox_thread = (
        session_record.inbox_thread_name
        if session_record is not None and session_record.inbox_thread_name
        else inbox_thread_name(name, text)
    )
    _deliver(
        terminal_host,
        target,
        from_slack_text(inbox_thread, text),
        slack=slack,
        channel=channel,
        thread_ts=thread_ts,
    )

    now = clock.now()
    reply_mode = ReplyMode.SLACK if reply_in_slack else ReplyMode.ON_DEMAND
    if session_record is None:
        # A desk session: pan did not start it, so its record holds its own folder and pan
        # never stops, removes or reopens it.
        tied_record = ThreadRecord(
            thread_ts=thread_ts,
            terminal_name=name,
            terminal_address=target.address,
            channel=channel,
            worktree_path=target.cwd,
            status=WorkerStatus.RUNNING,
            created_at=now,
            updated_at=now,
            source=ThreadSource.SLACK,
            started_by_pan=False,
            requester=requester,
            reply_mode=reply_mode,
            inbox_thread_name=inbox_thread,
        )
    else:
        same_thread = session_record.thread_ts == thread_ts
        updates: dict[str, object] = {
            "thread_ts": thread_ts,
            "channel": channel,
            "terminal_address": target.address,
            "source": ThreadSource.SLACK,
            "requester": requester,
            "reply_mode": reply_mode,
            "inbox_thread_name": inbox_thread,
            "created_at": session_record.created_at if same_thread else now,
            "updated_at": now,
        }
        if not session_record.started_by_pan:
            # No hooks report a desk session's turns; it was just given work.
            updates["status"] = WorkerStatus.RUNNING
            updates["worktree_path"] = target.cwd
        tied_record = session_record.model_copy(update=updates)
    thread_map.tie(tied_record)
    tied_record = _with_inbox_link(tied_record, inbox_directory, thread_map)
    slack_post(slack, channel, thread_ts, sent_notice(tied_record))
    logger.info(
        f"sent to session terminal={target.address} thread={thread_ts} "
        f"started_by_pan={tied_record.started_by_pan} len={len(text)}"
    )
    return tied_record


def follow_up_to_session(
    *,
    thread_ts: str,
    channel: str,
    text: str,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    slack: SlackAdapter,
) -> ThreadRecord:
    # A message with no route words in a tied thread goes to the tied session, with the same
    # busy and blocked refusals as `to <name>:`. A desk session gets the "from Slack" line;
    # pan's own sessions already know their Inbox thread from their brief. When the session
    # has ended: for pan's own, nothing is posted and TerminalGoneError tells the caller to
    # reopen it; a desk session is never reopened, so pan says it ended.
    record = thread_map.get(thread_ts)
    if record is None or not record.tied:
        slack_post(slack, channel, thread_ts, UNTIED_THREAD_REPLY)
        raise ThreadNotFoundError(f"no tied session for thread_ts={thread_ts}")
    terminal = _live_terminal(terminal_host, record)
    if terminal is None:
        if not record.started_by_pan:
            slack_post(slack, channel, thread_ts, desk_session_ended_reply(record.terminal_name))
        raise TerminalGoneError(f"the session {record.terminal_name} has ended")

    typed = text
    if not record.started_by_pan:
        inbox_thread = record.inbox_thread_name or inbox_thread_name(record.terminal_name, text)
        typed = from_slack_text(inbox_thread, text)
    _deliver(terminal_host, terminal, typed, slack=slack, channel=channel, thread_ts=thread_ts)
    if not record.started_by_pan:
        thread_map.update_status(thread_ts, WorkerStatus.RUNNING, session_name=record.terminal_name)
    logger.info(f"follow-up sent terminal={terminal.address} thread={thread_ts} len={len(text)}")
    return record


def forget_thread(
    *, thread_ts: str, channel: str, thread_map: ThreadMap, slack: SlackAdapter
) -> None:
    # Untie the thread. The session is never stopped or touched.
    record = thread_map.get(thread_ts)
    if record is None or not record.tied:
        slack_post(slack, channel, thread_ts, UNTIED_THREAD_REPLY)
        return
    thread_map.untie(thread_ts)
    slack_post(
        slack,
        channel,
        thread_ts,
        f"untied this thread from {record.terminal_name}; the session was not stopped",
    )
    logger.info(f"forget thread={thread_ts}")


def _live_terminal(terminal_host: TerminalHost, record: ThreadRecord) -> Terminal | None:
    selector = record.terminal_address or record.terminal_name
    try:
        terminal = terminal_host.get(selector)
    except TerminalGoneError:
        return None
    return terminal if terminal.state is TerminalState.LIVE else None


def check_reply(record: ThreadRecord, terminal_host: TerminalHost, *, screen_lines: int) -> str:
    # The tied session's latest answer, as is: the last assistant text from its transcript,
    # or, without one, its recent screen lines labelled as such. Headed by the session name,
    # its Inbox thread and what state it is in.
    terminal = _live_terminal(terminal_host, record)
    status = worker_status_for(terminal, record.status)
    header = record.terminal_name
    reference = inbox_reference(record)
    header = (
        f"{header} · {reference}" if reference is not None else f"{header} · no Inbox thread yet"
    )
    state = _STATE_SENTENCES[status].format(name=record.terminal_name)

    answer = last_assistant_text(record.transcript_path) if record.transcript_path else None
    if answer is not None:
        body = answer
    elif terminal is not None:
        try:
            screen = read_session(
                terminal_host,
                [],
                terminal.address,
                [terminal],
                full=False,
                lines=screen_lines,
                full_history_lines=screen_lines,
            )
            body = f"Screen text (no transcript yet):\n```\n{screen}\n```"
        except TerminalHostError:
            body = "Its screen could not be read."
    else:
        body = "No answer recorded."
    return f"{header}\n{state}\n\n{body}"


def check_thread(
    *,
    thread_ts: str,
    channel: str,
    thread_map: ThreadMap,
    terminal_host: TerminalHost,
    inbox_directory: InboxDirectory,
    slack: SlackAdapter,
    screen_lines: int,
) -> None:
    record = thread_map.get(thread_ts)
    if record is None or not record.tied:
        slack_post(slack, channel, thread_ts, UNTIED_THREAD_REPLY)
        return
    record = _with_inbox_link(record, inbox_directory, thread_map)
    slack_post_parts(
        slack, channel, thread_ts, check_reply(record, terminal_host, screen_lines=screen_lines)
    )
    logger.info(f"check thread={thread_ts} session={record.terminal_name}")
