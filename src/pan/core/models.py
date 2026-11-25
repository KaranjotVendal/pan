from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator

from pan.core.errors import SlackPostError


class WorkerStatus(StrEnum):
    SPAWNING = "spawning"
    RUNNING = "running"
    # Between turns: the session's turn ended and it waits for a follow-up. Set by the
    # Stop hook; the terminal is still live, so it is not finished work.
    IDLE = "idle"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


class ThreadSource(StrEnum):
    # Where a thread lives, so a Slack thread ts and (later) an X post id never collide.
    SLACK = "slack"
    X = "x"


class Conversation(BaseModel, frozen=True):
    # Where a message was said and where pan answers, on any source: a Slack thread (`id` its
    # thread ts, `place` its channel) or an X post (`id` the post id, no place). Lookups match
    # on `key`, the source and id, so a Slack thread and an X post with the same id never
    # collide; `place` only says where to reply.
    source: ThreadSource
    id: str
    place: str | None = None

    @classmethod
    def slack(cls, thread_ts: str, channel: str | None = None) -> Conversation:
        return cls(source=ThreadSource.SLACK, id=thread_ts, place=channel)

    @property
    def key(self) -> str:
        return f"{self.source.value}:{self.id}"

    def slack_channel(self) -> str:
        # The Slack channel pan posts in; SlackPostError when the conversation has none.
        if self.place is None:
            raise SlackPostError(f"conversation {self.key} has no channel to post in")
        return self.place


class RequestOrigin(BaseModel, frozen=True):
    # Where a request that starts a session came from, which the worker brief is worded for:
    # the conversation, whether the request was a reply inside a thread there, who asked, and
    # text from outside pan to show the session before its task. Outside context may come with
    # a request from any source (an X link pasted in Slack, say); None when there is none.
    conversation: Conversation
    from_thread_reply: bool = False
    requester: str | None = None
    outside_context: str | None = None


def _lift_flat_conversation(raw: Any, *, author_key: str | None = None) -> Any:
    # Files written before Conversation existed hold the identity flat: thread_ts, channel and
    # an optional source (and an InboxItem's sender as slack_user). They load into the new
    # shape and are written back in it on the next save. A flat record without a channel
    # stays unparseable, as it always was.
    if not isinstance(raw, dict) or "conversation" in raw:
        return raw
    if "thread_ts" not in raw or "channel" not in raw:
        return raw
    lifted = {
        key: value
        for key, value in raw.items()
        if key not in ("thread_ts", "channel", "source", author_key)
    }
    lifted["conversation"] = {
        "source": raw.get("source", ThreadSource.SLACK.value),
        "id": raw["thread_ts"],
        "place": raw["channel"],
    }
    if author_key is not None and author_key in raw:
        lifted["author"] = raw[author_key]
    return lifted


class ReplyMode(StrEnum):
    # ON_DEMAND: Slack gets the status message and then a reply only when asked. SLACK: the
    # request asked for a reply in Slack, so the answer is posted at every turn end.
    ON_DEMAND = "on_demand"
    SLACK = "slack"


class StatusState(StrEnum):
    # What pan's one status message in a thread says about the session (slack_render).
    WORKING = "working"
    WAITING = "waiting"
    FINISHED = "finished"
    CLOSED = "closed"
    STOPPED = "stopped"


class AgentStatus(StrEnum):
    # hc's OSC 7501 agent status vocabulary, modeled as a closed set so the reconcile
    # compares typed values rather than raw strings. NONE is a program that reports no
    # status (a shell, or an agent that has not reported yet). UNKNOWN absorbs any value hc
    # reports that is not a known member, so an unexpected string never crashes the adapter.
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    ERROR = "error"
    NONE = "none"
    UNKNOWN = "unknown"


class TerminalState(StrEnum):
    LIVE = "live"
    FINISHED = "finished"


class TerminalHostKind(StrEnum):
    # The terminal multiplexer pan drives through the TerminalHost seam (config key
    # `terminal_host`). Only one host is active at a time.
    HC = "hc"


class InboxItem(BaseModel, frozen=True):
    id: str
    # Who sent the message: a Slack user id.
    author: str
    conversation: Conversation
    is_thread_reply: bool
    raw_text: str
    received_at: datetime
    # The message mentioned the bot (an app_mention event); False for a reply the gateway
    # forwarded without one. Items written before it existed were all mentions.
    mentioned: bool = True

    @model_validator(mode="before")
    @classmethod
    def _load_flat_item(cls, raw: Any) -> Any:
        return _lift_flat_conversation(raw, author_key="slack_user")


# Keys only a herdr-era thread record carries. Such a record points at a herdr pane that
# no longer exists, so it loads as lost (FAILED) and is never driven.
_LEGACY_RECORD_KEYS = frozenset({"workspace_name", "workspace_id", "pane_ids", "morcli_session"})


class ThreadRecord(BaseModel):
    conversation: Conversation
    # The worker's hc terminal: `terminal_name` is the stream name (also the git branch);
    # `terminal_address` is hc's address (t7, machine/t7), empty when no terminal exists.
    terminal_name: str
    terminal_address: str = ""
    # The worker's worktree; for a session pan did not start (started_by_pan False), the
    # session's own folder, which pan never removes.
    worktree_path: Path
    # The repository pan created the worktree from (pan spawn's --repo), so `done` can delete
    # the branch after the worktree folder is gone. None on records written before it existed.
    repo_path: Path | None = None
    # Recorded by the worker's SessionStart hook; used by `pan read --full`.
    claude_session_id: str | None = None
    transcript_path: Path | None = None
    status: WorkerStatus = WorkerStatus.SPAWNING
    created_at: datetime
    updated_at: datetime
    # Session control. Every field defaults, so records written before they existed load.
    # The thread is tied to this session: follow-ups go to it. One thread per session, so a
    # new tie of the session elsewhere unties this one (FileThreadMap.tie).
    tied: bool = True
    started_by_pan: bool = True
    # The sender of the request that started the session (a Slack user id).
    requester: str | None = None
    reply_mode: ReplyMode = ReplyMode.ON_DEMAND
    # The Plannotator Inbox thread the session reports in: the name pan gives it at spawn,
    # and the id and link the inbox-link hook captures from the session's first send.
    inbox_thread_name: str | None = None
    inbox_thread_id: str | None = None
    inbox_thread_url: str | None = None
    # pan's one status message in the thread (its Slack ts), edited in place as the session's
    # state changes; the short task label it shows and when that task started. None on records
    # written before they existed, and status_message_ts until the first status is posted.
    status_message_ts: str | None = None
    status_label: str | None = None
    task_started_at: datetime | None = None
    # A digest of the last answer pan posted to the thread (Stop hook or `check`), so the same
    # answer is never posted twice. None until one is posted.
    last_answer_digest: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _load_legacy_record(cls, raw: Any) -> Any:
        return _load_legacy_herdr_record(_lift_flat_conversation(raw))


def _load_legacy_herdr_record(raw: Any) -> Any:
    if not isinstance(raw, dict) or "terminal_name" in raw:
        return raw
    if not _LEGACY_RECORD_KEYS.intersection(raw):
        return raw
    upgraded = {key: value for key, value in raw.items() if key not in _LEGACY_RECORD_KEYS}
    upgraded["terminal_name"] = raw.get("workspace_name", "")
    upgraded["terminal_address"] = ""
    if raw.get("status") != WorkerStatus.DONE.value:
        upgraded["status"] = WorkerStatus.FAILED.value
    return upgraded


class ConfirmationCandidate(BaseModel, frozen=True):
    # One session pan proposes: its terminal address, name, folder and Herdr label (checked
    # again at send time, so a terminal now at that address is the same session), and the
    # label shown in Slack.
    address: str
    name: str | None = None
    cwd: Path
    where: str | None = None
    label: str


class PendingConfirmation(BaseModel, frozen=True):
    # pan's open question in a conversation: which session should get `task`. Stored per
    # thread until it is answered, cancelled or expires (CONFIRMATION_TIMEOUT). It is not kept
    # on the thread's ThreadRecord because a send to a named session in a new thread has no
    # record yet. `answered_at` and `answer_text` (the reply, cleaned) are set by `pan inbox
    # drain` when the requester replies after pan last asked; `pan confirm --choice` refuses
    # until they are set, and they are cleared whenever pan asks again. A question stored
    # without a requester is never answered; it expires.
    conversation: Conversation
    selector: str
    task: str
    requester: str | None = None
    reply_in_slack: bool = False
    candidates: list[ConfirmationCandidate]
    created_at: datetime
    # When pan last asked: at creation, and again at each re-ask or busy/blocked refusal. None
    # on questions stored before it existed (created_at is used then).
    asked_at: datetime | None = None
    answered_at: datetime | None = None
    answer_text: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _load_flat_question(cls, raw: Any) -> Any:
        return _lift_flat_conversation(raw)

    def last_asked_at(self) -> datetime:
        return self.asked_at if self.asked_at is not None else self.created_at


class Terminal(BaseModel, frozen=True):
    # The confined projection of one terminal; the only terminal type a terminal host
    # adapter lets escape (INV-8). `address` is the host's id (for hc: t7, machine/t7);
    # `name` is unique among live terminals; `where` is the Herdr app's "space / tab" label
    # when it shows one (hc only; other hosts leave it empty). `turn` counts the agent's
    # finished turns when the host reports it (0 for a session that has not answered yet).
    address: str
    name: str | None = None
    where: str | None = None
    cwd: Path
    state: TerminalState
    agent: str | None = None
    agent_status: AgentStatus
    turn: int | None = None


class WorktreeStatus(BaseModel, frozen=True):
    # What a read-only look at a git worktree found (GitWorktreeAdapter.worktree_status).
    # `main_repo` is the repository's main worktree, where its branches are deleted from.
    # `branch` is the checked-out branch, None when HEAD is detached. Paths are relative to
    # the worktree; ignored files are left out. `untracked_paths` are new files git does not
    # track; `modified_paths` are tracked files with uncommitted changes (staged or not).
    # `detached_commits` counts, for a detached HEAD only, its commits on no branch and no
    # remote branch: removing the worktree would lose them.
    main_repo: Path
    is_main_worktree: bool
    branch: str | None = None
    untracked_paths: list[str] = Field(default_factory=list)
    modified_paths: list[str] = Field(default_factory=list)
    detached_commits: int = 0


class BranchStatus(BaseModel, frozen=True):
    # What a read-only look at a local branch found (GitWorktreeAdapter.branch_status).
    # `upstream` is its configured upstream (None when it has none); `upstream_gone` is True
    # when that upstream no longer exists on the remote (deleted after a merge, then pruned
    # by a fetch). `commits_not_on_upstream` counts the branch's commits the upstream lacks (0
    # without one, or when it is gone). `commits_not_in_base` counts its commits in neither the
    # remote's default branch (origin/HEAD) nor the branch the main worktree has checked out,
    # which is the branch pan created the worktree from.
    upstream: str | None = None
    upstream_gone: bool = False
    commits_not_on_upstream: int = 0
    commits_not_in_base: int = 0


class InboxThread(BaseModel, frozen=True):
    # One Plannotator Inbox thread found by name. `url` is None when the Inbox listing
    # carries no link for it.
    thread_id: str
    name: str
    url: str | None = None


class TerminalRead(BaseModel, frozen=True):
    # `complete` is False when the host could return only the visible screen (the target was
    # working, blocked or in error), so a summary of it must not overclaim.
    text: str
    complete: bool


class SessionSummary(BaseModel, frozen=True):
    # A Terminal flattened, plus the pan-owned join. The pan_* fields are None for an
    # external (non-pan-owned) terminal. `drift` is meaningful only when is_pan_owned is
    # True — an external terminal has no pan_status to disagree with.
    address: str
    name: str | None = None
    where: str | None = None
    cwd: Path
    agent: str | None = None
    agent_status: AgentStatus
    thread_ts: str | None = None
    # The channel of the session's Slack thread, when pan has one.
    thread_channel: str | None = None
    pan_status: WorkerStatus | None = None
    is_pan_owned: bool = False
    drift: bool = False
    # The last thing the session was asked, from its transcript when pan knows it.
    last_ask: str | None = None
    # The Plannotator Inbox thread the session reports in, when pan knows it from its record.
    inbox_thread_name: str | None = None
    # The short task label pan keeps for the session's status message (ThreadRecord.status_label).
    label: str | None = None
    # When pan last updated the session's record: how recently it was used.
    updated_at: datetime | None = None


class LaunchdJobState(BaseModel, frozen=True):
    # What launchd reports for one loaded job. `pid` is None when the job is loaded but not
    # running (com.pan.up after it succeeded); `last_exit_code` is None when it never exited.
    pid: int | None = None
    last_exit_code: int | None = None
    state: str | None = None


class ServiceStatus(BaseModel, frozen=True):
    # One `pan service status` row: `name` is the pan command the agent runs (gateway,
    # watcher, up), `label` its launchd label (com.pan.<name>).
    name: str
    label: str
    plist_installed: bool
    loaded: bool
    pid: int | None = None
    last_exit_code: int | None = None


class SlackCredentials(BaseModel, frozen=True):
    bot_token: SecretStr
    app_token: SecretStr


class SlackConfig(BaseModel, frozen=True):
    socket_mode: bool = True


class UserPolicy(BaseModel, frozen=True):
    channels: list[str] = Field(default_factory=lambda: ["*"])


class OrchestratorConfig(BaseModel, frozen=True):
    # `terminal` is the orchestrator's hc name (an address also works). A name, because
    # `pan up` always recreates the orchestrator under it while its address changes.
    terminal: str = "pan-orchestrator"
    cwd: Path
    # The program `pan up` starts the orchestrator with; the boot brief is appended as the
    # last argument. Add permission flags here if the orchestrator should not prompt.
    command: list[str] = Field(default_factory=lambda: ["claude"])
    worktree_base: Path
    # Optional Herdr app space id. When set, each worker is labelled into that space so it
    # shows as a tab; when absent, workers stay hidden. pan never creates spaces.
    app_space: str | None = None


class PanPaths(BaseModel, frozen=True):
    inbox: Path
    threads: Path
    credentials: Path


class PanConfig(BaseModel, frozen=True):
    slack: SlackConfig = Field(default_factory=SlackConfig)
    terminal_host: TerminalHostKind = TerminalHostKind.HC
    orchestrator: OrchestratorConfig
    users: dict[str, UserPolicy] = Field(default_factory=dict)
    paths: PanPaths
