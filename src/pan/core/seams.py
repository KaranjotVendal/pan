from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pan.core.events import Event
from pan.core.models import (
    BranchStatus,
    Conversation,
    InboxItem,
    InboxThread,
    LaunchdJobState,
    PendingConfirmation,
    ReplyMode,
    Terminal,
    TerminalRead,
    ThreadRecord,
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


class Reporter(Protocol):
    # Shows what happened in a conversation to the people in it (Slack today: see
    # frontend.slack_reporter). The backend reports events (core.events), never wording. For a
    # StatusChanged it returns the id of the status message when it posted a new one, None
    # when it edited the one it was given; every other event returns None. A failed delivery
    # raises SlackPostError.
    def report(self, conversation: Conversation, event: Event) -> str | None: ...


# A Reporter built only when something is reported, for work that must finish even when the
# reporter cannot be built (its credentials are missing: CredentialsError).
OpenReporter = Callable[[], Reporter]


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGen(Protocol):
    def new_id(self) -> str: ...


class InboxStore(Protocol):
    def append(self, item: InboxItem) -> None: ...

    def drain(self) -> list[InboxItem]: ...


class ThreadMap(Protocol):
    # Records are kept per (conversation, session), matched on Conversation.key (source and
    # id). A conversation lookup returns its tied record, else its newest; `session_name` (a terminal name) picks that session's record
    # in the thread instead, which is how a session's own hooks reach it after it was untied.
    def get(
        self, conversation: Conversation, *, session_name: str | None = None
    ) -> ThreadRecord | None: ...

    def get_by_worktree(self, worktree_path: Path) -> ThreadRecord | None: ...

    def put(self, record: ThreadRecord) -> None: ...

    # Store the record as its thread's tie, untying any other thread tied to the same session
    # and the thread's previous session.
    def tie(self, record: ThreadRecord) -> None: ...

    def untie(self, conversation: Conversation) -> None: ...

    def record_inbox_thread(
        self,
        conversation: Conversation,
        inbox_thread_id: str,
        url: str | None,
        *,
        session_name: str | None = None,
    ) -> None: ...

    def update_status(
        self, conversation: Conversation, status: WorkerStatus, *, session_name: str | None = None
    ) -> None: ...

    # Persist the thread's reply mode (a follow-up or reopen that asks for the answer in Slack).
    def set_reply_mode(
        self, conversation: Conversation, reply_mode: ReplyMode, *, session_name: str | None = None
    ) -> None: ...

    def records(self) -> list[ThreadRecord]: ...

    # The ts of pan's status message in the thread (see ThreadRecord.status_message_ts).
    def record_status_message(
        self, conversation: Conversation, message_ts: str, *, session_name: str | None = None
    ) -> None: ...

    # The digest of the answer pan just posted to the thread (see last_answer_digest).
    def record_posted_answer(
        self, conversation: Conversation, answer_digest: str, *, session_name: str | None = None
    ) -> None: ...

    # The task the status message shows: its short label and when it started.
    def record_task(
        self,
        conversation: Conversation,
        label: str,
        started_at: datetime,
        *,
        session_name: str | None = None,
    ) -> None: ...

    def record_session(
        self,
        conversation: Conversation,
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
    def get(self, conversation: Conversation) -> PendingConfirmation | None: ...

    def all_pending(self) -> list[PendingConfirmation]: ...

    def put(self, pending: PendingConfirmation) -> None: ...

    # Sets the question's answer (the latest reply wins); a no-op when there is no question.
    def mark_answered(
        self, conversation: Conversation, answered_at: datetime, answer_text: str
    ) -> None: ...

    # Clears the question's answer when pan asks again at `asked_at`; a no-op when there is no
    # question.
    def clear_answer(self, conversation: Conversation, asked_at: datetime) -> None: ...

    def clear(self, conversation: Conversation) -> None: ...


class DrainedLog(Protocol):
    # The items `pan inbox drain` handed out, kept so an action can check the message it rests
    # on. Reads never fail (an unreadable log counts as empty); `record` raises OSError when it
    # cannot write.
    def get(self, item_id: str) -> InboxItem | None: ...

    def newest_in(self, conversation: Conversation, author: str) -> InboxItem | None: ...

    def record(self, items: list[InboxItem], now: datetime) -> None: ...


class GitWorktreeAdapter(Protocol):
    # Creating a worktree raises SpawnError; the other methods raise GitWorktreeError.
    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path: ...

    # Read-only: changes nothing in the worktree or its repository.
    def worktree_status(self, path: Path) -> WorktreeStatus: ...

    # Read-only and cheap (one git call). The branch checked out in `path`; None when `path` is
    # not in a git repository (or does not exist) or its HEAD is detached. Never raises.
    def current_branch(self, path: Path) -> str | None: ...

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
    # conversation is not the launcher's.
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
