from __future__ import annotations

import json
import re
from pathlib import Path

from pan.config import (
    HERDR_SPACE_LABEL,
    PAN_CHANNEL_LABEL,
    PAN_ROLE_LABEL,
    PAN_THREAD_LABEL,
    TERMINAL_NAME_MAX_LENGTH,
    WORKER_NAME_PREFIX,
    WORKER_ROLE,
)
from pan.errors import HcError, SpawnError, TerminalGoneError, TerminalNameTakenError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import Terminal, TerminalState, ThreadRecord, WorkerStatus
from pan.seams import (
    AgentLauncher,
    Clock,
    GitWorktreeAdapter,
    HcAdapter,
    IdGen,
    SlackAdapter,
    ThreadMap,
)

logger = initialise_logger(__name__)

# The Claude Code hooks settings written into each worker's worktree. Stop and Notification
# auto-reply to the originating Slack thread; SessionStart records the Claude session id and
# transcript path on the thread record (for `pan read --full`). Non-tool events take a list
# of {hooks: [{type, command}]} objects (matcher omitted). The `pan hook` commands resolve
# the thread from the worker's cwd via the thread map.
_WORKER_SETTINGS = {
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "pan hook session-start"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "pan hook stop"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "pan hook notification"}]}],
    }
}

_NAME_DISALLOWED = re.compile(r"[^a-z0-9_-]+")


def _write_worker_settings(worktree_path: Path) -> None:
    settings_dir = worktree_path / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(json.dumps(_WORKER_SETTINGS, indent=2))


def worker_terminal_name(stream: str | None, id_gen: IdGen) -> str:
    # `pan-<stream>` normalized to hc's name rules (1-32 of a-z 0-9 - _); the same string
    # is the git branch. A stream with nothing usable left falls back to a fresh id.
    slug = _NAME_DISALLOWED.sub("-", stream.lower()).strip("-") if stream else ""
    if not slug:
        slug = id_gen.new_id()[:8]
    slug = slug[: TERMINAL_NAME_MAX_LENGTH - len(WORKER_NAME_PREFIX)].rstrip("-")
    return f"{WORKER_NAME_PREFIX}{slug}"


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
    def __init__(self, hc: HcAdapter) -> None:
        self._hc = hc

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
        return self._hc.run(name=name, cwd=worktree, argv=argv, labels=labels)


def _ensure_name_free(hc: HcAdapter, name: str) -> None:
    # Fail before creating a worktree when a live terminal already holds the name, so a
    # reused `--stream` never half-spawns. hc's own name_taken check still guards the race.
    try:
        existing = hc.get(name)
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
    hc: HcAdapter,
    launcher: AgentLauncher,
    thread_map: ThreadMap,
    slack: SlackAdapter,
    clock: Clock,
    id_gen: IdGen,
) -> ThreadRecord:
    name = worker_terminal_name(stream, id_gen)
    created_at = clock.now()

    try:
        _ensure_name_free(hc, name)
        worktree_path = git.create_worktree(repo, name, base)
        # Register the worker's hooks before it launches, so its SessionStart / Stop /
        # Notification events reach the thread (resolved by cwd).
        _write_worker_settings(worktree_path)
        terminal = launcher.launch(
            name, worktree_path, task, worker_labels(thread_ts, channel, app_space)
        )
    except (SpawnError, HcError) as error:
        # Record the failed attempt so the thread map still reflects the binding
        # (the worktree may not exist, so record its intended path) and surface it.
        reason = "stream name in use" if isinstance(error, TerminalNameTakenError) else None
        failed_record = ThreadRecord(
            thread_ts=thread_ts,
            terminal_name=name,
            channel=channel,
            worktree_path=base / name,
            status=WorkerStatus.FAILED,
            created_at=created_at,
            updated_at=clock.now(),
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
        status=WorkerStatus.SPAWNING,
        created_at=created_at,
        updated_at=created_at,
    )
    thread_map.put(record)
    slack_post(slack, channel, thread_ts, f"on it — stream {name}")
    logger.info(f"spawn stream={name} terminal={terminal.address} thread={thread_ts}")
    return record


def reopen_worker(
    *,
    record: ThreadRecord,
    task: str,
    app_space: str | None,
    hc: HcAdapter,
    launcher: AgentLauncher,
    thread_map: ThreadMap,
    slack: SlackAdapter,
    clock: Clock,
) -> ThreadRecord:
    # Reopen a finished or lost worker in its existing worktree: a new terminal under the
    # same name continues the worktree's last Claude conversation with the follow-up task.
    # The binding stays the same thread record (INV-7); only its terminal and status change.
    name = record.terminal_name
    try:
        if not record.worktree_path.is_dir():
            raise SpawnError(f"worktree missing for stream {name}")
        _ensure_name_free(hc, name)
        terminal = launcher.launch(
            name,
            record.worktree_path,
            task,
            worker_labels(record.thread_ts, record.channel, app_space),
            continue_session=True,
        )
    except (SpawnError, HcError) as error:
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
    thread_map.put(reopened)
    slack_post(slack, record.channel, record.thread_ts, f"reopened — stream {name}")
    logger.info(f"reopen stream={name} terminal={terminal.address} thread={record.thread_ts}")
    return reopened
