# pan

`pan` is a Slack-driven laptop coding-agent orchestrator. You `@pan` a task from Slack (or your
phone); a thin always-on gateway authenticates you, adds a fast `:eyes:` reaction and appends the
raw request to an on-disk inbox (it decides nothing about the work). A filesystem watcher nudges a
persistent orchestrator Claude session, which drains the inbox, reads what you meant, and acts
through `pan` commands that hold the safety rules; it runs each new task as its own
Claude session in an isolated git worktree and its own herdr-core (`hc`) terminal. Each Slack thread
is tied to the session it started, so follow-ups in that thread go to the same session (a reply
without `@pan` reaches pan only in a thread pan is already working in; in any thread not currently
tied to a session or just started with `@pan`, mention `@pan`); say so in your own words to start a
new session, send to a named one, list, check, untie or close. When a session starts,
the thread gets one status message that pan keeps editing as the session works; the session asks
questions and reports progress in its Plannotator Inbox thread, whose link only you see. Slack hears
more when you write `@pan check`, or when the request said "reply in slack" (its answer is then
posted at every turn end). See [What Slack shows](#what-slack-shows).

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
  "users": {
    "U_YOURID": { "channels": ["*"] }
  },
  "paths": {
    "inbox": "~/.pan/inbox",
    "threads": "~/.pan/threads.json",
    "credentials": "~/.pan/credentials.json"
  }
}
```

- `orchestrator.terminal` is the hc terminal name of your persistent orchestrator Claude session
  (default `pan-orchestrator`); `orchestrator.cwd` is the folder it runs in. It reads the
  orchestrating skill installed with pan, by absolute path, which is outside that folder, so
  `orchestrator.command` must let it read that file without a prompt (for example through the
  session's permission mode or `--add-dir <pan install folder>`). `pan up` creates it under that name, so nothing needs pasting after
  a restart. Optional: `orchestrator.command` (default `["claude"]`) is the program it starts with,
  and `orchestrator.app_space` is a Herdr app space id (from `herdr-app ls`): when set, each worker
  appears as a tab in that space; when absent, workers stay hidden (watch any of them with
  `hc attach <terminal>`). pan never creates spaces.
- A config from before herdr-core (with `orchestrator.pane_id`) is rejected with a message naming
  the keys that replace it.
- `users` is your sender allowlist keyed by Slack user id; a mention from anyone not listed is
  dropped before it reaches the inbox. `channels: ["*"]` allows all channels.
- Run `pan config show` to print the resolved config with the credentials masked.

## Start the orchestrator

    pan up

`pan up` is idempotent and safe to run any time. It starts the orchestrator Claude session in its
hc terminal if it is not running (a fresh session that drains the inbox once on boot), marks workers
whose terminals are gone as `failed`, and wakes an orchestrator that is already running so any
backlog in the inbox is drained. herdr-core restores no agents after a Mac reboot, so run `pan up`
after one, or install the LaunchAgents below (`pan service install`) to do it at login. A worker lost
to a reboot keeps its worktree; a follow-up in its thread can reopen it there (`pan reopen`).

The boot brief and every wake name the installed orchestrating skill (`SKILL.md`) and its version
(a short hash of the file). To test a branch, run `uv tool install <checkout> --force` and then
`pan service install`: the restarted watcher's next wake carries the new version, and the running
orchestrator reads the branch's skill again before it drains.

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
awake for nothing. It is **not built yet**, so pan does not hold the machine awake today — keep the
lid open, or expect the machine to sleep.

## CLI

`pan` is a Typer toolbelt; the two long-runners are launchd-supervised, the rest are one-shot:

| Command | What it does |
| --- | --- |
| `pan gateway` | Run the always-on Bolt Socket Mode gateway (blocking). |
| `pan watcher` | Run the inbox watcher that nudges the orchestrator (blocking). |
| `pan service install\|uninstall\|status` | Install and load, remove, or inspect the gateway, watcher and up LaunchAgents (macOS). |
| `pan config set-token` | Write `~/.pan/credentials.json` at mode 0600 (prompts). |
| `pan config show` | Print the resolved config, credentials masked. |
| `pan inbox drain --json` | Atomically drain the inbox, emitting each item (with `mentioned`: false for a reply without `@pan`) with its cleaned text (mention, client signature and phone punctuation removed), its thread's tie and pan's open `pan propose` question in the thread. A reply from the requester after the question marks it answered and records its text, which `pan confirm --choice` needs. Every drained item is also kept for 24 hours (at most 1000) in `~/.pan/drained.jsonl`, so `--item` can find what the person typed. |
| `pan up` | Start the orchestrator if needed, mark lost workers failed, wake it (idempotent). |
| `pan spawn --thread … --task … --channel … [--thread-reply]` | Create worktree + hc terminal + worker; tie the thread to it; post the thread's status message and send the Inbox thread to the requester only. With `--thread-reply` (the message was a reply inside a Slack thread) the brief names the channel and thread ts and tells the session to read the whole thread first with its own Slack read-thread tool (`slack_read_thread`), only to read. |
| `pan reopen --thread … --task … [--item …] [--reply-in-slack]` | Reopen a finished or lost worker in its worktree, continuing its conversation; the thread's status message says Working again. `--reply-in-slack` switches the thread to posting answers in Slack. A thread from X (its record, or the `--item` pan drained) exits 35. |
| `pan threads get --thread …` | Print a thread's `ThreadRecord` as JSON. |
| `pan threads set --thread … --status …` | Set a thread's `ThreadRecord` status. |
| `pan slack-post --thread … --channel … --text …` | Post to a thread through the single Slack egress. |
| `pan status --thread …` | Report a worker's live status (from its hc terminal). |
| `pan sessions [--json] [--show <address,…> --thread … --channel … --requester …]` | List every live hc terminal with the last thing each was asked, marking pan-owned ones and status drift. With `--show` and the three thread options, send the requester alone the sessions at those addresses (the orchestrator's pick, in its order, at most 5), each with its state and a link to its thread. Exit 32: the private message failed, and nothing was posted. |
| `pan check --thread … --channel …` | Bring the thread's status message up to date, post the tied session's latest answer as its own message (its transcript's last answer, or its screen lines; nothing when there is neither), and send its Inbox thread and terminal to the requester only. |
| `pan send --thread … --channel … [--to <name> --item …] --task … [--reply-in-slack]` | Send a task to a live session (pan's own or one of yours) and tie the thread to it; post the thread's status message (Working) and send the Inbox thread to the requester only. `--to` must match exactly (address, name, or Herdr label or space) and appear, as a whole word or phrase in any letter case, in the person's message pan drained as `--item <item id>` (the requester's newest in this thread; a later message from someone else does not count); otherwise nothing is sent and it exits 28. A thread from X exits 35. Without `--to`, send a follow-up to the thread's tied session. `--reply-in-slack` switches the thread to posting answers in Slack (on a follow-up it stays on for the thread; leaving it off never turns it off). A busy session (or one whose state pan cannot read, except a brand-new Claude session that has not answered yet) is refused with "<name> is busy; try again, or @pan check"; nothing is typed. |
| `pan propose --thread … --channel … --to … --task … --requester … --item … [--candidate <address> …]` | After `pan send` exits 28: ask in the thread whether to send the task to the proposed live session ("Send this to <session>? Reply yes.") or which of up to three ("Which session? Reply 1, 2 or 3"), storing the question. Candidates that are not live terminal addresses are dropped; with none left it says no session matches. Types nothing. `--item` must be the requester's newest message pan drained in the thread, else nothing is asked and it exits 28; a thread from X exits 35. |
| `pan confirm --thread … --channel … --choice <yes\|1-3>` / `--cancel` | Act on the requester's reply to that question, as the orchestrator read it: send the stored task to the chosen session (still live, same name) and tie the thread, or drop the question and post "Not sent.". `--choice` exits 34, sending and posting nothing, until the drain has seen the requester reply in the thread since pan last asked, when that reply declines ("no", "none of those"), when `yes` is chosen but the reply is not a plain yes (a yes word with at most "please", "thanks", "go ahead" or "that one" after it), or when a number is chosen but the reply does not start with it ("2", "2 please", "#2"). Re-asking, and refusing a busy or waiting session, clear the reply and count as asking again. A question stays open 30 minutes from when pan last asked; one stored before pan kept the requester is never answered and expires. |
| `pan forget --thread … --channel …` | Untie the thread from its session (never stops it); say so in the thread. |
| `pan done --thread … --channel … [--discard --item …]` | Close the thread's session: remove its terminal, its worktree and its branch, mark it done and untie the thread. Keeps everything (exit 29) while the worktree has uncommitted files or the branch unpushed commits, and refuses while the session is working (exit 26) or waiting on a prompt (exit 23), unless `--discard`. `--discard` needs `--item`: the thread requester's newest message pan drained in this thread, exactly `done --discard` (a phone's `—discard` counts); otherwise nothing is touched and it exits 33. Exit 30: a git cleanup failed (retry with `done`); exit 31: a folder pan never removes. A session pan did not start is only untied. See [Closing a session](#closing-a-session). |
| `pan relay <target> <message> --thread … --channel … --item …` | Send a message into a live terminal (address, name, or Herdr app label). The target must be named in the sender's newest message in the thread (`--item`), else exit 28; a thread from X exits 35. |
| `pan read <target> [--full]` | Print a terminal's recent output, or the full transcript. |
| `pan stop --thread …` | Kill switch: kill and remove the worker's terminal, mark the thread FAILED and edit its status message to Stopped. Refuses for a session pan did not start: it only unties the thread. |
| `pan hook stop` / `pan hook notification` | Claude Code worker hooks: mark the thread idle / blocked and edit its status message to Finished / Waiting for you (with the prompt's text); the stop hook also posts the answer, alone, when the request said "reply in slack". Only a prompt waiting on the person (a permission prompt) marks blocked; the idle reminder Claude Code sends after a minute changes nothing. |
| `pan hook inbox-link` | Claude Code worker PostToolUse hook on the Inbox tool (records the session's Inbox thread id and link). |
| `pan hook session-start` | Claude Code worker SessionStart hook (records the session id and transcript path). |

When pan cannot use the `--item` given to `send --to`, `propose`, `relay` or `done --discard`, it
posts "I couldn't act on that message; please send the request again." in the thread.

## What Slack shows

pan keeps a thread readable for everyone in the channel:

- **One status message per thread**, edited in place: *Working* (with the task, cut short), *Waiting
  for you* (a prompt waits at the terminal), *Finished* (the turn ended), *Closed* (`@pan done`) or
  *Stopped* (the session failed or was stopped). Small grey text under it names the session, how
  long the task has run and when the status was last updated (in each reader's own time zone). If
  the message was deleted or cannot be edited, pan posts a new one.
- **The answer as its own message**, converted from Markdown to Slack's format (links, bold,
  headings as bold lines, bullets, code blocks, strikethrough), with no session name, Inbox link or
  "has finished" line around it. Long answers are split into numbered parts. The same answer is
  never posted twice: `@pan check` after a reply-in-Slack turn only updates the status.
- **Private details only to you.** The session's Plannotator Inbox thread (its link is on your
  machine, so it means nothing to colleagues), its name and the terminal address arrive as a
  message only the requester can see, when the session starts and on `@pan check`. pan never puts
  a `localhost` link in a normal message.
- **Refusals and questions stay one short line**: busy, waiting on a prompt, not found, "Send this
  to ...? Reply yes.".

- **No local paths in public posts.** Every message and status edit pan posts in a thread has
  paths under `/Users`, `/home` or `~` and `file://` URLs cut to their last part (`.../pan-7`),
  in code too. A path continues past a space only after a folder name with no file extension
  and into a run holding a `/` that starts no new path or URL (`/Users/me/My Project/x.py` becomes
  `.../x.py`). A link to a local file shows only its label; web links and mentions keep their
  target with paths cut from their label. The private
  details sent only to you are not cut.

These use `chat:write` only (posting, editing and ephemeral messages); no new scope is needed.

Exit codes of the `pan` commands: 10 unauthorized sender, 11 config missing, 12 credentials, 13
inbox, 14 thread not found, 15 spawn, 16 terminal host, 17 Slack post, 20
target not found, 21 target ambiguous, 22 terminal gone, 23 waiting on a prompt, 24 terminal name
taken, 25 not a pan session, 26 busy, 27 service, 28 no exact match (propose instead), 29 unsaved
work, 30 git cleanup failed, 31 unsafe worktree, 32 private message failed, 33 `--discard` not typed
by the person, 34 open question not answered by the requester, 35 a thread from X may only start new
sessions.

## What to say in Slack

Mention `@pan` and say what you want in your own words. The orchestrator Claude session reads each
message for its meaning and picks the action; the `pan` commands it runs do the work and keep the
safety rules, whatever it decides. When it is unsure, it asks you in the thread.

| Say, for example | What pan does |
| --- | --- |
| `<task>` | In a new or untied thread, start a new session in a new worktree. In a tied thread, send it to the tied session. |
| `start a new session: <task>` | Start a new session even in a tied thread; the tie moves to it, the old session keeps running. |
| `to <name>: <task>` | Send the task to a live session, pan's own or one of your desk sessions, and tie the thread to it. It is sent at once only when `<name>` exactly matches a session's address, terminal name, or Herdr label or space, and you typed it. Otherwise pan sends nothing: it proposes the session you most likely meant ("Send this to stream-pan (work / 1, folder pan)? Reply yes.") or up to three ("Which session? Reply 1, 2 or 3:"), and sends only when you, the person who asked, answer in the thread within 30 minutes with `yes` or the number; anyone else's reply never sends. With no likely session it says how to start new work. A busy session gets "<name> is busy; try again, or @pan check" and nothing is typed. Not available to messages from X. |
| `check`, `any news?` | Post the tied session's latest answer here. |
| `what's running`, `sessions ocr` | Show you alone up to 5 sessions, each with its state and a link to its thread: the ones you most likely mean, or all of them, waiting on you and working first. |
| `forget` | Untie the thread; the session keeps running. |
| `done` / `done --discard` | Close the thread's session and remove its worktree and branch; see [Closing a session](#closing-a-session). Unsaved work is thrown away only when your whole message is `done --discard`. |
| `reply in slack`, `post it here` | Also post the session's answers here at every turn end. |
| `--stream <name>` | Name a new session's terminal. |
| `relay <name> <message>` | Send a one-off message into any live session; ties nothing. |
| `read <name> [--full]` | Summarize what a live session is doing, or post its whole transcript; ties nothing. |
| `help [command]` | The CLI help, answered at once by the gateway. |

### Closing a session

`@pan done` in a thread closes the session tied to it, once its work is saved:

- In a thread with no tied session, pan replies "This thread has no session." and does nothing.
- A session pan did not start (one of your desk sessions) is only untied: "Untied this thread from
  desk-x; pan never closes sessions it didn't start."
- A session pan started that is still working, or waiting on a prompt, is refused: "pan-7 is still
  working; send `@pan done` when it finishes, or `@pan done --discard` to stop it now."
- pan then looks at the session's worktree and branch (read-only). Work counts as unsaved when the
  worktree has uncommitted or untracked files (ignored files, and the untracked
  `.claude/settings.json` pan writes there at spawn, do not count; a tracked settings file the
  session changed does), or the branch has commits its upstream lacks. An upstream deleted after a
  merge (shown as gone after `git fetch --prune`) counts as holding them; with no upstream, commits
  merged into neither the repo's default branch (`origin/HEAD`) nor the branch the main checkout is
  on (the one pan created the worktree from) count. With unsaved work pan keeps everything: "pan-7
  has 2 uncommitted files and 1 unpushed commit; kept it. Push or commit them, or send `@pan done
  --discard`."
- Otherwise pan removes the session's terminal (if it is still there), removes the worktree
  (forced only with `--discard` or when pan's settings file is all that is left) and deletes the
  branch (`git branch -D` with `--discard`, when it has no commits outside the base branches, or
  when its upstream is gone; else `-d`), then marks the thread's record done and unties it:
  "Closed pan-7 and removed its worktree and branch."
- If the worktree folder is already gone, pan prunes the repository's record of it and deletes the
  branch under the same rules, judging the branch's commits. It finds the repository from the
  record (pan stores it at spawn); for records from before that, it uses the orchestrator's folder
  only if that repository has the branch. The reply says only what it removed.
- If a git command fails (exit 30), the thread stays tied with its status unchanged, so `@pan done`
  again finishes the cleanup. A folder outside pan's worktree folder, or a main checkout, is never
  removed (exit 31).

`done --discard` skips the busy and unsaved-work checks. `pan stop` is still the kill switch: it
marks the thread failed and leaves the worktree alone.

### Your own sessions

A named session can be any live session, including desk sessions pan did not start. pan types the task
only when the session is idle (busy, or a state pan cannot read, is refused; waiting on a prompt is
refused too), with one line on top saying it came from Slack via pan and naming the Plannotator Inbox
thread to use. When pan knows the session's thread (pan found it earlier) the line names it;
otherwise it says to keep using the session's existing thread for this work, or to start one named
`<name>: <short task>`.

The orchestrator only proposes a session it is not sure of: `pan propose` keeps only live terminal
addresses, the question is stored per thread in `~/.pan/confirmations.json` (next to the thread
map), `pan inbox drain` marks it answered when you reply in the thread, and `pan confirm` sends only
after that mark and checks the session is still the same live terminal before typing.

Follow-ups in the thread go to the same session with the same checks. pan never stops, removes or reopens a desk session: `pan stop`
only unties it, and when it has ended pan says so and suggests starting new work or naming another session. Desk sessions
have no pan hooks, so `check` looks their Inbox thread up by name (`plannotator inbox call
read_thread`, read only); when it is not found, only the name is shown.

## First task

From Slack (or your phone), in a channel where the app is present:

```
@pan create /tmp/pan-hello.txt containing today's date, then tell me the contents
```

You should see the `:eyes:` ack within about a second, then a status message saying *Working*, and
a message only you can see naming the session's Plannotator Inbox thread. The worker runs in its own git worktree and hc terminal and reports in
that Inbox thread; add "reply in slack" to the message to also get its answer in the Slack thread. A
follow-up reply in the same thread goes to the same worker, with or without `@pan`, because pan is
already working in that thread; `@pan check` posts its latest answer. A reply without `@pan` in any
thread not currently tied to a session or just started with `@pan` is ignored: a thread pan never
worked in, or one pan untied, by `@pan done`, `forget`, moving its session to another thread, or a
failed start.
When you start a session with a reply inside an existing thread, its brief tells it to read that
whole thread first with its own Slack read-thread tool, so it sees the earlier messages; pan does
not copy the thread anywhere.
