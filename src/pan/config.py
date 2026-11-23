from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from pan.errors import ConfigMissingError
from pan.logging import initialise_logger
from pan.models import PanConfig, TerminalHostKind

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

# Labels pan puts on the terminals it starts, for a host that supports them (hc does).
# Workers carry their thread and channel so `hc ls --label` finds them without the thread
# map; `herdr.space` opts a terminal into a Herdr app space (only when
# orchestrator.app_space is configured). They are a hint only: whether pan started a
# terminal is decided by the thread map, never by labels.
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

# A worker's Plannotator Inbox thread is named `<terminal name>: <short task>`; the task part
# is cut to this many characters so the name stays one short line.
INBOX_THREAD_TASK_MAX_LENGTH = 60

# A worker's brief is the Inbox preamble, this marker, then the task. `pan sessions` cuts a
# brief back to its task when it shows the session's last ask.
WORKER_BRIEF_TASK_MARKER = "\n\nTask:\n"
# `pan sessions` shows the last thing each session was asked on one line, cut to this length.
SESSION_LAST_ASK_MAX_LENGTH = 80

# The Claude Code tool name of the Plannotator Inbox tool, matched by the worker's
# PostToolUse inbox-link hook.
PLANNOTATOR_INBOX_TOOL_NAME = "mcp__plannotator__plannotator_inbox"
# Looking up a desk session's Inbox thread by name (`plannotator inbox call read_thread`) lists
# this many of the project's open threads, and gives up after this many seconds so `check` and
# `send` never hang on the Inbox.
INBOX_LOOKUP_THREAD_LIMIT = 50
INBOX_LOOKUP_TIMEOUT_SECONDS = 10.0

# Slack mrkdwn converter tunables (Principle 7 — named, not inlined in slack_format.py).
# The sentinel template stashes a protected region behind a NUL-delimited key that survives
# every later regex pass, then is restored verbatim (adapted from Hermes' `\x00SL<n>\x00`).
SLACK_MRKDWN_PLACEHOLDER_TEMPLATE = "\x00SL{index}\x00"
# A GFM table has no Slack primitive, so it degrades to an aligned monospace grid: each cell
# left-justified with the pad char to its column width, columns joined by the separator.
SLACK_TABLE_COLUMN_SEPARATOR = " | "
SLACK_TABLE_PAD_CHAR = " "

# A long message (a `check` answer, a long sessions list) is posted in parts of at most this many
# characters. Slack shows a message whole up to about 4000 characters; the margin leaves room for
# the part label and for mrkdwn escaping, which can lengthen the text.
SLACK_MESSAGE_PART_MAX_LENGTH = 3500

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
    "check",
    "forget",
    "relay",
    "read",
    "status",
    "stop",
    "pause",
)
# Slack renders a fenced block as monospace, and to_slack_mrkdwn passes a fence through verbatim,
# so the generated help keeps its column alignment.
CODE_FENCE = "```"
# The Slack-only grammar (route words in parse_route, directive flags in parse_directive) is NOT
# in Typer, so the auto-generated help cannot see it (help tech-spec R-1). This short block is the
# one hand-written piece and must be updated whenever a route word or directive flag changes.
SLACK_DIRECTIVE_FLAGS_FOOTER = """
The commands above are terminal commands (pan <command>). From Slack, say these:
  <task>                     in a new thread, start a session in a new worktree;
                             in a tied thread, send it to the tied session
  new: <task>                start a new session even in a tied thread (the tie moves)
  to <name>: <task>          send to the live session <name> (pan's or yours) if it is
                             idle; tie this thread to it
  check                      post the tied session's latest answer here
  sessions                   list live sessions: name, idle or busy, last ask
                             (--sessions works too)
  forget                     untie this thread (the session keeps running)
  ... reply in slack ...     also post the session's answers here at every turn end
  --stream <name>            name a new session's terminal
  relay <target> <message>   send a one-off message into a live session (ties nothing)
  read <target> [--full]     read a live session's recent output (or all of it)
  help [command]             this help
--new, --status, --sync and a leading ! are retired: use new:, check, and reply in slack.
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


def _reject_unknown_terminal_host(raw_config: dict, config_path: Path) -> None:
    if "terminal_host" not in raw_config:
        return
    terminal_host = raw_config["terminal_host"]
    accepted = sorted(kind.value for kind in TerminalHostKind)
    if terminal_host not in accepted:
        raise ConfigMissingError(
            f"config terminal_host {terminal_host!r} is not supported: {config_path}. "
            f"Accepted values: {', '.join(accepted)}"
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
    _reject_unknown_terminal_host(raw_config, config_path)
    _expand_user_paths(raw_config)

    try:
        config = PanConfig.model_validate(raw_config)
    except ValidationError as error:
        raise ConfigMissingError(
            f"config file is missing required fields: {config_path}"
        ) from error

    logger.info(f"config loaded path={config_path}")
    return config
