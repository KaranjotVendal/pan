from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr


class TaskMode(StrEnum):
    DELEGATE = "delegate"
    SYNC = "sync"
    STATUS = "status"
    SESSIONS = "sessions"
    RELAY = "relay"  # drive a target session's pane with a message


class WorkerStatus(StrEnum):
    SPAWNING = "spawning"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


class AgentStatus(StrEnum):
    # herdr's live per-agent status vocabulary, modeled as a closed set so the reconcile
    # compares typed values rather than raw strings. The members are inferred from the
    # herdr skill docs and morcli's status map; VERIFY against real `herdr workspace
    # list` / `pane list` output (tech-spec R-9) before treating them as final. UNKNOWN
    # absorbs any value herdr reports that is not a known member, so an unexpected string
    # never crashes the adapter.
    IDLE = "idle"
    WORKING = "working"
    BLOCKED = "blocked"
    DONE = "done"
    UNKNOWN = "unknown"


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
    # relay/read selector (label | workspace_id | pane_id) and the relay message. `target`
    # is distinct from `target_stream` (a spawn stream label); it names a live session to
    # address. `message` mirrors `cleaned_text` on the relay path.
    target: str | None = None
    message: str | None = None
    cleaned_text: str


class InboxItem(BaseModel, frozen=True):
    id: str
    slack_user: str
    channel: str
    thread_ts: str
    is_thread_reply: bool
    raw_text: str
    received_at: datetime


class ThreadRecord(BaseModel):
    thread_ts: str
    workspace_name: str
    workspace_id: str
    channel: str
    pane_ids: list[str] = Field(default_factory=list)
    worktree_path: Path
    agent: Agent = Agent.CLAUDE
    morcli_session: str | None = None
    status: WorkerStatus = WorkerStatus.SPAWNING
    created_at: datetime
    updated_at: datetime


class LiveSession(BaseModel, frozen=True):
    # The confined projection of one live herdr pane; the only session type the herdr
    # adapter lets escape (INV-8). An immutable snapshot of the live view.
    workspace_name: str
    workspace_id: str
    pane_id: str
    cwd: Path
    agent_status: AgentStatus


class SessionSummary(BaseModel, frozen=True):
    # A LiveSession flattened, plus the pan-owned join. The pan_* fields are None for an
    # external (non-pan-owned) session. `drift` is meaningful only when is_pan_owned is
    # True — an external session has no pan_status to disagree with.
    workspace_name: str
    workspace_id: str
    pane_id: str
    cwd: Path
    agent_status: AgentStatus
    thread_ts: str | None = None
    pan_status: WorkerStatus | None = None
    morcli_session: str | None = None
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
    workspace_name: str = "pan-orchestrator"
    pane_id: str
    worktree_base: Path


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
