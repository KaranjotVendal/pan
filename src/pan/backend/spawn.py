from __future__ import annotations

import json
import re
from pathlib import Path

from pan.backend.directive import wants_slack_reply
from pan.backend.status_message import publish_status, send_private_details
from pan.core.errors import (
    NotPanSessionError,
    SpawnError,
    TerminalGoneError,
    TerminalHostError,
    TerminalNameTakenError,
)
from pan.core.events import SessionNotice, SessionNoticeReason, StartAction, StartFailed
from pan.core.models import (
    Conversation,
    ReplyMode,
    StatusState,
    Terminal,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.core.seams import (
    AgentLauncher,
    Clock,
    GitWorktreeAdapter,
    IdGen,
    Reporter,
    TerminalHost,
    ThreadMap,
)
from pan.utils.config import (
    HERDR_SPACE_LABEL,
    INBOX_THREAD_TASK_MAX_LENGTH,
    PAN_CHANNEL_LABEL,
    PAN_ROLE_LABEL,
    PAN_THREAD_LABEL,
    PLANNOTATOR_INBOX_TOOL_NAME,
    REPL_TOOL_NAME,
    SLACK_READ_THREAD_TOOL_NAME,
    TERMINAL_NAME_MAX_LENGTH,
    WORKER_BRIEF_TASK_MARKER,
    WORKER_NAME_PREFIX,
    WORKER_ROLE,
    WORKER_SETTINGS_RELATIVE_PATH,
)
from pan.utils.logging import initialise_logger
from pan.utils.text import status_label

logger = initialise_logger(__name__)

# The Claude Code hooks settings written into each worker's worktree. Stop and Notification
# mark the thread idle / blocked (and post to Slack only in the reply-in-Slack mode);
# SessionStart records the Claude session id and transcript path on the thread record (for
# `pan read --full`); PostToolUse on the Inbox tool (or on code mode's REPL tool, which wraps
# Inbox calls) records the session's Inbox thread id and link. The matcher is an anchored
# regex alternation of exactly those tool names. Non-tool events take a list of
# {hooks: [{type, command}]} objects (matcher omitted). The `pan hook` commands resolve the
# thread from the worker's cwd via the thread map.
_WORKER_SETTINGS = {
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "pan hook session-start"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "pan hook stop"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "pan hook notification"}]}],
        "PostToolUse": [
            {
                "matcher": f"^({PLANNOTATOR_INBOX_TOOL_NAME}|{REPL_TOOL_NAME})$",
                "hooks": [{"type": "command", "command": "pan hook inbox-link"}],
            }
        ],
    }
}

# Put before every new worker's task. The session owns its Inbox thread (so the person's
# Inbox replies wake it), which is why the session creates it, under the name pan chose.
_WORKER_BRIEF_PREAMBLE = (
    "pan started you from a Slack request. Use the Plannotator Inbox for questions, decisions "
    'and progress, in a thread named exactly "{inbox_thread_name}". Send a first short message '
    "there before you start the work. {slack_reply_sentence} {relayed_follow_up_sentence}"
)
# Follow-ups from Slack are typed into the session from the orchestrator's terminal, so hc
# labels them as coming from it; without this a session took them for another agent and
# answered with `hc send`.
RELAYED_FOLLOW_UP_SENTENCE = (
    "Messages that arrive from pan-orchestrator are the person's follow-ups relayed from Slack: "
    "answer them as usual, and never reply with `hc send`."
)
# Tells a session started or messaged from Slack who replies in the Slack thread. pan's Stop
# hook posts the final answer itself in the reply-in-Slack mode; otherwise Slack gets a reply
# only when the person asks for one, and pan posts that too.
_SLACK_REPLY_SENTENCES: dict[ReplyMode, str] = {
    ReplyMode.SLACK: (
        "Do not post to Slack yourself: pan posts your final answer to this Slack thread for you."
    ),
    ReplyMode.ON_DEMAND: (
        "Do not post to Slack yourself: the Slack thread gets a reply only when the person asks "
        "for one, and pan posts it for you."
    ),
}

# Added to the brief of a new session started from a reply inside a Slack thread, so the
# session reads the earlier messages for context. pan does not fetch the thread: the session
# reads it with its own Slack tool, only to read.
_SLACK_THREAD_PARAGRAPH = (
    "This request came from a reply in the Slack thread in channel {channel} with thread ts "
    "{thread_ts}. Before you start, read the whole thread (every message, following pagination "
    "until there are no more) with the Slack read-thread tool ({tool_name}). Use it only to "
    "read; do not post to Slack. If the tool is unavailable, say so in the Inbox thread and "
    "continue with the task text."
)

_NAME_DISALLOWED = re.compile(r"[^a-z0-9_-]+")


def _write_worker_settings(worktree_path: Path) -> None:
    settings_path = worktree_path / WORKER_SETTINGS_RELATIVE_PATH
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(_WORKER_SETTINGS, indent=2))


def worker_terminal_name(stream: str | None, id_gen: IdGen) -> str:
    # `pan-<stream>` normalized to the terminal host's name rules (hc: 1-32 of a-z 0-9 - _);
    # the same string is the git branch. A stream with nothing usable left falls back to a fresh id.
    slug = _NAME_DISALLOWED.sub("-", stream.lower()).strip("-") if stream else ""
    if not slug:
        slug = id_gen.new_id()[:8]
    slug = slug[: TERMINAL_NAME_MAX_LENGTH - len(WORKER_NAME_PREFIX)].rstrip("-")
    return f"{WORKER_NAME_PREFIX}{slug}"


def inbox_thread_name(terminal_name: str, task: str) -> str:
    # `<terminal name>: <short task>` on one line: whitespace collapsed, the task cut short.
    short_task = " ".join(task.split())
    if len(short_task) > INBOX_THREAD_TASK_MAX_LENGTH:
        short_task = short_task[: INBOX_THREAD_TASK_MAX_LENGTH - 3].rstrip() + "..."
    return f"{terminal_name}: {short_task}" if short_task else terminal_name


def slack_reply_sentence(reply_mode: ReplyMode) -> str:
    return _SLACK_REPLY_SENTENCES[reply_mode]


def slack_thread_paragraph(channel: str, thread_ts: str) -> str:
    return _SLACK_THREAD_PARAGRAPH.format(
        channel=channel, thread_ts=thread_ts, tool_name=SLACK_READ_THREAD_TOOL_NAME
    )


def worker_brief(
    inbox_thread: str,
    task: str,
    reply_mode: ReplyMode,
    *,
    thread_paragraph: str | None = None,
) -> str:
    # `thread_paragraph` (slack_thread_paragraph) goes after the preamble, before the task.
    preamble = _WORKER_BRIEF_PREAMBLE.format(
        inbox_thread_name=inbox_thread,
        slack_reply_sentence=slack_reply_sentence(reply_mode),
        relayed_follow_up_sentence=RELAYED_FOLLOW_UP_SENTENCE,
    )
    if thread_paragraph is not None:
        preamble = f"{preamble}\n\n{thread_paragraph}"
    return f"{preamble}{WORKER_BRIEF_TASK_MARKER}{task}"


def start_failed_text(failure: StartFailed) -> str:
    # The one wording of a failed spawn or reopen: the SpawnError message, and the line the
    # reporter posts in the thread.
    notice = f"{failure.action.value} failed for stream {failure.session_name}"
    return f"{notice}: stream name in use" if failure.name_in_use else notice


def worker_labels(conversation: Conversation, app_space: str | None) -> dict[str, str]:
    labels = {PAN_ROLE_LABEL: WORKER_ROLE, PAN_THREAD_LABEL: conversation.id}
    if conversation.place is not None:
        labels[PAN_CHANNEL_LABEL] = conversation.place
    if app_space is not None:
        labels[HERDR_SPACE_LABEL] = app_space
    return labels


class ClaudeLauncher:
    def __init__(self, terminal_host: TerminalHost) -> None:
        self._terminal_host = terminal_host

    def launch(
        self,
        name: str,
        worktree: Path,
        brief: str,
        labels: dict[str, str],
        *,
        continue_session: bool = False,
    ) -> Terminal:
        # Claude runs as the terminal's program, with the brief as one argv element: no
        # shell sits in between, so the brief is never parsed as shell text. The brief is
        # the first task in the run itself; a send to a booting agent can be dropped.
        # continue_session resumes the worktree's most recent conversation (reopen).
        argv = ["claude", "--continue", brief] if continue_session else ["claude", brief]
        return self._terminal_host.run(name=name, cwd=worktree, argv=argv, labels=labels)


def _ensure_name_free(terminal_host: TerminalHost, name: str) -> None:
    # Fail before creating a worktree when a live terminal already holds the name, so a
    # reused `--stream` never half-spawns. The host's own name check still guards the race.
    try:
        existing = terminal_host.get(name)
    except TerminalGoneError:
        return
    if existing.state is TerminalState.LIVE:
        raise TerminalNameTakenError(f"a live terminal is already named {name}")


def spawn_worker(
    *,
    conversation: Conversation,
    task: str,
    repo: Path,
    base: Path,
    stream: str | None,
    app_space: str | None,
    git: GitWorktreeAdapter,
    terminal_host: TerminalHost,
    launcher: AgentLauncher,
    thread_map: ThreadMap,
    reporter: Reporter,
    clock: Clock,
    id_gen: IdGen,
    requester: str | None = None,
    request_text: str | None = None,
    reply_in_slack: bool = False,
    from_thread_reply: bool = False,
) -> ThreadRecord:
    # `request_text` is the original message; it asks for Slack replies when it contains
    # "reply in slack" (falls back to the task when absent). `reply_in_slack` forces it.
    # `from_thread_reply` marks a request that was a reply inside a Slack thread: the brief
    # then tells the session to read that thread first.
    name = worker_terminal_name(stream, id_gen)
    created_at = clock.now()
    inbox_thread = inbox_thread_name(name, task)
    asks_for_slack_reply = reply_in_slack or wants_slack_reply(
        request_text if request_text is not None else task
    )
    reply_mode = ReplyMode.SLACK if asks_for_slack_reply else ReplyMode.ON_DEMAND
    thread_paragraph = (
        slack_thread_paragraph(conversation.slack_channel(), conversation.id)
        if from_thread_reply
        else None
    )

    try:
        _ensure_name_free(terminal_host, name)
        worktree_path = git.create_worktree(repo, name, base)
        # Register the worker's hooks before it launches, so its SessionStart / Stop /
        # Notification events reach the thread (resolved by cwd).
        _write_worker_settings(worktree_path)
        terminal = launcher.launch(
            name,
            worktree_path,
            worker_brief(inbox_thread, task, reply_mode, thread_paragraph=thread_paragraph),
            worker_labels(conversation, app_space),
        )
    except (SpawnError, TerminalHostError) as error:
        # Record the failed attempt so the thread map still reflects it (the worktree may
        # not exist, so record its intended path) and surface it. No session started, so the
        # thread stays untied: a later plain request starts a new session.
        name_in_use = isinstance(error, TerminalNameTakenError)
        failed_record = ThreadRecord(
            conversation=conversation,
            terminal_name=name,
            worktree_path=base / name,
            repo_path=repo,
            status=WorkerStatus.FAILED,
            created_at=created_at,
            updated_at=clock.now(),
            tied=False,
            requester=requester,
            reply_mode=reply_mode,
        )
        thread_map.put(failed_record)
        failure = StartFailed(action=StartAction.SPAWN, session_name=name, name_in_use=name_in_use)
        notice = start_failed_text(failure)
        reporter.report(conversation, failure)
        logger.info(f"spawn failed stream={name} conversation={conversation.key}")
        raise SpawnError(notice) from error

    record = ThreadRecord(
        conversation=conversation,
        terminal_name=name,
        terminal_address=terminal.address,
        worktree_path=worktree_path,
        repo_path=repo,
        status=WorkerStatus.SPAWNING,
        created_at=created_at,
        updated_at=created_at,
        tied=True,
        started_by_pan=True,
        requester=requester,
        reply_mode=reply_mode,
        inbox_thread_name=inbox_thread,
        status_label=status_label(task),
        task_started_at=created_at,
    )
    thread_map.tie(record)
    # The thread's status message (Working), and the Inbox thread and terminal only to the
    # requester: they are on this machine and mean nothing to anyone else in the channel.
    record = publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=record,
        state=StatusState.WORKING,
        clock=clock,
    )
    send_private_details(reporter=reporter, record=record)
    logger.info(f"spawn stream={name} terminal={terminal.address} conversation={conversation.key}")
    return record


def upgraded_reply_mode(record: ThreadRecord, *, reply_in_slack: bool) -> ReplyMode:
    # The thread's reply mode once a follow-up or reopen is delivered: the reply-in-Slack mode
    # when asked, else the record's own. Never back to on demand. Callers save it only after
    # the delivery succeeds, so a refused or failed one leaves the record as it was.
    return ReplyMode.SLACK if reply_in_slack else record.reply_mode


def reopen_worker(
    *,
    record: ThreadRecord,
    task: str,
    app_space: str | None,
    terminal_host: TerminalHost,
    launcher: AgentLauncher,
    thread_map: ThreadMap,
    reporter: Reporter,
    clock: Clock,
    reply_in_slack: bool = False,
) -> ThreadRecord:
    # Reopen a finished or lost worker in its existing worktree: a new terminal under the
    # same name continues the worktree's last Claude conversation with the follow-up task.
    # The binding stays the same thread record (INV-7), tied again; only its terminal, status
    # and task change, and the thread's status message is edited back to Working. A session pan did not start is never reopened: its folder is not pan's.
    # `reply_in_slack` switches the thread to the reply-in-Slack mode, saved with the tie once
    # the launch succeeds, so the Stop hook posts the answer; without it the mode is left as
    # it is, and a failed reopen changes nothing.
    name = record.terminal_name
    if not record.started_by_pan:
        reporter.report(
            record.conversation,
            SessionNotice(reason=SessionNoticeReason.DESK_SESSION_ENDED, session_name=name),
        )
        raise NotPanSessionError(f"{name} was not started by pan; not reopened")
    reply_mode = upgraded_reply_mode(record, reply_in_slack=reply_in_slack)
    try:
        if not record.worktree_path.is_dir():
            raise SpawnError(f"worktree missing for stream {name}")
        _ensure_name_free(terminal_host, name)
        terminal = launcher.launch(
            name,
            record.worktree_path,
            task,
            worker_labels(record.conversation, app_space),
            continue_session=True,
        )
    except (SpawnError, TerminalHostError) as error:
        name_in_use = isinstance(error, TerminalNameTakenError)
        failure = StartFailed(action=StartAction.REOPEN, session_name=name, name_in_use=name_in_use)
        notice = start_failed_text(failure)
        reporter.report(record.conversation, failure)
        logger.info(f"reopen failed stream={name} conversation={record.conversation.key}")
        raise SpawnError(notice) from error

    now = clock.now()
    reopened = record.model_copy(
        update={
            "terminal_address": terminal.address,
            "status": WorkerStatus.SPAWNING,
            "updated_at": now,
            "reply_mode": reply_mode,
            "status_label": status_label(task),
            "task_started_at": now,
        }
    )
    thread_map.tie(reopened)
    reopened = publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=reopened,
        state=StatusState.WORKING,
        clock=clock,
    )
    logger.info(
        f"reopen stream={name} terminal={terminal.address} conversation={record.conversation.key}"
    )
    return reopened
