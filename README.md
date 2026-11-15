# pan

`pan` is a Slack-driven laptop coding-agent orchestrator. You `@pan` a task from Slack (or your
phone); a thin always-on gateway authenticates you, adds a fast `:eyes:` reaction, and appends the
request to an on-disk inbox. A filesystem watcher nudges a persistent orchestrator Claude session,
which drains the inbox, parses deterministic directive flags, decides reuse-vs-spawn, and runs each
task as its own Claude session in an isolated git worktree and its own herdr-core (`hc`) terminal. Workers post
progress and results back to the originating Slack thread.

The gateway and watcher are the two always-on processes; everything else runs on demand.

## Requirements

- macOS (the always-on layer is launchd/`pmset`-specific; the gateway itself is portable).
- [`uv`](https://docs.astral.sh/uv/) for install and dependency management.
- herdr-core (`hc`) on `PATH`, the terminal runtime that keeps the orchestrator and workers alive
  (the Herdr app is optional and only for watching them), plus `git` and the `claude` CLI.
- A Slack workspace where you can create an app.

## Install

`pan` is installed as a `uv` tool so the `pan` binary lands on your `PATH`:

```sh
command -v pan || uv tool install .
```

Run the quality gates during development with `uv run` (never `uv run` the finished CLI — use the
installed `pan` binary):

```sh
uv run ruff check && uv run ruff format --check && uv run ty check && uv run pytest -q
```

## Configure

### 1. Create the Slack app

Create a new Slack app **from an app manifest** and paste `slack/manifest.yaml`. It requests only
the scopes pan needs (`app_mentions:read`, `chat:write`, `reactions:write`, and the `*history`
scopes to read thread replies) and enables Socket Mode, so no public URL is needed behind your
laptop's NAT. Install the app to your workspace, then collect two tokens:

- the **bot token** (`xoxb-...`) from *OAuth & Permissions*, and
- an **app-level token** (`xapp-...`, scope `connections:write`) from *Basic Information → App-Level
  Tokens* — this one is required for Socket Mode.

### 2. Store the tokens

Tokens are kept out of the config file, in `~/.pan/credentials.json` written at mode `0600` (this is
the deliberate, portable alternative to the macOS keychain). Never pass them any other way:

```sh
pan config set-token   # prompts (hidden) for the bot token and the app token
```

### 3. Write the config

Config is JSON at `~/.config/pan/config.json` and contains **no secrets**. A minimal config:

```json
{
  "slack": { "socket_mode": true },
  "orchestrator": {
    "terminal": "pan-orchestrator",
    "cwd": "~/dev/pan",
    "worktree_base": "~/dev/pan-worktrees"
  },
  "defaults": { "agent": "claude", "permission_mode": "bypass", "repo_allowlist": [] },
  "users": {
    "U_YOURID": { "autonomy": "full", "channels": ["*"], "repos": ["*"] }
  },
  "gated_ops": [],
  "paths": {
    "inbox": "~/.pan/inbox",
    "threads": "~/.pan/threads.json",
    "logs": "~/.pan/logs",
    "credentials": "~/.pan/credentials.json"
  }
}
```

- `orchestrator.terminal` is the hc terminal name of your persistent orchestrator Claude session
  (default `pan-orchestrator`); `orchestrator.cwd` is the folder it runs in (this repo, so it can
  read the orchestrating skill). `pan up` creates it under that name, so nothing needs pasting after
  a restart. Optional: `orchestrator.command` (default `["claude"]`) is the program it starts with,
  and `orchestrator.app_space` is a Herdr app space id (from `herdr-app ls`): when set, each worker
  appears as a tab in that space; when absent, workers stay hidden (watch any of them with
  `hc attach <terminal>`). pan never creates spaces.
- A config from before herdr-core (with `orchestrator.pane_id`) is rejected with a message naming
  the keys that replace it.
- `users` is your sender allowlist keyed by Slack user id; a mention from anyone not listed is
  dropped before it reaches the inbox. `channels: ["*"]` allows all channels. `gated_ops: []` leaves
  the approval gate dormant (populate it later — e.g. `git push`, `rm -rf` — to require approvals
  with no code change).
- Run `pan config show` to print the resolved config with the credentials masked.

## Start the orchestrator

    pan up

`pan up` is idempotent and safe to run any time. It starts the orchestrator Claude session in its
hc terminal if it is not running (a fresh session that drains the inbox once on boot), marks workers
whose terminals are gone as `failed`, and wakes an orchestrator that is already running so any
backlog in the inbox is drained. herdr-core restores no agents after a Mac reboot, so run `pan up`
after one, or install the optional `com.pan.up` LaunchAgent below to do it at login. A worker lost
to a reboot keeps its worktree; a follow-up in its thread can reopen it there (`pan reopen`).

## Run always-on (launchd)

Two LaunchAgents keep the gateway and watcher up across sleep/wake, each `KeepAlive=true` and
`RunAtLoad=true`. A third, optional one (`com.pan.up`) runs `pan up` once at login and is retried by
launchd only if it fails (for example while the hc core is not reachable yet). Templates live in
`launchd/`; each uses a single `<PAN_HOME>` placeholder for your home directory.

```sh
mkdir -p ~/Library/LaunchAgents ~/.pan/logs
for svc in gateway watcher up; do
  sed "s|<PAN_HOME>|$HOME|g" "launchd/com.pan.$svc.plist.template" \
    > "$HOME/Library/LaunchAgents/com.pan.$svc.plist"
  launchctl load "$HOME/Library/LaunchAgents/com.pan.$svc.plist"
done
```

The templates assume the `pan` and `hc` binaries are in `<PAN_HOME>/.local/bin` (where `uv tool
install` and the herdr-core installer put them) and set `PATH` to include it, since launchd's default
`PATH` does not; adjust if your install prefix differs. To stop the services, `launchctl unload` the
plists — that is the hard off switch. For interactive debugging instead of launchd, run them in hc
terminals: `hc run --bg --no-parent --name pan-gateway pan gateway` (and the same for the watcher).

### Lid-closed execution

The intended lid-closed story is the proven `caffeinate` + `pmset disablesleep 1` pattern, engaged
**only on AC power and only while at least one worker is active**, so the machine is never forced
awake for nothing. `src/pan/power.py` provides the policy predicate for that — `should_caffeinate`
returns True exactly when on AC and at least one worker is running (R-3). The actuation that calls
`caffeinate`/`pmset` off this decision is **not yet wired** (it is the deferred, live-verified part
of the always-on work), so lid-closed execution does not hold the machine awake today — keep the lid
open, or expect the machine to sleep. On battery it may sleep regardless; plug in for lid-closed
runs once the actuation lands.

## CLI

`pan` is a Typer toolbelt; the two long-runners are launchd-supervised, the rest are one-shot:

| Command | What it does |
| --- | --- |
| `pan gateway` | Run the always-on Bolt Socket Mode gateway (blocking). |
| `pan watcher` | Run the inbox watcher that nudges the orchestrator (blocking). |
| `pan config set-token` | Write `~/.pan/credentials.json` at mode 0600 (prompts). |
| `pan config show` | Print the resolved config, credentials masked. |
| `pan inbox drain --json` | Atomically drain the inbox, emitting each item with its parsed directive. |
| `pan up` | Start the orchestrator if needed, mark lost workers failed, wake it (idempotent). |
| `pan spawn --thread … --task … --channel …` | Create worktree + hc terminal + worker; record the thread; post the ack. |
| `pan reopen --thread … --task …` | Reopen a finished or lost worker in its worktree, continuing its conversation. |
| `pan threads get --thread …` | Print a thread's `ThreadRecord` as JSON. |
| `pan slack-post --thread … --channel … --text …` | Post to a thread through the single Slack egress. |
| `pan status --thread …` | Report a worker's live status (from its hc terminal). |
| `pan sessions [--json]` | List every live hc terminal, marking pan-owned ones and status drift. |
| `pan relay <target> <message>` | Send a message into a live terminal (address, name, or Herdr app label). |
| `pan read <target> [--full]` | Print a terminal's recent output, or the full transcript. |
| `pan stop --thread …` | Kill switch: kill and remove the worker's terminal and mark the thread FAILED. |
| `pan pause` | Toggle the pause flag. |
| `pan hook stop` / `pan hook notification` | Claude Code worker completion-hook entrypoints (post the result / question, transition status). |
| `pan hook session-start` | Claude Code worker SessionStart hook (records the session id and transcript path). |

## Directive flags

The task text after `@pan` is parsed deterministically (never by model judgment):

- no flag → **delegate** (ack now, post the result later);
- `--sync` or a leading `!` → **sync** (block the item until the worker's Stop hook);
- `--status` → **status** (answer from the worker's hc terminal; touches no worker);
- `--new` → force a fresh worker even if the thread already has one;
- `--stream <name>` → target/name the worker stream;
- `--agent <x>` → parsed but reserved for a later version.

## First task

From Slack (or your phone), in a channel where the app is present:

```
@pan create /tmp/pan-hello.txt containing today's date, then tell me the contents
```

You should see the `:eyes:` ack within about a second, a worker spawn in its own git worktree and
hc terminal, the file created, and its contents posted back in the thread. A follow-up reply in
the same thread routes to the same worker; `@pan --status` reports its live state.
