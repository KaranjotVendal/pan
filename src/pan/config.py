from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from pan.errors import ConfigMissingError
from pan.logging import initialise_logger
from pan.models import PanConfig

logger = initialise_logger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pan" / "config.json"

# Default recent-lines window for `pan read` (no --full): the lines of terminal history
# fetched via `hc read` before the orchestrator summarizes them. Named here rather than
# inlined at the call site so the tunable is discoverable and overridable (Principle 7).
READ_RECENT_LINES = 200
# `pan read --full` on a terminal pan has no transcript for (an external one) falls back to
# this much hc screen history.
READ_FULL_HISTORY_LINES = 5000
# The watcher wakes the orchestrator once the inbox has been quiet this long, so a burst of
# filesystem events (or several items arriving together) becomes a single wake.
WATCHER_DEBOUNCE_SECONDS = 1.0

# hc labels pan puts on the terminals it starts. Workers carry their thread and channel so
# `hc ls --label` finds them without the thread map; `herdr.space` opts a terminal into a
# Herdr app space (only when orchestrator.app_space is configured).
PAN_ROLE_LABEL = "pan.role"
PAN_THREAD_LABEL = "pan.thread"
PAN_CHANNEL_LABEL = "pan.channel"
WORKER_ROLE = "worker"
ORCHESTRATOR_ROLE = "orchestrator"
HERDR_SPACE_LABEL = "herdr.space"

# The agent names hc reports for a running Claude Code session (`claude-code` once it
# reports status; the program name before that).
CLAUDE_AGENT_NAMES: frozenset[str] = frozenset({"claude-code", "claude"})

# hc terminal names allow 1-32 of a-z 0-9 - _. Worker names are `pan-<stream>`.
WORKER_NAME_PREFIX = "pan-"
TERMINAL_NAME_MAX_LENGTH = 32

# Slack mrkdwn converter tunables (Principle 7 — named, not inlined in slack_format.py).
# The sentinel template stashes a protected region behind a NUL-delimited key that survives
# every later regex pass, then is restored verbatim (adapted from Hermes' `\x00SL<n>\x00`).
SLACK_MRKDWN_PLACEHOLDER_TEMPLATE = "\x00SL{index}\x00"
# A GFM table has no Slack primitive, so it degrades to an aligned monospace grid: each cell
# left-justified with the pad char to its column width, columns joined by the separator.
SLACK_TABLE_COLUMN_SEPARATOR = " | "
SLACK_TABLE_PAD_CHAR = " "

# `@pan help` tunables (Principle 7 — named here, not inlined in help.py / gateway/app.py).
# The exact tokens that mean "show me the help", matched on lowercased whole tokens only, so a
# task brief that merely contains one is not hijacked.
HELP_REQUEST_TOKENS: frozenset[str] = frozenset({"help", "--help", "-h", "?"})
# In TRAILING position (`relay --help`) only the unambiguous flag forms count. A trailing bare
# `help`/`?` is ordinary prose (`deploy ?`, `retry help`), and treating it as help would swallow
# a real task: the help branch answers instantly and never reaches the inbox.
HELP_FLAG_TOKENS: frozenset[str] = frozenset({"--help", "-h"})
# The unknown-command notice echoes a Slack-supplied token back into the reply, so it is
# truncated (and reduced to a safe charset in help.py) before it is posted.
HELP_ECHOED_COMMAND_MAX_LENGTH = 40
# The commands worth listing to a phone user. The rest (gateway, watcher, hook, inbox, threads,
# spawn, slack-post) are launchd/dev-only, so they are dropped from the Slack listing; asking for
# one by name (`help inbox`) still renders it, so nothing is hard-blocked.
HELP_USER_FACING_COMMANDS: tuple[str, ...] = (
    "sessions",
    "relay",
    "read",
    "status",
    "stop",
    "pause",
)
# Slack renders a fenced block as monospace, and to_slack_mrkdwn passes a fence through verbatim,
# so the generated help keeps its column alignment.
CODE_FENCE = "```"
# The Slack-only directive grammar lives in parse_directive, NOT in Typer, so the auto-generated
# help cannot see it (help tech-spec R-1). This short block is the one hand-written piece and must
# be updated whenever a directive flag changes.
SLACK_DIRECTIVE_FLAGS_FOOTER = """
The commands above are terminal commands (pan <command>). From Slack, say these:
  <task>                     start a worker; it acks now, asks here if it gets
                             stuck, and posts the result when it is done
  !<task>  or  --sync        same worker, but pan waits for it to finish before
                             handling anything else in this thread
  --status                   the live state of this thread's worker
  --sessions                 list every live session (a bare "sessions" works too)
  --new                      force a new worker instead of reusing this thread's
  --stream <name>            name the worker's terminal
  relay <target> <message>   send a message into a live session
  read <target> [--full]     read a live session's recent output (or all of it)
  help [command]             this help
""".strip()


def _expand_user_paths(raw_config: dict) -> None:
    paths = raw_config.get("paths")
    if isinstance(paths, dict):
        for key, value in list(paths.items()):
            if isinstance(value, str):
                paths[key] = str(Path(value).expanduser())

    orchestrator = raw_config.get("orchestrator")
    if isinstance(orchestrator, dict):
        for key in ("worktree_base", "cwd"):
            value = orchestrator.get(key)
            if isinstance(value, str):
                orchestrator[key] = str(Path(value).expanduser())


def _reject_herdr_era_orchestrator(raw_config: dict, config_path: Path) -> None:
    # herdr pane ids went stale on every restart and herdr itself is gone; say exactly
    # which keys replace them rather than failing with a generic missing-field error.
    orchestrator = raw_config.get("orchestrator")
    if isinstance(orchestrator, dict) and "pane_id" in orchestrator:
        raise ConfigMissingError(
            f"config orchestrator.pane_id is no longer used: {config_path}. Replace it with "
            "orchestrator.terminal (the orchestrator's hc terminal name, default "
            "pan-orchestrator) and orchestrator.cwd (the folder the orchestrator runs in)"
        )


def load_config(path: Path | None = None) -> PanConfig:
    config_path = path if path is not None else DEFAULT_CONFIG_PATH

    try:
        raw_text = config_path.read_text()
    except FileNotFoundError as error:
        raise ConfigMissingError(f"config file not found: {config_path}") from error

    try:
        raw_config = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ConfigMissingError(f"config file is not valid JSON: {config_path}") from error

    if not isinstance(raw_config, dict):
        raise ConfigMissingError(f"config file must be a JSON object: {config_path}")

    _reject_herdr_era_orchestrator(raw_config, config_path)
    _expand_user_paths(raw_config)

    try:
        config = PanConfig.model_validate(raw_config)
    except ValidationError as error:
        raise ConfigMissingError(
            f"config file is missing required fields: {config_path}"
        ) from error

    logger.info(f"config loaded path={config_path}")
    return config
