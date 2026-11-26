from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, assert_never

from pan.backend.project_map import is_agent_session
from pan.backend.spawn import start_failed_text
from pan.core.events import (
    ChoiceUnclear,
    CleanupFailed,
    NoSessionMatches,
    NotDelivered,
    NotDeliveredReason,
    PrivateDetails,
    QuestionAsked,
    SessionClosed,
    SessionNotice,
    SessionNoticeReason,
    StartFailed,
    ThreadNotice,
    ThreadNoticeReason,
    WorkKept,
)
from pan.core.models import AgentStatus, SessionRole, SessionSummary, StatusState, WorkerStatus
from pan.utils.config import SESSIONS_SLACK_MAX_COUNT
from pan.utils.text import status_label

# What pan's messages in a thread look like, from the events the backend reports
# (core.events). Everything here is pure: the blocks and texts are built from an event and the
# time, and frontend.slack_reporter only sends them. Every plain line is converted to Slack
# mrkdwn at the single egress (slack_format.to_slack_mrkdwn, through slack_post).

STATUS_TITLES: dict[StatusState, str] = {
    StatusState.WORKING: "Working",
    StatusState.WAITING: "Waiting for you",
    StatusState.FINISHED: "Finished",
    StatusState.CLOSED: "Closed",
    StatusState.STOPPED: "Stopped",
}

_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60


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


def _inbox_line(details: PrivateDetails) -> str:
    # The session's Inbox thread: a link once captured, else its name; a name pan only
    # suggested to a desk session is shown as the alternative it is.
    name = details.inbox_thread_name
    if details.inbox_thread_url:
        shown_name = escape_mrkdwn(name) if name else "open"
        return f"Inbox thread: <{details.inbox_thread_url}|{shown_name}>"
    if name and details.inbox_thread_known:
        return f'Inbox thread: "{escape_mrkdwn(name)}"'
    if name:
        return f'Inbox thread: its existing one, or "{escape_mrkdwn(name)}"'
    return "Inbox thread: none yet"


def private_details_text(details: PrivateDetails) -> str:
    # The details only the requester sees (an ephemeral message): the session, its terminal
    # and its Inbox thread, whose link is on this machine and useless to anyone else.
    lines = [f"Details for {escape_mrkdwn(details.session_name)} (only visible to you)"]
    lines.append(_inbox_line(details))
    if details.terminal_address:
        lines.append(f"Terminal: {escape_mrkdwn(details.terminal_address)}")
    return "\n".join(lines)


# `pan sessions --show` in Slack: the sessions the orchestrator picked, in its order, sent to
# the requester alone. The text is Slack mrkdwn, posted as is (chat.postEphemeral).
SESSIONS_IDLE_TITLE = "Idle"
# A Herdr location's tab number ("jiracli-generate / 1"), which says nothing to a reader.
_WHERE_TAB_NUMBER = re.compile(r" / \d+$")
SESSIONS_EMPTY_TEXT = "No sessions running."
SESSIONS_NONE_SHOWN_TEXT = "None of those sessions is running."
SESSIONS_HEADER = "*Your sessions*"

_SESSIONS_GROUP_FOR_AGENT: dict[AgentStatus, str] = {
    AgentStatus.BLOCKED: STATUS_TITLES[StatusState.WAITING],
    AgentStatus.WORKING: STATUS_TITLES[StatusState.WORKING],
    AgentStatus.IDLE: SESSIONS_IDLE_TITLE,
    AgentStatus.DONE: STATUS_TITLES[StatusState.FINISHED],
    AgentStatus.ERROR: STATUS_TITLES[StatusState.STOPPED],
}
# For a live agent hc reports no known status for: what pan recorded. The terminal is live,
# so never Stopped (as in status_message._STATE_FOR_LIVE_RECORD): a recorded failure reads as idle.
_SESSIONS_GROUP_FOR_PAN_STATUS: dict[WorkerStatus, str] = {
    WorkerStatus.SPAWNING: STATUS_TITLES[StatusState.WORKING],
    WorkerStatus.RUNNING: STATUS_TITLES[StatusState.WORKING],
    WorkerStatus.BLOCKED: STATUS_TITLES[StatusState.WAITING],
    WorkerStatus.IDLE: SESSIONS_IDLE_TITLE,
    WorkerStatus.DONE: STATUS_TITLES[StatusState.FINISHED],
    WorkerStatus.FAILED: SESSIONS_IDLE_TITLE,
}
# Before each session listed under a project heading. Em spaces, which Slack does not
# collapse, so the indent survives.
_PROJECT_MEMBER_INDENT = "\u2003\u2003"
# Emphasis characters that would garble text shown inside **bold** or _italic_.
_EMPHASIS_CHARACTERS = re.compile(r"[*~`]")
# An underscore not inside a word (`snake_case` is safe), which Slack can read as italics.
_LOOSE_UNDERSCORE = re.compile(r"(?<![^\W_])_|_(?![^\W_])")


def session_group(summary: SessionSummary) -> str | None:
    # The group a session is listed under; None for a terminal that is not an agent session
    # (project_map.is_agent_session: a shell, a dev server), unless it is a pan worker still
    # booting. A Claude session reports no status
    # until its first turn ends (see session_control._is_ready), so it and an unknown status
    # fall back to pan's record. With no record, a Claude session with no status is most
    # often one nobody has asked anything yet, so it counts as idle; an unknown status as
    # working.
    group = _SESSIONS_GROUP_FOR_AGENT.get(summary.agent_status)
    if group is not None:
        return group
    if not is_agent_session(summary.agent, summary.agent_status):
        booting = summary.pan_status is WorkerStatus.SPAWNING
        return STATUS_TITLES[StatusState.WORKING] if booting else None
    if summary.pan_status is not None:
        return _SESSIONS_GROUP_FOR_PAN_STATUS[summary.pan_status]
    if summary.agent_status is AgentStatus.NONE:
        return SESSIONS_IDLE_TITLE
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


def sessions_to_show(
    summaries: list[SessionSummary],
    orchestrator_name: str,
    addresses: list[str],
    *,
    max_count: int = SESSIONS_SLACK_MAX_COUNT,
) -> list[SessionSummary]:
    # The listed sessions at the given terminal addresses, in the order given, each once, at
    # most max_count. An address that is not a listed session is dropped. The list then
    # groups them by project, each project where its first one was (sessions_by_project).
    by_address = {
        summary.address: summary for summary in listed_sessions(summaries, orchestrator_name)
    }
    shown: list[SessionSummary] = []
    for address in addresses:
        summary = by_address.get(address)
        if summary is not None and summary not in shown:
            shown.append(summary)
    return shown[:max_count]


def _session_line(summary: SessionSummary, permalinks: dict[str, str]) -> str:
    line = f"• *{plain_mrkdwn(session_display_name(summary))}* · {session_group(summary)}"
    if summary.branch:
        # In code format so Slack reads nothing in the branch name as markup.
        line = f"{line} · `{escape_mrkdwn(summary.branch).replace('`', '')}`"
    link = permalinks.get(summary.thread_ts) if summary.thread_ts else None
    return f"{line} · <{link}|thread>" if link else line


def sessions_by_project(selected: list[SessionSummary]) -> list[list[SessionSummary]]:
    # The sessions grouped by project, each project where its first chosen session was, and
    # the chosen order kept inside each.
    groups: dict[str, list[SessionSummary]] = {}
    for summary in selected:
        groups.setdefault(summary.project_root, []).append(summary)
    return list(groups.values())


def _project_heading(group: list[SessionSummary]) -> str | None:
    # Only a project with an orchestrator gets a heading; a standalone session is listed on its
    # own line, so standalone sessions sharing a space label never repeat a heading.
    if all(summary.role is SessionRole.STANDALONE for summary in group):
        return None
    label = plain_mrkdwn(group[0].project) or group[0].project_root
    return f"*{label}*"


def render_sessions_for_slack(
    selected: list[SessionSummary], permalinks: dict[str, str], *, any_running: bool = True
) -> str:
    # `selected` comes from sessions_to_show. `permalinks` maps a session's thread_ts to its
    # thread's link; a session without one is listed without a link. A project's sessions are
    # listed together under its name, indented so a standalone session after them does not
    # read as one of them.
    if not selected:
        return SESSIONS_NONE_SHOWN_TEXT if any_running else SESSIONS_EMPTY_TEXT
    lines = [SESSIONS_HEADER]
    for group in sessions_by_project(selected):
        heading = _project_heading(group)
        if heading is None:
            lines.extend(_session_line(summary, permalinks) for summary in group)
            continue
        lines.append(heading)
        lines.extend(
            f"{_PROJECT_MEMBER_INDENT}{_session_line(summary, permalinks)}" for summary in group
        )
    return "\n".join(lines)


def screen_text(screen: str) -> str:
    # A session's cleaned screen lines (ScreenCaptured), labelled as what they are.
    return f"Screen text (no transcript yet):\n```\n{screen}\n```"


# The one-line notices pan posts in a thread. None carries markup of its own beyond the GFM
# slack_post converts, so they read the same in any thread.
UNTIED_THREAD_REPLY = "this thread isn't tied to a session; @pan sessions lists them"
NO_SESSION_REPLY = "This thread has no session."
NOT_SENT_REPLY = "Not sent."
EXPIRED_REPLY = "Not sent: that question expired. Send the message again."
# An action refused because the message it rests on (--item) cannot be used: unknown, not the
# sender's newest in the thread, or not from the requester. No session names or paths.
UNUSABLE_ITEM_REPLY = "I couldn't act on that message; please send the request again."
# How to start new work instead, offered when a `to` selector matches no session at all.
NEW_WORK_HINT = "@pan new: <your text>"

_THREAD_NOTICE_TEXTS: dict[ThreadNoticeReason, str] = {
    ThreadNoticeReason.THREAD_NOT_TIED: UNTIED_THREAD_REPLY,
    ThreadNoticeReason.NO_SESSION: NO_SESSION_REPLY,
    ThreadNoticeReason.QUESTION_EXPIRED: EXPIRED_REPLY,
    ThreadNoticeReason.NOT_SENT: NOT_SENT_REPLY,
    ThreadNoticeReason.MESSAGE_UNUSABLE: UNUSABLE_ITEM_REPLY,
}

# Each takes the session's name as {name}.
_SESSION_NOTICE_TEMPLATES: dict[SessionNoticeReason, str] = {
    SessionNoticeReason.DESK_SESSION_ENDED: (
        "{name} has ended, and pan does not reopen sessions it did not start; "
        "start one with @pan new: <task>, or send to another with @pan to <name>: <task>"
    ),
    SessionNoticeReason.THREAD_UNTIED: (
        "untied this thread from {name}; the session was not stopped"
    ),
    SessionNoticeReason.DESK_SESSION_UNTIED: (
        "Untied this thread from {name}; pan never closes sessions it didn't start."
    ),
    SessionNoticeReason.NOT_CLOSED_WORKING: (
        "{name} is still working; send `@pan done` when it finishes, "
        "or `@pan done --discard` to stop it now."
    ),
    SessionNoticeReason.NOT_CLOSED_WAITING_ON_PROMPT: (
        "{name} is waiting on a prompt; answer it at the terminal and send `@pan done` again, "
        "or `@pan done --discard` to stop it now."
    ),
    SessionNoticeReason.FOLDER_OUTSIDE_WORKTREES: (
        "{name}'s folder is not in pan's worktree folder; pan never removes it."
    ),
    SessionNoticeReason.FOLDER_IS_MAIN_REPOSITORY: (
        "{name}'s folder is a main repository; pan never removes it."
    ),
    SessionNoticeReason.WORKTREE_UNREADABLE: "Could not read {name}'s worktree; kept it.",
    SessionNoticeReason.PROPOSED_SESSION_GONE: "{name} is no longer running; not sent",
}

# The events slack_reporter posts as one plain line in the thread.
LineEvent = (
    QuestionAsked
    | ChoiceUnclear
    | ThreadNotice
    | SessionNotice
    | NotDelivered
    | StartFailed
    | NoSessionMatches
    | WorkKept
    | CleanupFailed
    | SessionClosed
)


def _numbers_phrase(count: int) -> str:
    numbers = [str(number) for number in range(1, count + 1)]
    return f"{', '.join(numbers[:-1])} or {numbers[-1]}"


def question_text(candidate_labels: list[str]) -> str:
    if len(candidate_labels) == 1:
        return f"Send this to {candidate_labels[0]}? Reply yes."
    numbered = "\n".join(
        f"{number}. {label}" for number, label in enumerate(candidate_labels, start=1)
    )
    return f"Which session? Reply {_numbers_phrase(len(candidate_labels))}:\n{numbered}"


def reask_text(candidate_count: int) -> str:
    if candidate_count == 1:
        return "Reply yes to send it, or anything else to cancel."
    return f"Reply {_numbers_phrase(candidate_count)} to choose, or anything else to cancel."


def not_delivered_text(refusal: NotDelivered) -> str:
    # A refused answer to pan's question says to send the same choice again later.
    retry_hint = (
        f"reply {refusal.retry_choice} again later, or anything else to cancel."
        if refusal.retry_choice
        else None
    )
    name = refusal.session_name
    if refusal.reason is NotDeliveredReason.BUSY:
        return f"{name} is busy; {retry_hint or 'try again, or @pan check'}"
    reply = f"{name} is waiting on a prompt at the terminal; nothing was sent"
    return f"{reply}; {retry_hint}" if retry_hint else reply


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def kept_reply(kept: WorkKept) -> str:
    unsaved = [
        _counted(count, noun)
        for count, noun in (
            (kept.uncommitted_files, "uncommitted file"),
            (kept.unpushed_commits, "unpushed commit"),
        )
        if count
    ]
    return (
        f"{kept.session_name} has {' and '.join(unsaved)}; kept it. "
        "Push or commit them, or send `@pan done --discard`."
    )


def closed_reply(closed: SessionClosed) -> str:
    # Says what this run removed and nothing more, so a retry after a half-finished cleanup
    # reports only the part it did.
    name = closed.session_name
    if closed.worktree_removed and closed.branch_deleted:
        return f"Closed {name} and removed its worktree and branch."
    if closed.worktree_removed:
        return f"Closed {name} and removed its worktree."
    if closed.branch_deleted:
        return f"Closed {name} and removed its branch; its worktree was already gone."
    if closed.repo_known:
        return f"Closed {name}; its worktree and branch were already gone."
    return f"Closed {name}; its worktree was already gone."


def line_text(event: LineEvent) -> str:  # noqa: C901 - one flat case per LineEvent type
    # The one line pan posts in the thread for `event`. One case per event type keeps the
    # match exhaustive (assert_never); splitting it would only move cases elsewhere.
    match event:
        case QuestionAsked():
            return question_text(event.candidate_labels)
        case ChoiceUnclear():
            return reask_text(event.candidate_count)
        case ThreadNotice():
            return _THREAD_NOTICE_TEXTS[event.reason]
        case SessionNotice():
            return _SESSION_NOTICE_TEMPLATES[event.reason].format(name=event.session_name)
        case NotDelivered():
            return not_delivered_text(event)
        case StartFailed():
            return start_failed_text(event)
        case NoSessionMatches():
            return (
                f'no session matches "{event.selector}"; to start new work send `{NEW_WORK_HINT}`'
            )
        case WorkKept():
            return kept_reply(event)
        case CleanupFailed():
            return (
                f"Closed {event.session_name}, but could not remove its worktree or branch; "
                f"send `@pan done` to try again, or remove {event.worktree_path} by hand."
            )
        case SessionClosed():
            return closed_reply(event)
        case _:
            assert_never(event)
