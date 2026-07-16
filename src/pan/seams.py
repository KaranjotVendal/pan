from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from pan.models import InboxItem, ThreadRecord, WorkerStatus

# Every seam Protocol lives here for a single import point.


class SlackAdapter(Protocol):
    def add_reaction(self, channel: str, ts: str, name: str) -> None: ...

    def post_message(self, channel: str, thread_ts: str, text: str) -> None: ...

    def start(self) -> None: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGen(Protocol):
    def new_id(self) -> str: ...


class InboxStore(Protocol):
    def append(self, item: InboxItem) -> None: ...

    def drain(self) -> list[InboxItem]: ...


class ThreadMap(Protocol):
    def get(self, thread_ts: str) -> ThreadRecord | None: ...

    def put(self, record: ThreadRecord) -> None: ...

    def update_status(self, thread_ts: str, status: WorkerStatus) -> None: ...


class HerdrAdapter(Protocol):
    def create_workspace(self, label: str, cwd: Path) -> tuple[str, str]: ...

    def nudge(self, pane_id: str) -> None: ...

    def send_text(self, pane_id: str, text: str) -> None: ...

    def kill_pane(self, pane_id: str) -> None: ...


class GitWorktreeAdapter(Protocol):
    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path: ...

    def remove_worktree(self, path: Path) -> None: ...


class MorcliAdapter(Protocol):
    def session_status(self, handle: str) -> WorkerStatus: ...


class AgentLauncher(Protocol):
    # Starts a worker agent session in the given pane with the task brief. The
    # orchestrator (spawn_worker) owns ThreadRecord construction, since the record's
    # required thread_ts/channel context is not available to the launcher.
    def launch(self, worktree: Path, pane_id: str, brief: str) -> None: ...


class InboxWatcher(Protocol):
    def on_inbox_changed(self) -> None: ...

    def start(self) -> None: ...
