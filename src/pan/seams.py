from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from pan.models import InboxItem, ThreadRecord, WorkerStatus

# The remaining seam Protocols (SlackAdapter, MorcliAdapter, AgentLauncher, InboxWatcher) land
# here as their implementing tasks arrive, so every seam has a single import point.


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
