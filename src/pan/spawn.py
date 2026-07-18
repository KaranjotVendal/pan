from __future__ import annotations

import json
import shlex
from pathlib import Path

from pan.errors import HerdrError, SpawnError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import ThreadRecord, WorkerStatus
from pan.seams import (
    AgentLauncher,
    Clock,
    GitWorktreeAdapter,
    HerdrAdapter,
    IdGen,
    SlackAdapter,
    ThreadMap,
)

logger = initialise_logger(__name__)

# The Claude Code hooks settings written into each worker's worktree so its Stop and
# Notification events auto-reply to the originating Slack thread. Non-tool events take a
# list of {hooks: [{type, command}]} objects (matcher omitted). `pan hook stop` /
# `pan hook notification` resolve the thread from the worker's cwd via the thread map.
_WORKER_SETTINGS = {
    "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "pan hook stop"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "pan hook notification"}]}],
    }
}


def _write_worker_settings(worktree_path: Path) -> None:
    settings_dir = worktree_path / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    (settings_dir / "settings.json").write_text(json.dumps(_WORKER_SETTINGS, indent=2))


class ClaudeLauncher:
    def __init__(self, herdr: HerdrAdapter) -> None:
        self._herdr = herdr

    def launch(self, worktree: Path, pane_id: str, brief: str) -> None:
        # The worktree pane already runs a shell in the worktree cwd; type the claude
        # launch command (brief shell-quoted so it can't be interpreted) then submit it
        # with the fixed Enter nudge.
        command = f"claude {shlex.quote(brief)}"
        self._herdr.send_text(pane_id, command)
        self._herdr.nudge(pane_id)


def spawn_worker(
    *,
    thread_ts: str,
    channel: str,
    task: str,
    repo: Path,
    base: Path,
    stream: str | None,
    git: GitWorktreeAdapter,
    herdr: HerdrAdapter,
    launcher: AgentLauncher,
    thread_map: ThreadMap,
    slack: SlackAdapter,
    clock: Clock,
    id_gen: IdGen,
) -> ThreadRecord:
    label = f"pan-{stream}" if stream else f"pan-{id_gen.new_id()[:8]}"
    created_at = clock.now()

    try:
        worktree_path = git.create_worktree(repo, label, base)
        # Register the worker's completion hooks before it launches, so its Stop /
        # Notification events auto-reply to the Slack thread (resolved by cwd).
        _write_worker_settings(worktree_path)
        workspace_id, pane_id = herdr.create_workspace(label, worktree_path)
        launcher.launch(worktree_path, pane_id, task)
    except (SpawnError, HerdrError) as error:
        # Record the failed attempt so the thread map still reflects the binding
        # (the worktree may not exist, so record its intended path) and surface it.
        failed_record = ThreadRecord(
            thread_ts=thread_ts,
            workspace_name=label,
            workspace_id="",
            channel=channel,
            worktree_path=base / label,
            status=WorkerStatus.FAILED,
            created_at=created_at,
            updated_at=clock.now(),
        )
        thread_map.put(failed_record)
        slack_post(slack, channel, thread_ts, f"spawn failed for stream {label}")
        logger.info(f"spawn failed stream={label} thread={thread_ts}")
        raise SpawnError(f"spawn failed for stream {label}") from error

    record = ThreadRecord(
        thread_ts=thread_ts,
        workspace_name=label,
        workspace_id=workspace_id,
        channel=channel,
        pane_ids=[pane_id],
        worktree_path=worktree_path,
        status=WorkerStatus.SPAWNING,
        created_at=created_at,
        updated_at=created_at,
    )
    thread_map.put(record)
    slack_post(slack, channel, thread_ts, f"on it — stream {label}")
    logger.info(f"spawn stream={label} thread={thread_ts}")
    return record
