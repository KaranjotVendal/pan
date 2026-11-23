from __future__ import annotations

import difflib

from pan.errors import (
    NotPanSessionError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
    TerminalGoneError,
    TerminalHostError,
)
from pan.gateway.slack_post import slack_post, slack_post_parts
from pan.interface import read_session, resolve_target
from pan.logging import initialise_logger
from pan.models import (
    ReplyMode,
    Terminal,
    TerminalState,
    ThreadRecord,
    ThreadSource,
    WorkerStatus,
)
from pan.seams import Clock, SlackAdapter, TerminalHost, ThreadMap
from pan.sessions import worker_status_for
from pan.transcript import last_assistant_text

logger = initialise_logger(__name__)

UNTIED_THREAD_REPLY = "this thread isn't tied to a session; @pan sessions lists them"
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


def _pan_record_for(terminal: Terminal, records: list[ThreadRecord]) -> ThreadRecord | None:
    # The record of the session pan started in this terminal, joined by address, then by
    # terminal name; a tied record wins over an untied one.
    matches = [
        record
        for record in records
        if record.started_by_pan
        and (
            (record.terminal_address and record.terminal_address == terminal.address)
            or (terminal.name is not None and record.terminal_name == terminal.name)
        )
    ]
    for record in matches:
        if record.tied:
            return record
    return matches[0] if matches else None


def _not_found_reply(selector: str, terminals: list[Terminal], records: list[ThreadRecord]) -> str:
    pan_names = [
        _session_name(terminal)
        for terminal in terminals
        if _pan_record_for(terminal, records) is not None
    ]
    close = [name for name in pan_names if selector.lower() in name.lower()]
    for name in difflib.get_close_matches(
        selector, pan_names, n=_CLOSE_MATCH_LIMIT, cutoff=_CLOSE_MATCH_CUTOFF
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
    slack: SlackAdapter,
    clock: Clock,
) -> ThreadRecord:
    # `to <name>: <task>`: send the task to the live session pan started under that name and
    # tie the thread to it (the session's previous thread is untied). Every refusal posts a
    # short reply and raises, with nothing sent.
    terminals = terminal_host.list_terminals()
    records = thread_map.records()
    try:
        target = resolve_target(selector, terminals)
    except TargetNotFoundError:
        slack_post(slack, channel, thread_ts, _not_found_reply(selector, terminals, records))
        raise
    except TargetAmbiguousError as error:
        slack_post(slack, channel, thread_ts, _ambiguous_reply(selector, error.candidates))
        raise

    session_record = _pan_record_for(target, records)
    name = _session_name(target)
    if session_record is None:
        slack_post(
            slack,
            channel,
            thread_ts,
            f"{name} is not a pan session; reaching your own sessions comes next",
        )
        raise NotPanSessionError(f"{target.address} was not started by pan")

    try:
        terminal_host.send(target.address, text)
    except TargetBlockedError:
        slack_post(
            slack,
            channel,
            thread_ts,
            f"{name} is waiting on a prompt at the terminal; nothing was sent",
        )
        raise

    now = clock.now()
    same_thread = session_record.thread_ts == thread_ts
    tied_record = session_record.model_copy(
        update={
            "thread_ts": thread_ts,
            "channel": channel,
            "terminal_address": target.address,
            "source": ThreadSource.SLACK,
            "requester": requester,
            "reply_mode": ReplyMode.SLACK if reply_in_slack else ReplyMode.ON_DEMAND,
            "created_at": session_record.created_at if same_thread else now,
            "updated_at": now,
        }
    )
    thread_map.tie(tied_record)
    slack_post(slack, channel, thread_ts, sent_notice(tied_record))
    logger.info(f"sent to session terminal={target.address} thread={thread_ts} len={len(text)}")
    return tied_record


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
    slack: SlackAdapter,
    screen_lines: int,
) -> None:
    record = thread_map.get(thread_ts)
    if record is None or not record.tied:
        slack_post(slack, channel, thread_ts, UNTIED_THREAD_REPLY)
        return
    slack_post_parts(
        slack, channel, thread_ts, check_reply(record, terminal_host, screen_lines=screen_lines)
    )
    logger.info(f"check thread={thread_ts} session={record.terminal_name}")
