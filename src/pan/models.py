from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator


class TaskMode(StrEnum):
    DELEGATE = "delegate"
    SYNC = "sync"
    STATUS = "status"
    SESSIONS = "sessions"
    RELAY = "relay"  # send a message into a target terminal
    READ = "read"  # read a target session's output (recent | --full transcript)


class WorkerStatus(StrEnum):
    SPAWNING = "spawning"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


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


class Autonomy(StrEnum):
    FULL = "full"
    GATED = "gated"
    READONLY = "readonly"


class Agent(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    PI = "pi"


class Directive(BaseModel, frozen=True):
    mode: TaskMode = TaskMode.DELEGATE
    force_new: bool = False
    target_stream: str | None = None
    agent: Agent | None = None
    # relay/read selector (hc address | terminal name | Herdr app space label) and the relay
    # message. `target` is distinct from `target_stream` (a spawn stream name); it names a
    # live terminal to address. `message` mirrors `cleaned_text` on the relay path.
    target: str | None = None
    message: str | None = None
    # read the full transcript instead of recent lines (the `read` verb's --full modifier).
    full: bool = False
    cleaned_text: str


class InboxItem(BaseModel, frozen=True):
    id: str
    slack_user: str
    channel: str
    thread_ts: str
    is_thread_reply: bool
    raw_text: str
    received_at: datetime


# Keys only a herdr-era thread record carries. Such a record points at a herdr pane that
# no longer exists, so it loads as lost (FAILED) and is never driven.
_LEGACY_RECORD_KEYS = frozenset({"workspace_name", "workspace_id", "pane_ids", "morcli_session"})


class ThreadRecord(BaseModel):
    thread_ts: str
    # The worker's hc terminal: `terminal_name` is the stream name (also the git branch);
    # `terminal_address` is hc's address (t7, machine/t7), empty when no terminal exists.
    terminal_name: str
    terminal_address: str = ""
    channel: str
    worktree_path: Path
    agent: Agent = Agent.CLAUDE
    # Recorded by the worker's SessionStart hook; used by `pan read --full`.
    claude_session_id: str | None = None
    transcript_path: Path | None = None
    status: WorkerStatus = WorkerStatus.SPAWNING
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _load_legacy_herdr_record(cls, raw: Any) -> Any:
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


class Terminal(BaseModel, frozen=True):
    # The confined projection of one hc terminal; the only terminal type the hc adapter
    # lets escape (INV-8). `address` is hc's id (t7, machine/t7); `name` is unique among
    # live terminals; `where` is the Herdr app's "space / tab" label when it shows one.
    address: str
    name: str | None = None
    where: str | None = None
    cwd: Path
    state: TerminalState
    agent: str | None = None
    agent_status: AgentStatus


class TerminalRead(BaseModel, frozen=True):
    # `complete` is False when hc could return only the visible screen (the target was
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
    pan_status: WorkerStatus | None = None
    is_pan_owned: bool = False
    drift: bool = False


class SlackCredentials(BaseModel, frozen=True):
    bot_token: SecretStr
    app_token: SecretStr


class SlackConfig(BaseModel, frozen=True):
    socket_mode: bool = True


class UserPolicy(BaseModel, frozen=True):
    autonomy: Autonomy = Autonomy.FULL
    channels: list[str] = Field(default_factory=lambda: ["*"])
    repos: list[str] = Field(default_factory=lambda: ["*"])


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


class Defaults(BaseModel, frozen=True):
    agent: Agent = Agent.CLAUDE
    permission_mode: str = "bypass"
    repo_allowlist: list[str] = Field(default_factory=list)


class PanPaths(BaseModel, frozen=True):
    inbox: Path
    threads: Path
    logs: Path
    credentials: Path


class PanConfig(BaseModel, frozen=True):
    slack: SlackConfig = Field(default_factory=SlackConfig)
    orchestrator: OrchestratorConfig
    defaults: Defaults
    users: dict[str, UserPolicy] = Field(default_factory=dict)
    gated_ops: list[str] = Field(default_factory=list)
    paths: PanPaths
