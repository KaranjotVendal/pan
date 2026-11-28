from __future__ import annotations

from functools import cached_property

from pan.backend.confirmations import FilePendingConfirmations, confirmations_path
from pan.backend.project_map_sources import build_project_map_source
from pan.core.models import PanConfig
from pan.core.seams import (
    Clock,
    GitWorktreeAdapter,
    InboxDirectory,
    ProjectMapSource,
    Reporter,
    SlackAdapter,
    TerminalHost,
)
from pan.data.adapters.git_worktree import ShellGitWorktreeAdapter
from pan.data.adapters.plannotator_inbox import ShellPlannotatorInboxAdapter
from pan.data.adapters.terminal_host import build_terminal_host
from pan.data.drained import FileDrainedLog, drained_log_path
from pan.data.inbox import FileInboxStore
from pan.data.threadmap import FileThreadMap
from pan.frontend.gateway.app import BoltSlackAdapter
from pan.frontend.slack_reporter import SlackReporter
from pan.utils.clock import SystemClock
from pan.utils.config import load_config
from pan.utils.credentials import load_credentials


def build_slack_adapter(
    config: PanConfig, clock: Clock, *, thread_map: FileThreadMap | None = None
) -> SlackAdapter:
    # Loads the Slack credentials, so it raises CredentialsError (exit 12) when they are unset.
    credentials = load_credentials(config.paths.credentials)
    return BoltSlackAdapter(
        credentials, config, FileInboxStore(config.paths.inbox), clock, thread_map=thread_map
    )


class Wiring:
    # The adapters and stores a command works with, each built from the config the first time
    # it is used and then shared, so a command that never posts never loads the Slack
    # credentials. Tests replace an adapter by patching the builder this module calls.
    def __init__(self, config: PanConfig) -> None:
        self.config = config
        self.clock = SystemClock()

    @cached_property
    def thread_map(self) -> FileThreadMap:
        return FileThreadMap(self.config.paths.threads, self.clock)

    @cached_property
    def terminal_host(self) -> TerminalHost:
        return build_terminal_host(self.config.terminal_host)

    @cached_property
    def slack(self) -> SlackAdapter:
        return build_slack_adapter(self.config, self.clock)

    @cached_property
    def reporter(self) -> Reporter:
        # What the backend reports, shown in Slack; built (with the Slack adapter) on first use.
        return SlackReporter(self.slack)

    @cached_property
    def inbox_store(self) -> FileInboxStore:
        return FileInboxStore(self.config.paths.inbox)

    @cached_property
    def confirmations(self) -> FilePendingConfirmations:
        return FilePendingConfirmations(confirmations_path(self.config.paths.threads))

    @cached_property
    def drained_log(self) -> FileDrainedLog:
        return FileDrainedLog(drained_log_path(self.config.paths.threads))

    @cached_property
    def git(self) -> GitWorktreeAdapter:
        return ShellGitWorktreeAdapter()

    @cached_property
    def project_map_source(self) -> ProjectMapSource:
        # The configured project map (config `project_map`); the built-in one reads branches
        # through `git`.
        return build_project_map_source(self.config.project_map, git=self.git)

    @cached_property
    def inbox_directory(self) -> InboxDirectory:
        return ShellPlannotatorInboxAdapter()

    def gateway_slack_adapter(self) -> SlackAdapter:
        # Only the gateway's adapter reads the thread map (to forward follow-ups in tied threads).
        return build_slack_adapter(self.config, self.clock, thread_map=self.thread_map)


def load_wiring() -> Wiring:
    return Wiring(load_config())
