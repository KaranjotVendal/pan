from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pan.models import (
    BranchStatus,
    InboxItem,
    InboxThread,
    LaunchdJobState,
    PendingConfirmation,
    ReplyMode,
    Terminal,
    TerminalRead,
    ThreadRecord,
    ThreadSource,
    WorkerStatus,
    WorktreeStatus,
)

# Every seam Protocol lives here for a single import point.


class SlackAdapter(Protocol):
    def add_reaction(self, channel: str, ts: str, name: str) -> None: ...

    def post_message(self, channel: str, thread_ts: str, text: str) -> None: ...

    # A Block Kit message with its plain-text fallback; returns the posted message's ts.
    def post_blocks(
        self, channel: str, thread_ts: str, blocks: list[dict[str, Any]], text: str
    ) -> str: ...

    # Replace a posted message's blocks and fallback text (chat.update).
    def update_message(
        self, channel: str, ts: str, blocks: list[dict[str, Any]], text: str
    ) -> None: ...

    # A message in the thread only `user` sees (chat.postEphemeral).
    def post_ephemeral(self, channel: str, thread_ts: str, user: str, text: str) -> None: ...

    # A message's link (chat.getPermalink); None when Slack cannot give one.
    def permalink(self, channel: str, ts: str) -> str | None: ...

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

    # Persist the thread's reply mode (a follow-up or reopen that asks for the answer in Slack).
    def set_reply_mode(
        self, thread_ts: str, reply_mode: ReplyMode, *, session_name: str | None = None
    ) -> None: ...

    def records(self) -> list[ThreadRecord]: ...

    # The ts of pan's status message in the thread (see ThreadRecord.status_message_ts).
    def record_status_message(
        self, thread_ts: str, message_ts: str, *, session_name: str | None = None
    ) -> None: ...

    # The digest of the answer pan just posted to the thread (see last_answer_digest).
    def record_posted_answer(
        self, thread_ts: str, answer_digest: str, *, session_name: str | None = None
    ) -> None: ...

    # The task the status message shows: its short label and when it started.
    def record_task(
        self, thread_ts: str, label: str, started_at: datetime, *, session_name: str | None = None
    ) -> None: ...

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


class PendingConfirmations(Protocol):
    # pan's open questions asking the person which session should get a task, one per thread
    # (see PendingConfirmation). `put` replaces the thread's question; `clear` is a no-op when
    # there is none. Reads never fail: an unreadable store counts as holding no questions.
    def get(self, source: ThreadSource, thread_ts: str) -> PendingConfirmation | None: ...

    def all_pending(self) -> list[PendingConfirmation]: ...

    def put(self, pending: PendingConfirmation) -> None: ...

    # Sets the question's answer (the latest reply wins); a no-op when there is no question.
    def mark_answered(
        self, source: ThreadSource, thread_ts: str, answered_at: datetime, answer_text: str
    ) -> None: ...

    # Clears the question's answer when pan asks again; a no-op when there is no question.
    def clear_answer(self, source: ThreadSource, thread_ts: str) -> None: ...

    def clear(self, source: ThreadSource, thread_ts: str) -> None: ...


class GitWorktreeAdapter(Protocol):
    # Creating a worktree raises SpawnError; the other methods raise GitWorktreeError.
    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path: ...

    # Read-only: changes nothing in the worktree or its repository.
    def worktree_status(self, path: Path) -> WorktreeStatus: ...

    # Read-only. None when the repository has no such local branch.
    def branch_status(self, repo: Path, branch: str) -> BranchStatus | None: ...

    # `force` also removes a worktree holding uncommitted or untracked files.
    def remove_worktree(self, path: Path, *, force: bool) -> None: ...

    # Drop the repository's records of worktrees whose folders are gone.
    def prune_worktrees(self, repo: Path) -> None: ...

    # `force` (-D) also deletes a branch whose commits are not merged (-d refuses it).
    def delete_branch(self, repo: Path, branch: str, *, force: bool) -> None: ...


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


class ServiceManager(Protocol):
    # pan's per-user launchd agents (see `pan service`). Only the launchd adapter in
    # src/pan/adapters/launchd.py runs launchctl or writes the LaunchAgents folder. Failures
    # raise ServiceError.
    def plist_installed(self, label: str) -> bool: ...

    # Write the agent's plist into the LaunchAgents folder and return its path.
    def write_plist(self, label: str, content: str) -> Path: ...

    # Delete the agent's plist; returns False when there was none.
    def remove_plist(self, label: str) -> bool: ...

    def ensure_directory(self, path: Path) -> None: ...

    # None when launchd has no job of that label loaded.
    def job_state(self, label: str) -> LaunchdJobState | None: ...

    def bootstrap(self, plist_path: Path) -> None: ...

    # Unload the job; a job that is already gone counts as unloaded.
    def bootout(self, label: str) -> None: ...
