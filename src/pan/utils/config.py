from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from pan.core.errors import ConfigMissingError
from pan.core.models import PanConfig, TerminalHostKind
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pan" / "config.json"

# Default recent-lines window for `pan read` (no --full): the lines of terminal history
# fetched via `hc read` before the orchestrator summarizes them. Named here rather than
# inlined at the call site so the tunable is discoverable and overridable (Principle 7).
READ_RECENT_LINES = 200
# `check` on a session with no transcript posts its screen text, cleaned for Slack and cut to
# at most this many non-empty lines from the bottom of the screen.
CHECK_SCREEN_MAX_LINES = 30
# `pan read --full` on a terminal pan has no transcript for (an external one) falls back to
# this much hc screen history.
READ_FULL_HISTORY_LINES = 5000
# The watcher wakes the orchestrator once the inbox has been quiet this long, so a burst of
# filesystem events (or several items arriving together) becomes a single wake.
WATCHER_DEBOUNCE_SECONDS = 1.0

# The launchd agents `pan service` installs, in install order: the gateway and watcher are
# KeepAlive services, and `up` runs `pan up` once at login (it needs the other two up first).
# Each is the per-user LaunchAgent com.pan.<name>, rendered from the package's
# launchd/com.pan.<name>.plist.template.
LAUNCHD_SERVICE_NAMES: tuple[str, ...] = ("gateway", "watcher", "up")
LAUNCHD_LABEL_PREFIX = "com.pan."
LAUNCHD_TEMPLATE_SUFFIX = ".plist.template"
# The two placeholders in those templates.
LAUNCHD_HOME_PLACEHOLDER = "<PAN_HOME>"
LAUNCHD_BINARY_PLACEHOLDER = "<PAN_BIN>"
# A bootstrap right after a bootout can fail while launchd still tears the old job down, so
# the launchd adapter retries it this many times, this far apart.
LAUNCHD_BOOTSTRAP_ATTEMPTS = 3
LAUNCHD_BOOTSTRAP_RETRY_SECONDS = 1.0

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

# The Claude Code settings file (the worker's hooks) pan writes into every worker's worktree,
# relative to the worktree. `done` does not count it as the worker's uncommitted work.
WORKER_SETTINGS_RELATIVE_PATH = ".claude/settings.json"

# A worker's Plannotator Inbox thread is named `<terminal name>: <short task>`; the task part
# is cut to this many characters so the name stays one short line.
INBOX_THREAD_TASK_MAX_LENGTH = 60

# A worker's brief is the Inbox preamble, this marker, then the task. `pan sessions` cuts a
# brief back to its task when it shows the session's last ask.
WORKER_BRIEF_TASK_MARKER = "\n\nTask:\n"
# The Slack plugin's read-thread tool, named in the brief of a new session started from a reply
# inside a Slack thread: the session reads the thread itself, pan never fetches it.
SLACK_READ_THREAD_TOOL_NAME = "mcp__plugin_slack_slack__slack_read_thread"
# `pan sessions` shows the last thing each session was asked on one line, cut to this length.
SESSION_LAST_ASK_MAX_LENGTH = 80
# `pan sessions --show` sends the requester at most this many sessions, in the order given.
SESSIONS_SLACK_MAX_COUNT = 5
# `pan sessions` shows a stream's goal, from its workset file, cut to this length.
WORKSET_GOAL_MAX_LENGTH = 120
# A stream's workset file, relative to the folder holding the project's docs; `{stream}` is
# the stream's terminal name without STREAM_NAME_PREFIX.
WORKSET_RELATIVE_PATH = "docs/streams/{stream}/workset.md"
STREAM_NAME_PREFIX = "stream-"
# Coding agents hc names as a terminal's program, recognised as agent sessions even before
# they report a status (backend.project_map.is_agent_session).
KNOWN_AGENT_NAMES: frozenset[str] = CLAUDE_AGENT_NAMES | {"pi"}
# `pan sessions` reads each agent session's branch with one git call, given up after this
# long (a hung network filesystem must not hang the listing).
GIT_BRANCH_READ_TIMEOUT_SECONDS = 3.0

# The Claude Code tool name of the Plannotator Inbox tool, matched by the worker's
# PostToolUse inbox-link hook.
PLANNOTATOR_INBOX_TOOL_NAME = "mcp__plannotator__plannotator_inbox"
# The Claude Code tool name of code mode's REPL tool. A worker in code mode calls the Inbox from
# inside REPL code, so REPL is the top-level tool the inbox-link hook sees; it is matched too.
REPL_TOOL_NAME = "REPL"
# REPL code reaches the Inbox tool through an accessor that may be namespaced
# (`tools.mcp__plannotator__plannotator_inbox`, `tools.plannotator.plannotator_inbox`, ...); code
# holding this text counts as using the Inbox.
PLANNOTATOR_INBOX_TOOL_MARKER = "plannotator_inbox"
# Looking up a desk session's Inbox thread by name (`plannotator inbox call read_thread`) lists
# this many of the project's open threads, and gives up after this many seconds so `check` and
# `send` never hang on the Inbox.
INBOX_LOOKUP_THREAD_LIMIT = 50
INBOX_LOOKUP_TIMEOUT_SECONDS = 10.0
# A send to a named session with no exact match asks the person to confirm a proposed session
# in the Slack thread (`pan propose`); a question left unanswered this long is dropped, and the
# next reply in the thread is handled as usual.
CONFIRMATION_TIMEOUT = timedelta(minutes=30)
# A proposal names at most this many sessions, numbered 1 to this.
CONFIRMATION_MAX_CANDIDATES = 3
# `pan inbox drain` keeps the items it drained (drained.jsonl, next to the thread map) so `pan send
# --to` and `pan done --discard` can read the person's own message by item id. Items older than
# this, and all but the newest DRAINED_LOG_MAX_ITEMS, are dropped at each drain.
DRAINED_LOG_RETENTION = timedelta(hours=24)
DRAINED_LOG_MAX_ITEMS = 1000

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

# pan's status message in a thread shows the task as a short label, cut to this many characters.
STATUS_LABEL_MAX_LENGTH = 80

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
# The commands worth listing to a phone user. The rest (gateway, watcher, service, hook, inbox,
# threads, spawn, slack-post) are launchd/dev-only, so they are dropped from the Slack listing;
# asking for one by name (`help inbox`) still renders it, so nothing is hard-blocked.
HELP_USER_FACING_COMMANDS: tuple[str, ...] = (
    "sessions",
    "check",
    "forget",
    "done",
    "relay",
    "read",
    "status",
    "stop",
)
# Slack renders a fenced block as monospace, and to_slack_mrkdwn passes a fence through verbatim,
# so the generated help keeps its column alignment.
CODE_FENCE = "```"
# What to say from Slack is not in Typer, so the auto-generated help cannot see it (help tech-spec
# R-1). The orchestrator reads each message for its meaning; these are the requests it knows.
SLACK_DIRECTIVE_FLAGS_FOOTER = """
The commands above are terminal commands (pan <command>). From Slack, mention @pan and say
what you want in your own words, for example:
  <task>                     in a new thread, start a session in a new worktree;
                             in a tied thread, send it to the tied session
  start a new session: ...   start a new session even in a tied thread (the tie moves)
  send to <name>: <task>     send to a live session (pan's or yours) if it is idle and tie
                             this thread to it. <name> must be typed exactly as its
                             address, name or Herdr label; otherwise pan proposes the
                             likely session (or up to 3) and sends only when you answer
  check                      post the tied session's latest answer here
  what's running             list live sessions, sent only to you
  forget                     untie this thread (the session keeps running)
  done [--discard]           close pan's session for this thread and remove its
                             worktree and branch; kept if it has uncommitted files or
                             unpushed commits; done --discard (the whole message) closes
                             it anyway; a session you started yourself is only untied
  ... reply in slack ...     also post the session's answers here at every turn end
  --stream <name>            name a new session's terminal
  relay <name> <message>     send a one-off message into a live session (ties nothing)
  read <name> [--full]       summarize what a live session is doing, or post its whole
                             transcript with --full
  help [command]             this help
Untagged replies count only in a thread pan is working in.
""".strip()


# Config keys pan once had and no longer reads. A config holding them still loads; load_config
# names them in a warning.
REMOVED_TOP_LEVEL_CONFIG_KEYS: tuple[str, ...] = ("defaults", "gated_ops")
REMOVED_USER_POLICY_KEYS: tuple[str, ...] = ("autonomy", "repos")


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


def _warn_about_removed_keys(raw_config: dict, config_path: Path) -> None:
    # Keys pan no longer reads. Named (never their values) so a person who set gated_ops or
    # autonomy learns they enforced nothing, and can delete them.
    removed_keys = [key for key in REMOVED_TOP_LEVEL_CONFIG_KEYS if key in raw_config]
    paths = raw_config.get("paths")
    if isinstance(paths, dict) and "logs" in paths:
        removed_keys.append("paths.logs")
    users = raw_config.get("users")
    if isinstance(users, dict):
        removed_keys.extend(
            f"users.*.{user_key}"
            for user_key in REMOVED_USER_POLICY_KEYS
            if any(isinstance(policy, dict) and user_key in policy for policy in users.values())
        )
    if removed_keys:
        logger.warning(
            f"config keys {', '.join(removed_keys)} are no longer used and are ignored (gated_ops "
            f"and autonomy never enforced anything); they can be deleted: {config_path}"
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
    _warn_about_removed_keys(raw_config, config_path)
    _expand_user_paths(raw_config)

    try:
        config = PanConfig.model_validate(raw_config)
    except ValidationError as error:
        raise ConfigMissingError(
            f"config file is missing required fields: {config_path}"
        ) from error

    logger.info(f"config loaded path={config_path}")
    return config
