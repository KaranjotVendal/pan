from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from pan.models import (
    InboxItem,
    InboxThread,
    Terminal,
    TerminalRead,
    ThreadRecord,
    WorkerStatus,
)

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
    # Records are kept per (thread, session). A thread lookup returns the thread's tied
    # record, else its newest; `session_name` (a terminal name) picks that session's record
    # in the thread instead, which is how a session's own hooks reach it after it was untied.
    def get(self, thread_ts: str, *, session_name: str | None = None) -> ThreadRecord | None: ...

    def get_by_worktree(self, worktree_path: Path) -> ThreadRecord | None: ...

    def put(self, record: ThreadRecord) -> None: ...

    # Store the record as its thread's tie, untying any other thread tied to the same session
    # and the thread's previous session.
    def tie(self, record: ThreadRecord) -> None: ...

    def untie(self, thread_ts: str) -> None: ...

    def record_inbox_thread(
        self,
        thread_ts: str,
        inbox_thread_id: str,
        url: str | None,
        *,
        session_name: str | None = None,
    ) -> None: ...

    def update_status(
        self, thread_ts: str, status: WorkerStatus, *, session_name: str | None = None
    ) -> None: ...

    def records(self) -> list[ThreadRecord]: ...

    def record_session(
        self,
        thread_ts: str,
        session_id: str,
        transcript_path: Path | None,
        *,
        session_name: str | None = None,
    ) -> None: ...


class TerminalHost(Protocol):
    # The terminal multiplexer pan runs agents in (hc today; see TerminalHostKind). Only the
    # host's adapter in src/pan/adapters/ talks to the host itself. `terminal` arguments
    # accept any address form the host reports, or a terminal name. Failures raise
    # TerminalHostError or one of its typed kinds.
    #
    # `labels` are an optional hint a host may store and filter on (hc does); a host without
    # labels ignores them. Ownership ("pan started this") never depends on labels: it comes
    # from pan's thread map (see sessions.collect_sessions).
    def run(self, *, name: str, cwd: Path, argv: list[str], labels: dict[str, str]) -> Terminal: ...

    # Paste text and press Enter; raises TargetBlockedError when the target is blocked.
    def send(self, terminal: str, text: str) -> None: ...

    def read(self, terminal: str, lines: int) -> TerminalRead: ...

    # Raises TerminalGoneError when the terminal does not exist.
    def get(self, terminal: str) -> Terminal: ...

    def list_terminals(self, labels: dict[str, str] | None = None) -> list[Terminal]: ...

    # Kill then remove; a terminal that is already gone counts as removed.
    def remove(self, terminal: str) -> None: ...


class InboxDirectory(Protocol):
    # Read-only lookup of Plannotator Inbox threads, for sessions pan has no hooks in (desk
    # sessions). Never sends anything. Returns None when no open thread of that name is found
    # in the project, or when the Inbox cannot be read.
    def find_thread(self, project_path: Path, thread_name: str) -> InboxThread | None: ...


class GitWorktreeAdapter(Protocol):
    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path: ...

    def remove_worktree(self, path: Path) -> None: ...


class AgentLauncher(Protocol):
    # Starts a worker agent in a new terminal named `name`, in the worktree, with the task
    # brief as its first task; continue_session resumes the worktree's last conversation.
    # The orchestrator (spawn_worker) owns ThreadRecord construction, since the record's
    # thread_ts/channel context is not the launcher's.
    def launch(
        self,
        name: str,
        worktree: Path,
        brief: str,
        labels: dict[str, str],
        *,
        continue_session: bool = False,
    ) -> Terminal: ...


class InboxWatcher(Protocol):
    def on_inbox_changed(self) -> None: ...

    def start(self) -> None: ...
