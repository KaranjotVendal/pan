from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pan.config import STATUS_LABEL_MAX_LENGTH
from pan.models import (
    AgentStatus,
    StatusState,
    Terminal,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.sessions import inbox_thread_known

# What pan's status message in a thread looks like. Everything here is pure: the blocks and
# texts are built from a record and the time, and the gateway adapter only sends them. The
# answer itself is converted to Slack mrkdwn at the single egress (slack_format.to_slack_mrkdwn).

STATUS_TITLES: dict[StatusState, str] = {
    StatusState.WORKING: "Working",
    StatusState.WAITING: "Waiting for you",
    StatusState.FINISHED: "Finished",
    StatusState.CLOSED: "Closed",
    StatusState.STOPPED: "Stopped",
}

# The state `check` shows for a live terminal, from the agent status hc reports. hc's `done`
# means the turn finished, not that the session was closed: Closed comes only from `@pan done`.
_STATE_FOR_LIVE_AGENT: dict[AgentStatus, StatusState] = {
    AgentStatus.WORKING: StatusState.WORKING,
    AgentStatus.BLOCKED: StatusState.WAITING,
    AgentStatus.IDLE: StatusState.FINISHED,
    AgentStatus.DONE: StatusState.FINISHED,
    AgentStatus.ERROR: StatusState.STOPPED,
}
# A live terminal reporting no status or an unknown one (a fresh Claude session before its
# first turn, say) shows what pan recorded; the terminal is live, so never Closed or Stopped.
_STATE_FOR_LIVE_RECORD: dict[WorkerStatus, StatusState] = {
    WorkerStatus.SPAWNING: StatusState.WORKING,
    WorkerStatus.RUNNING: StatusState.WORKING,
    WorkerStatus.BLOCKED: StatusState.WAITING,
    WorkerStatus.IDLE: StatusState.FINISHED,
    WorkerStatus.DONE: StatusState.FINISHED,
    WorkerStatus.FAILED: StatusState.FINISHED,
}

_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60


def check_state(terminal: Terminal | None, recorded: WorkerStatus) -> StatusState:
    # The status message's state on `check`. A terminal that is gone (or finished) is Stopped,
    # unless pan recorded the session as closed by `@pan done` (DONE is only set there).
    if terminal is None or terminal.state is TerminalState.FINISHED:
        return StatusState.CLOSED if recorded is WorkerStatus.DONE else StatusState.STOPPED
    live_state = _STATE_FOR_LIVE_AGENT.get(terminal.agent_status)
    return live_state if live_state is not None else _STATE_FOR_LIVE_RECORD[recorded]


def status_label(task: str, max_length: int = STATUS_LABEL_MAX_LENGTH) -> str:
    # The task on one line: whitespace collapsed, cut short with "..." past max_length.
    short_task = " ".join(task.split())
    if len(short_task) > max_length:
        short_task = short_task[: max_length - 3].rstrip() + "..."
    return short_task


def escape_mrkdwn(text: str) -> str:
    # Slack's three control characters, so text from a person or a session is shown as is.
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def elapsed_text(elapsed: timedelta) -> str | None:
    # "4m", "2h 5m"; None under a minute, which is not worth showing.
    total_minutes = int(elapsed.total_seconds()) // _SECONDS_PER_MINUTE
    if total_minutes < 1:
        return None
    hours, minutes = divmod(total_minutes, _MINUTES_PER_HOUR)
    if hours == 0:
        return f"{minutes}m"
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def slack_time(moment: datetime) -> str:
    # A time Slack shows in each reader's own time zone, with a UTC fallback.
    utc_moment = moment.astimezone(UTC)
    return f"<!date^{int(utc_moment.timestamp())}^{{time}}|{utc_moment:%H:%M} UTC>"


def status_text(state: StatusState, label: str | None) -> str:
    # The plain-text fallback (notifications, clients without blocks).
    title = STATUS_TITLES[state]
    return f"{title}: {label}" if label else title


def status_context_text(
    *,
    session_name: str,
    started_at: datetime | None,
    now: datetime,
    replies_in_thread: bool,
) -> str:
    parts = [escape_mrkdwn(session_name)]
    if started_at is not None:
        elapsed = elapsed_text(now - started_at)
        if elapsed is not None:
            parts.append(f"{elapsed} elapsed")
    parts.append(f"updated {slack_time(now)}")
    if replies_in_thread:
        parts.append("answers posted here")
    return " · ".join(parts)


def status_blocks(
    state: StatusState,
    *,
    session_name: str,
    label: str | None,
    started_at: datetime | None,
    now: datetime,
    replies_in_thread: bool = False,
    detail: str | None = None,
) -> list[dict[str, Any]]:
    # A section with the state in bold and the task label under it (and, when waiting, what
    # the session waits for), then a context line: session name, elapsed and updated time.
    lines = [f"*{STATUS_TITLES[state]}*"]
    if label:
        lines.append(escape_mrkdwn(label))
    if detail:
        lines.append(f"_{escape_mrkdwn(status_label(detail))}_")
    context = status_context_text(
        session_name=session_name,
        started_at=started_at,
        now=now,
        replies_in_thread=replies_in_thread,
    )
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": context}]},
    ]


def _inbox_line(record: ThreadRecord) -> str:
    # The session's Inbox thread: a link once captured, else its name; a name pan only
    # suggested to a desk session is shown as the alternative it is.
    name = record.inbox_thread_name
    if record.inbox_thread_url:
        shown_name = escape_mrkdwn(name) if name else "open"
        return f"Inbox thread: <{record.inbox_thread_url}|{shown_name}>"
    if name and inbox_thread_known(record):
        return f'Inbox thread: "{escape_mrkdwn(name)}"'
    if name:
        return f'Inbox thread: its existing one, or "{escape_mrkdwn(name)}"'
    return "Inbox thread: none yet"


def private_details_text(record: ThreadRecord) -> str:
    # The details only the requester sees (an ephemeral message): the session, its terminal
    # and its Inbox thread, whose link is on this machine and useless to anyone else.
    lines = [f"Details for {escape_mrkdwn(record.terminal_name)} (only visible to you)"]
    lines.append(_inbox_line(record))
    if record.terminal_address:
        lines.append(f"Terminal: {escape_mrkdwn(record.terminal_address)}")
    return "\n".join(lines)
