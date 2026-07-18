from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr


class TaskMode(StrEnum):
    DELEGATE = "delegate"
    SYNC = "sync"
    STATUS = "status"


class WorkerStatus(StrEnum):
    SPAWNING = "spawning"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"


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
