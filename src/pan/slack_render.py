from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pan.config import (
    CLAUDE_AGENT_NAMES,
    SESSION_LAST_ASK_MAX_LENGTH,
    SESSIONS_COMPACT_LINE_MAX_LENGTH,
    STATUS_LABEL_MAX_LENGTH,
)
from pan.models import (
    AgentStatus,
    SessionSummary,
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


# `@pan sessions` in Slack: one line per session, grouped by state, needs-action first. The
# text is GFM (**bold**), turned into Slack mrkdwn at the single egress (slack_post).
SESSIONS_IDLE_TITLE = "Idle"
SESSIONS_GROUP_ORDER: tuple[str, ...] = (
    STATUS_TITLES[StatusState.WAITING],
    STATUS_TITLES[StatusState.WORKING],
    SESSIONS_IDLE_TITLE,
    STATUS_TITLES[StatusState.FINISHED],
    STATUS_TITLES[StatusState.STOPPED],
)
# Groups that need no action are listed on one line each, names only.
SESSIONS_COMPACT_GROUPS = frozenset(
    {
        SESSIONS_IDLE_TITLE,
        STATUS_TITLES[StatusState.FINISHED],
        STATUS_TITLES[StatusState.STOPPED],
    }
)
# A Herdr location's tab number ("jiracli-generate / 1"), which says nothing to a reader.
_WHERE_TAB_NUMBER = re.compile(r" / \d+$")
SESSIONS_FOOTER = "_Full details: run `pan sessions` on the Mac._"
SESSIONS_EMPTY_TEXT = "No sessions running."

_SESSIONS_GROUP_FOR_AGENT: dict[AgentStatus, str] = {
    AgentStatus.BLOCKED: STATUS_TITLES[StatusState.WAITING],
    AgentStatus.WORKING: STATUS_TITLES[StatusState.WORKING],
    AgentStatus.IDLE: SESSIONS_IDLE_TITLE,
    AgentStatus.DONE: STATUS_TITLES[StatusState.FINISHED],
    AgentStatus.ERROR: STATUS_TITLES[StatusState.STOPPED],
}
# For a live agent hc reports no known status for: what pan recorded. The terminal is live,
# so never Stopped (as in _STATE_FOR_LIVE_RECORD): a recorded failure reads as idle.
_SESSIONS_GROUP_FOR_PAN_STATUS: dict[WorkerStatus, str] = {
    WorkerStatus.SPAWNING: STATUS_TITLES[StatusState.WORKING],
    WorkerStatus.RUNNING: STATUS_TITLES[StatusState.WORKING],
    WorkerStatus.BLOCKED: STATUS_TITLES[StatusState.WAITING],
    WorkerStatus.IDLE: SESSIONS_IDLE_TITLE,
    WorkerStatus.DONE: STATUS_TITLES[StatusState.FINISHED],
    WorkerStatus.FAILED: SESSIONS_IDLE_TITLE,
}
# Emphasis characters that would garble text shown inside **bold** or _italic_.
_EMPHASIS_CHARACTERS = re.compile(r"[*~`]")
# An underscore not inside a word (`snake_case` is safe), which Slack can read as italics.
_LOOSE_UNDERSCORE = re.compile(r"(?<![^\W_])_|_(?![^\W_])")


def session_group(summary: SessionSummary) -> str | None:
    # The group a session is listed under; None for a plain shell (no status and not a Claude
    # agent), unless it is a pan worker still booting. A Claude session reports no status
    # until its first turn ends (see session_control._is_ready), so it and an unknown status
    # fall back to pan's record, else count as working.
    group = _SESSIONS_GROUP_FOR_AGENT.get(summary.agent_status)
    if group is not None:
        return group
    plain_shell = (
        summary.agent_status is AgentStatus.NONE and summary.agent not in CLAUDE_AGENT_NAMES
    )
    if plain_shell:
        booting = summary.pan_status is WorkerStatus.SPAWNING
        return STATUS_TITLES[StatusState.WORKING] if booting else None
    if summary.pan_status is not None:
        return _SESSIONS_GROUP_FOR_PAN_STATUS[summary.pan_status]
    return STATUS_TITLES[StatusState.WORKING]


def listed_sessions(
    summaries: list[SessionSummary], orchestrator_name: str
) -> list[SessionSummary]:
    # The sessions Slack lists: not pan's own orchestrator, not plain shells.
    return [
        summary
        for summary in summaries
        if summary.name != orchestrator_name and session_group(summary) is not None
    ]


def linked_sessions(
    summaries: list[SessionSummary], orchestrator_name: str
) -> list[SessionSummary]:
    # The listed sessions shown one per line with their thread link (not the compact groups).
    return [
        summary
        for summary in listed_sessions(summaries, orchestrator_name)
        if session_group(summary) not in SESSIONS_COMPACT_GROUPS
    ]


def plain_mrkdwn(text: str) -> str:
    # Free text made safe to wrap in **bold** or _italic_: control characters escaped and
    # emphasis characters dropped.
    without_emphasis = _EMPHASIS_CHARACTERS.sub("", escape_mrkdwn(text))
    return " ".join(_LOOSE_UNDERSCORE.sub("", without_emphasis).split())


def session_display_name(summary: SessionSummary) -> str:
    # Words, never an address or a path: the task label, the Inbox thread, the terminal name,
    # the Herdr location without its tab number, then the folder name.
    where = _WHERE_TAB_NUMBER.sub("", summary.where) if summary.where else None
    return summary.label or summary.inbox_thread_name or summary.name or where or summary.cwd.name


def _compact_group_line(group: str, members: list[SessionSummary]) -> str:
    # `**Idle (21):** a, b, c`, names only; past the length limit the rest is counted.
    prefix = f"**{group} ({len(members)}):** "
    names = [plain_mrkdwn(session_display_name(summary)) for summary in members]
    shown: list[str] = []
    for index, name in enumerate(names):
        remaining = len(names) - index - 1
        tail = f" and {remaining} more" if remaining else ""
        candidate = prefix + ", ".join([*shown, name]) + tail
        if shown and len(candidate) > SESSIONS_COMPACT_LINE_MAX_LENGTH:
            break
        shown.append(name)
    hidden_count = len(names) - len(shown)
    line = prefix + ", ".join(shown)
    return f"{line} and {hidden_count} more" if hidden_count else line


def _session_lines(summary: SessionSummary, permalinks: dict[str, str]) -> list[str]:
    line = f"• **{plain_mrkdwn(session_display_name(summary))}**"
    link = permalinks.get(summary.thread_ts) if summary.thread_ts else None
    if link:
        line += f" · <{link}|thread>"
    lines = [line]
    if summary.last_ask:
        lines.append(
            f"    _{plain_mrkdwn(status_label(summary.last_ask, SESSION_LAST_ASK_MAX_LENGTH))}_"
        )
    return lines


def render_sessions_for_slack(
    summaries: list[SessionSummary], permalinks: dict[str, str], *, orchestrator_name: str
) -> str:
    # `permalinks` maps a session's thread_ts to its thread's link; a session without one is
    # listed without a link.
    listed = listed_sessions(summaries, orchestrator_name)
    if not listed:
        return SESSIONS_EMPTY_TEXT
    noun = "session" if len(listed) == 1 else "sessions"
    lines = [f"**{len(listed)} {noun}**"]
    for group in SESSIONS_GROUP_ORDER:
        members = [summary for summary in listed if session_group(summary) == group]
        if not members:
            continue
        if group in SESSIONS_COMPACT_GROUPS:
            lines.append(_compact_group_line(group, members))
            continue
        lines.append(f"**{group}**")
        for summary in members:
            lines.extend(_session_lines(summary, permalinks))
    lines.append(SESSIONS_FOOTER)
    return "\n".join(lines)
