# pan

`pan` is a Slack-driven laptop coding-agent orchestrator. You `@pan` a task from Slack (or your
phone); a thin always-on gateway authenticates you, adds a fast `:eyes:` reaction, reads the route
words at the start of the message (pure string parsing; it decides nothing about the work), and
appends the request to an on-disk inbox. A filesystem watcher nudges a persistent orchestrator Claude
session, which drains the inbox, follows the route, and runs each new task as its own
Claude session in an isolated git worktree and its own herdr-core (`hc`) terminal. Each Slack thread
is tied to the session it started, so follow-ups in that thread go to the same session; route words
(`new:`, `to <name>:`, `check`, `sessions`, `forget`) let you choose otherwise. When a session starts,
Slack gets one notice naming it and its Plannotator Inbox thread; the session then asks questions and
reports progress in the Inbox. Slack hears more when you write `@pan check`, or when the request said
"reply in slack" (its answer is then posted at every turn end).

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
after one, or install the LaunchAgents below (`pan service install`) to do it at login. A worker lost
to a reboot keeps its worktree; a follow-up in its thread can reopen it there (`pan reopen`).

## Run always-on (launchd)

Two LaunchAgents keep the gateway and watcher up across sleep/wake, each `KeepAlive=true` and
`RunAtLoad=true`. A third (`com.pan.up`) runs `pan up` once at login and is retried by launchd only
if it fails (for example while the hc core is not reachable yet). Install all three, so pan starts by
itself after a login or reboot:

    pan service install     # write ~/Library/LaunchAgents/com.pan.{gateway,watcher,up}.plist and load them
    pan service status      # per agent: plist installed, loaded, pid, last exit code
    pan service uninstall   # unload them and delete the plists (the hard off switch)

`install` fills each template with your home directory and the absolute path of the `pan` on your
`PATH` (falling back to `~/.local/bin/pan`, where `uv tool install` puts it), creates `~/.pan/logs`
for the agents' output (`<name>.out.log`, `<name>.err.log`), and loads the agents with `launchctl
bootstrap gui/<uid>` in the order gateway, watcher, up. Running it again reinstalls (each loaded
agent is booted out first), so run it again after moving the `pan` install. The plists set `PATH` to
include `~/.local/bin`, where the herdr-core installer puts `hc`, since launchd's default `PATH` does
not.

Manual fallback: the templates ship in the package at `src/pan/launchd/`, with a `<PAN_HOME>`
placeholder for your home directory and `<PAN_BIN>` for the `pan` executable:

```sh
mkdir -p ~/Library/LaunchAgents ~/.pan/logs
for svc in gateway watcher up; do
  sed -e "s|<PAN_HOME>|$HOME|g" -e "s|<PAN_BIN>|$(command -v pan)|g" \
    "src/pan/launchd/com.pan.$svc.plist.template" > "$HOME/Library/LaunchAgents/com.pan.$svc.plist"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.pan.$svc.plist"
done
```

For interactive debugging instead of launchd, run them in hc terminals: `hc run --bg --no-parent
--name pan-gateway pan gateway` (and the same for the watcher).

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
| `pan service install\|uninstall\|status` | Install and load, remove, or inspect the gateway, watcher and up LaunchAgents (macOS). |
| `pan config set-token` | Write `~/.pan/credentials.json` at mode 0600 (prompts). |
| `pan config show` | Print the resolved config, credentials masked. |
| `pan inbox drain --json` | Atomically drain the inbox, emitting each item with its route, its parsed directive and its thread's tie; a `yes`/1-3 reply to an open `pan propose` question becomes a `confirm` route, and any other message there is marked `cancels_confirmation`. |
| `pan up` | Start the orchestrator if needed, mark lost workers failed, wake it (idempotent). |
| `pan spawn --thread … --task … --channel … [--thread-reply]` | Create worktree + hc terminal + worker; tie the thread to it; post the start notice naming its Inbox thread. With `--thread-reply` (the message was a reply inside a Slack thread) the brief names the channel and thread ts and tells the session to read the whole thread first with its own Slack read-thread tool (`slack_read_thread`), only to read. |
| `pan reopen --thread … --task …` | Reopen a finished or lost worker in its worktree, continuing its conversation. |
| `pan threads get --thread …` | Print a thread's `ThreadRecord` as JSON. |
| `pan slack-post --thread … --channel … --text …` | Post to a thread through the single Slack egress. |
| `pan status --thread …` | Report a worker's live status (from its hc terminal). |
| `pan sessions [--json] [--thread … --channel …]` | List every live hc terminal with the last thing each was asked, marking pan-owned ones and status drift; with `--thread` and `--channel`, post the table to that thread. |
| `pan check --thread … --channel …` | Post the tied session's latest answer to the thread as is (its transcript's last answer, or its screen lines), with its Inbox thread and whether it is still working or waiting on a prompt. |
| `pan send --thread … --channel … [--to <name>] --task …` | Send a task to a live session (pan's own or one of yours) and tie the thread to it; post "Sent to …" with its Inbox thread. `--to` must match exactly (address, name, or Herdr label or space); otherwise nothing is sent or posted and it exits 28. Without `--to`, send a follow-up to the thread's tied session. A busy session (or one whose state pan cannot read, except a brand-new Claude session that has not answered yet) is refused with "<name> is busy; try again, or @pan check"; nothing is typed. |
| `pan propose --thread … --channel … --to … --task … --candidate <address> …` | After `pan send` exits 28: ask in the thread whether to send the task to the proposed live session ("Reply yes") or which of up to three ("Reply 1, 2 or 3"), storing the question; with no candidate, say no session matches. Types nothing. |
| `pan confirm --thread … --channel … --choice <yes\|n>` / `--cancel` | Act on the reply to that question: send the stored task to the chosen session (still live, same name) and tie the thread, or drop the question and post "Not sent.". |
| `pan forget --thread … --channel …` | Untie the thread from its session (never stops it); say so in the thread. |
| `pan relay <target> <message>` | Send a message into a live terminal (address, name, or Herdr app label). |
| `pan read <target> [--full]` | Print a terminal's recent output, or the full transcript. |
| `pan stop --thread …` | Kill switch: kill and remove the worker's terminal and mark the thread FAILED. Refuses for a session pan did not start: it only unties the thread. |
| `pan pause` | Toggle the pause flag. |
| `pan hook stop` / `pan hook notification` | Claude Code worker hooks: mark the thread idle / blocked; post the answer / question only when the request said "reply in slack". Only a prompt waiting on the person (a permission prompt) marks blocked; the idle reminder Claude Code sends after a minute changes nothing. |
| `pan hook inbox-link` | Claude Code worker PostToolUse hook on the Inbox tool (records the session's Inbox thread id and link). |
| `pan hook session-start` | Claude Code worker SessionStart hook (records the session id and transcript path). |

## Route words and flags

The text after `@pan` is parsed deterministically, never by model judgment. A route word must be the
first word after the mention, spelled exactly, in any letter case; anything else is task text.

| Say | What pan does |
| --- | --- |
| `<task>` | In a new or untied thread, start a new session in a new worktree. In a tied thread, send it to the tied session. |
| `new: <task>` | Start a new session even in a tied thread; the tie moves to it, the old session keeps running. The colon is required ("new login page" is a plain task). |
| `to <name>: <task>` | Send the task to a live session, pan's own or one of your desk sessions, and tie the thread to it. `<name>` (one word) that exactly matches a session's address, terminal name, or Herdr label or space sends at once. Otherwise pan sends nothing: it proposes the session you most likely meant ("Send this to stream-pan (work / 1, folder pan)? Reply yes.") or up to three ("Which session? Reply 1, 2 or 3:"), and sends only when you reply `yes` or a number in the thread within 30 minutes; `no` (or `n`, `cancel`, `stop`) declines with "Not sent." and nothing more; any other reply cancels ("Not sent.") and is then handled as a normal message. With no likely session it says how to start new work with `new:`; pan never starts a session from `to`. A busy session gets "<name> is busy; try again, or @pan check" and nothing is typed. Not available to messages from X. |
| `check` | Post the tied session's latest answer here. The whole message must be just this word. |
| `sessions` | List live sessions: name, idle or busy, last ask, and the Inbox thread when pan knows it. Also `--sessions`. |
| `forget` | Untie the thread; the session keeps running. |
| `reply in slack` (anywhere) | Also post the session's answers here at every turn end. The orchestrator also turns this on when the message plainly asks for the result in the thread ("share it here", "post it here", "send the link here", "reply here"); this is its one judgment about the reply mode, and it never changes the route. |
| `--stream <name>` | Name a new session's terminal. |
| `relay <target> <message>` | Send a one-off message into any live session; ties nothing. |
| `read <target> [--full]` | Read a live session's recent output (or all of it); ties nothing. |

### Your own sessions

`to <name>:` reaches any live session, including desk sessions pan did not start. pan types the task
only when the session is idle (busy, or a state pan cannot read, is refused; waiting on a prompt is
refused too), with one line on top saying it came from Slack via pan and naming the Plannotator Inbox
thread to use. When pan knows the session's thread (pan found it earlier) the line names it;
otherwise it says to keep using the session's existing thread for this work, or to start one named
`<name>: <short task>`.

The proposal is the one place the orchestrator Claude judges a target. It only proposes: `pan
propose` keeps only live terminal addresses, the question is stored per thread in
`~/.pan/confirmations.json` (next to the thread map), and on your `yes` or number `pan confirm`
checks the session is still the same live terminal before typing.

Follow-ups in the thread go to the same session with the same checks. pan never stops, removes or reopens a desk session: `pan stop`
only unties it, and when it has ended pan says so and suggests `new:` or `to <name>:`. Desk sessions
have no pan hooks, so `check` looks their Inbox thread up by name (`plannotator inbox call
read_thread`, read only); when it is not found, only the name is shown.

`--new`, `--status`, `--sync` and a leading `!` are retired: pan replies saying what replaced them
(`new:`, `check` or `sessions`, and `reply in slack`). `--agent <x>` is parsed but reserved for a
later version.

## First task

From Slack (or your phone), in a channel where the app is present:

```
@pan create /tmp/pan-hello.txt containing today's date, then tell me the contents
```

You should see the `:eyes:` ack within about a second, then a start notice naming the worker and
its Plannotator Inbox thread. The worker runs in its own git worktree and hc terminal and reports in
that Inbox thread; add "reply in slack" to the message to also get its answer in the Slack thread. A
follow-up reply in the same thread goes to the same worker; `@pan check` posts its latest answer.
When you start a session with a reply inside an existing thread, its brief tells it to read that
whole thread first with its own Slack read-thread tool, so it sees the earlier messages; pan does
not copy the thread anywhere.
