from __future__ import annotations

import json
import re
from pathlib import Path

from pan.config import (
    HERDR_SPACE_LABEL,
    INBOX_THREAD_TASK_MAX_LENGTH,
    PAN_CHANNEL_LABEL,
    PAN_ROLE_LABEL,
    PAN_THREAD_LABEL,
    PLANNOTATOR_INBOX_TOOL_NAME,
    REPL_TOOL_NAME,
    TERMINAL_NAME_MAX_LENGTH,
    WORKER_BRIEF_TASK_MARKER,
    WORKER_NAME_PREFIX,
    WORKER_ROLE,
    WORKER_SETTINGS_RELATIVE_PATH,
)
from pan.directive import wants_slack_reply
from pan.errors import (
    NotPanSessionError,
    SpawnError,
    TerminalGoneError,
    TerminalHostError,
    TerminalNameTakenError,
)
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import (
    ReplyMode,
    Terminal,
    TerminalState,
    ThreadRecord,
    ThreadSource,
    WorkerStatus,
)
from pan.seams import (
    AgentLauncher,
    Clock,
    GitWorktreeAdapter,
    IdGen,
    SlackAdapter,
    TerminalHost,
    ThreadMap,
)

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
    "there before you start the work. {slack_reply_sentence}"
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


def worker_brief(inbox_thread: str, task: str, reply_mode: ReplyMode) -> str:
    preamble = _WORKER_BRIEF_PREAMBLE.format(
        inbox_thread_name=inbox_thread, slack_reply_sentence=slack_reply_sentence(reply_mode)
    )
    return f"{preamble}{WORKER_BRIEF_TASK_MARKER}{task}"


def start_notice(terminal_name: str, inbox_thread: str, reply_mode: ReplyMode) -> str:
    notice = f'Started {terminal_name} in a new worktree · Inbox thread: "{inbox_thread}"'
    if reply_mode is ReplyMode.SLACK:
        notice = f"{notice} · answers will also be posted here"
    return notice


def desk_session_ended_reply(name: str) -> str:
    return (
        f"{name} has ended, and pan does not reopen sessions it did not start; "
        "start one with @pan new: <task>, or send to another with @pan to <name>: <task>"
    )


def worker_labels(thread_ts: str, channel: str, app_space: str | None) -> dict[str, str]:
    labels = {
        PAN_ROLE_LABEL: WORKER_ROLE,
        PAN_THREAD_LABEL: thread_ts,
        PAN_CHANNEL_LABEL: channel,
    }
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
    thread_ts: str,
    channel: str,
    task: str,
    repo: Path,
    base: Path,
    stream: str | None,
    app_space: str | None,
    git: GitWorktreeAdapter,
    terminal_host: TerminalHost,
    launcher: AgentLauncher,
    thread_map: ThreadMap,
    slack: SlackAdapter,
    clock: Clock,
    id_gen: IdGen,
    requester: str | None = None,
    request_text: str | None = None,
    reply_in_slack: bool = False,
) -> ThreadRecord:
    # `request_text` is the original message; it asks for Slack replies when it contains
    # "reply in slack" (falls back to the task when absent). `reply_in_slack` forces it.
    name = worker_terminal_name(stream, id_gen)
    created_at = clock.now()
    inbox_thread = inbox_thread_name(name, task)
    asks_for_slack_reply = reply_in_slack or wants_slack_reply(
        request_text if request_text is not None else task
    )
    reply_mode = ReplyMode.SLACK if asks_for_slack_reply else ReplyMode.ON_DEMAND

    try:
        _ensure_name_free(terminal_host, name)
        worktree_path = git.create_worktree(repo, name, base)
        # Register the worker's hooks before it launches, so its SessionStart / Stop /
        # Notification events reach the thread (resolved by cwd).
        _write_worker_settings(worktree_path)
        terminal = launcher.launch(
            name,
            worktree_path,
            worker_brief(inbox_thread, task, reply_mode),
            worker_labels(thread_ts, channel, app_space),
        )
    except (SpawnError, TerminalHostError) as error:
        # Record the failed attempt so the thread map still reflects it (the worktree may
        # not exist, so record its intended path) and surface it. No session started, so the
        # thread stays untied: a later plain request starts a new session.
        reason = "stream name in use" if isinstance(error, TerminalNameTakenError) else None
        failed_record = ThreadRecord(
            thread_ts=thread_ts,
            terminal_name=name,
            channel=channel,
            worktree_path=base / name,
            repo_path=repo,
            status=WorkerStatus.FAILED,
            created_at=created_at,
            updated_at=clock.now(),
            source=ThreadSource.SLACK,
            tied=False,
            requester=requester,
            reply_mode=reply_mode,
        )
        thread_map.put(failed_record)
        notice = f"spawn failed for stream {name}"
        if reason is not None:
            notice = f"{notice}: {reason}"
        slack_post(slack, channel, thread_ts, notice)
        logger.info(f"spawn failed stream={name} thread={thread_ts}")
        raise SpawnError(notice) from error

    record = ThreadRecord(
        thread_ts=thread_ts,
        terminal_name=name,
        terminal_address=terminal.address,
        channel=channel,
        worktree_path=worktree_path,
        repo_path=repo,
        status=WorkerStatus.SPAWNING,
        created_at=created_at,
        updated_at=created_at,
        source=ThreadSource.SLACK,
        tied=True,
        started_by_pan=True,
        requester=requester,
        reply_mode=reply_mode,
        inbox_thread_name=inbox_thread,
    )
    thread_map.tie(record)
    slack_post(slack, channel, thread_ts, start_notice(name, inbox_thread, reply_mode))
    logger.info(f"spawn stream={name} terminal={terminal.address} thread={thread_ts}")
    return record


def reopen_worker(
    *,
    record: ThreadRecord,
    task: str,
    app_space: str | None,
    terminal_host: TerminalHost,
    launcher: AgentLauncher,
    thread_map: ThreadMap,
    slack: SlackAdapter,
    clock: Clock,
) -> ThreadRecord:
    # Reopen a finished or lost worker in its existing worktree: a new terminal under the
    # same name continues the worktree's last Claude conversation with the follow-up task.
    # The binding stays the same thread record (INV-7), tied again; only its terminal and
    # status change. A session pan did not start is never reopened: its folder is not pan's.
    name = record.terminal_name
    if not record.started_by_pan:
        slack_post(slack, record.channel, record.thread_ts, desk_session_ended_reply(name))
        raise NotPanSessionError(f"{name} was not started by pan; not reopened")
    try:
        if not record.worktree_path.is_dir():
            raise SpawnError(f"worktree missing for stream {name}")
        _ensure_name_free(terminal_host, name)
        terminal = launcher.launch(
            name,
            record.worktree_path,
            task,
            worker_labels(record.thread_ts, record.channel, app_space),
            continue_session=True,
        )
    except (SpawnError, TerminalHostError) as error:
        notice = f"reopen failed for stream {name}"
        if isinstance(error, TerminalNameTakenError):
            notice = f"{notice}: stream name in use"
        slack_post(slack, record.channel, record.thread_ts, notice)
        logger.info(f"reopen failed stream={name} thread={record.thread_ts}")
        raise SpawnError(notice) from error

    reopened = record.model_copy(
        update={
            "terminal_address": terminal.address,
            "status": WorkerStatus.SPAWNING,
            "updated_at": clock.now(),
        }
    )
    thread_map.tie(reopened)
    slack_post(slack, record.channel, record.thread_ts, f"reopened — stream {name}")
    logger.info(f"reopen stream={name} terminal={terminal.address} thread={record.thread_ts}")
    return reopened
