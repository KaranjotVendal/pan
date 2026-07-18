# pan

`pan` is a Slack-driven laptop coding-agent orchestrator. You `@pan` a task from Slack (or your
phone); a thin always-on gateway authenticates you, adds a fast `:eyes:` reaction, and appends the
request to an on-disk inbox. A filesystem watcher nudges a persistent orchestrator Claude session,
which drains the inbox, parses deterministic directive flags, decides reuse-vs-spawn, and runs each
task as its own Claude session in an isolated git worktree plus herdr workspace. Workers post
progress and results back to the originating Slack thread.

The gateway and watcher are the two always-on processes; everything else runs on demand.

## Requirements

- macOS (the always-on layer is launchd/`pmset`-specific; the gateway itself is portable).
- [`uv`](https://docs.astral.sh/uv/) for install and dependency management.
- [`herdr`](https://github.com/badlogic/herdr) and [`morcli`](https://github.com/badlogic/morcli)
  on `PATH` (the worker session manager and its observability CLI), plus `git` and the `claude` CLI.
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
    "workspace_name": "pan-orchestrator",
    "pane_id": "%<your-orchestrator-pane-id>",
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

- `orchestrator.pane_id` is the herdr pane of your persistent orchestrator Claude session. Create
  that workspace/pane in herdr first, then paste its id here.
- `users` is your sender allowlist keyed by Slack user id; a mention from anyone not listed is
  dropped before it reaches the inbox. `channels: ["*"]` allows all channels. `gated_ops: []` leaves
  the approval gate dormant (populate it later — e.g. `git push`, `rm -rf` — to require approvals
  with no code change).
- Run `pan config show` to print the resolved config with the credentials masked.

## Run always-on (launchd)

Two LaunchAgents keep the gateway and watcher up across sleep/wake, each `KeepAlive=true` and
`RunAtLoad=true`. Templates live in `launchd/`; each uses a single `<PAN_HOME>` placeholder for your
home directory.

```sh
mkdir -p ~/Library/LaunchAgents ~/.pan/logs
for svc in gateway watcher; do
  sed "s|<PAN_HOME>|$HOME|g" "launchd/com.pan.$svc.plist.template" \
    > "$HOME/Library/LaunchAgents/com.pan.$svc.plist"
  launchctl load "$HOME/Library/LaunchAgents/com.pan.$svc.plist"
done
```

The templates assume the `pan` binary is at `<PAN_HOME>/.local/bin/pan` (where `uv tool install`
puts it); adjust `ProgramArguments` if your install prefix differs. To stop the services, `launchctl
unload` the two plists — that is the hard off switch.

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
| `pan spawn --thread … --task … --channel …` | Create worktree + workspace + worker; record the thread; post the ack. |
| `pan threads get --thread …` | Print a thread's `ThreadRecord` as JSON. |
| `pan slack-post --thread … --channel … --text …` | Post to a thread through the single Slack egress. |
| `pan status --thread …` | Report a worker's live status (via morcli). |
| `pan stop --thread …` | Kill switch: kill the worker's pane(s) and mark the thread FAILED. |
| `pan pause` | Toggle the pause flag. |
| `pan hook stop` / `pan hook notification` | Claude Code worker completion-hook entrypoints (post the result / question, transition status). |

## Directive flags

The task text after `@pan` is parsed deterministically (never by model judgment):

- no flag → **delegate** (ack now, post the result later);
- `--sync` or a leading `!` → **sync** (block the item until the worker's Stop hook);
- `--status` → **status** (answer from morcli; touches no worker);
- `--new` → force a fresh worker even if the thread already has one;
- `--stream <name>` → target/name the worker stream;
- `--agent <x>` → parsed but reserved for a later version.

## First task

From Slack (or your phone), in a channel where the app is present:

```
@pan create /tmp/pan-hello.txt containing today's date, then tell me the contents
```

You should see the `:eyes:` ack within about a second, a worker spawn in its own git worktree +
herdr workspace, the file created, and its contents posted back in the thread. A follow-up reply in
the same thread routes to the same worker; `@pan --status` reports its live state.
