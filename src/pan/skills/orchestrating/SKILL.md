---
name: orchestrating
description: Use on every inbox nudge in the persistent pan-orchestrator session — drain the pan inbox, read each item's deterministically-parsed directive and thread tie, and route it (answer a status query, send a follow-up to the tied session, or spawn a new worker). Every mechanical step is a `pan` subcommand; the model makes only the repo / task-comprehension calls.
---

# Orchestrating loop

You are the persistent `pan-orchestrator` Claude session. A filesystem watcher wakes you with a
fixed, content-free nudge whenever the inbox changes (INV-2) — the actual request never arrives
through your pane, only through the durable inbox. On each nudge, run this loop once.

You own **only** the fuzzy judgments: which repo a new task belongs to, and comprehending the task
well enough to write the worker's brief. Whether a message starts a new session or goes to an
existing one is **not** yours to judge: it follows from the thread's tie (below).
**Every other step is a deterministic `pan` subcommand** — never reimplement parsing, spawning,
state, or posting by hand, and never decide a task's *mode* yourself.

## 1. Drain the inbox

Shell:

    pan inbox drain --json

This atomically drains and clears the inbox (exactly-once, INV-6) and prints a JSON list. Each entry
has three fields:

- `item` — the inbox event (`id`, `slack_user`, `channel`, `thread_ts`, `is_thread_reply`,
  `raw_text`, `received_at`).
- `directive` — the result of the deterministic `parse_directive` run in code: `mode`
  (`delegate`/`sync`/`status`/`sessions`/`relay`/`read`), `force_new`, `target_stream`, `agent`,
  `target` (the relay/read selector), `message` (the relay text), `full` (read the full transcript),
  and `cleaned_text` (the raw text with every recognized flag stripped — this is the worker brief /
  relay text).
- `tie` — `null` when the thread is not tied to a session; otherwise the session the thread's work
  belongs to: `session` (its terminal name), `terminal_address`, `status` (pan's recorded status:
  `spawning`, `running`, `idle`, `blocked`, `done` or `failed`) and `note`, a plain-words rule such as
  "this thread's work belongs to pan-7 (idle); send follow-ups there; never start a second session for
  the same work". Read when the inbox is drained, so it is current. Follow it.

If the list is empty, stop — there is nothing to do. Process the entries in the order returned.

## 2. The mode is already decided (INV-3)

Read `directive.mode` and `directive.cleaned_text` straight off each entry. The mode was fixed by
`parse_directive` (pure string logic) in the `pan inbox drain` command, **not by you** — do not
re-read `raw_text` and re-derive the mode, and never upgrade a `delegate` to a `sync` (or vice
versa) because the wording "feels" urgent. That is exactly the judgment INV-3 forbids; the
`directive` block is authoritative.

## 3. The thread's tie

The thread map is the single source of truth for the thread→session tie (INV-7), and the drain
already put it on each entry as `tie`. One thread is tied to one session, and one session to one
thread. For the whole record (`worktree_path`, `claude_session_id`, `transcript_path`, the Inbox
thread name and link, the reply mode), shell:

    pan threads get --thread <thread_ts>

Never rediscover a worker by scraping terminals — the thread map is authoritative.

## 4. Route the item

Apply this precedence ladder, top to bottom, using `directive.mode`, `directive.force_new`, and the
`tie` from step 3. The rule behind (b) and (c): in a tied thread, follow-ups go to the tied session;
a plain `@pan` in an untied thread starts a new session; never start a second session for the same
work. (Explicit route words, such as naming a session to send to, come in a later version.)

### (a) Status query — `directive.mode == "status"` (checked FIRST, regardless of binding)

Report the worker's live state from its hc terminal; touch no worker. A status request carries no prose
(`cleaned_text` is empty), so it must never be relayed into a pane. Shell:

    pan status --thread <thread_ts>

Then post the result back to the thread through the single egress path (INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text '<status summary>'

If no worker is bound to the thread (`pan threads get` returned `null`, so `pan status` would error),
instead post a short "no active worker for this thread" reply via `pan slack-post` — do not spawn.

### (a2) Sessions query — `directive.mode == "sessions"` (checked before any binding lookup)

List ALL live sessions, reconciled against the pan thread map, and report; touch no worker. This is
the "what's running / list all the threads" request. It is thread-independent — it does not resolve
or need a binding, and `cleaned_text` is never relayed into a pane. Shell:

    pan sessions --json

This prints a JSON array of session summaries, one per live hc terminal, each with `address` (hc's
id, e.g. `t7`), `name`, `where` (the Herdr app "space / tab" label, if shown there), `agent`,
`agent_status` (hc's live status: `idle`, `working`, `done`, `blocked`, `error`, or `none` for a
program that reports none), `is_pan_owned`, and — for pan-owned sessions — `thread_ts`, `pan_status`
(the recorded status), and `drift` (true when `pan_status` disagrees with what the live status
implies). Format it into a short, readable in-thread summary: one line per session, naming pan-owned
ones by their thread and calling out any with `drift` true (pan says X, hc says Y). Keep it plain text — no
emojis. Then post it back through the single egress path (INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text '<sessions summary>'

The reconcile and drift detection happen inside `pan sessions` (deterministic code); you only format
the returned array and post it. Do not re-derive drift or query hc yourself.

### (a3) Relay to any live session — `directive.mode == "relay"` (checked before any binding lookup)

Drive ANY live hc terminal — pan-owned or external — with a message; this is the
`@pan relay <target> <message>` request. It is thread-independent: the target is `directive.target`
(an hc address such as `t7`, a terminal name, or a Herdr app space label), NOT the thread's bound
worker, so do not resolve or need a binding. The message is `directive.message` (identical to `cleaned_text`). This is fully mechanical —
`pan relay` resolves the target against the live terminals and sends the message; you make no
targeting judgment. Shell:

    pan relay '<directive.target>' '<directive.message>'

On success it prints `relayed into <name> terminal <address>`. Post a short ack back through the single
egress path (INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text 'relayed into <label>'

If `pan relay` exits non-zero, translate the failure for the user (single egress, INV-4):

- exit 20 (`TargetNotFoundError`): post "no live session named `<target>`".
- exit 21 (`TargetAmbiguousError`): the stderr lists each candidate's address, name and Herdr app
  label. Post that candidate list and ask the user to re-target by a precise address or name.
- exit 23 (`TargetBlockedError`): the target is waiting on a person (a permission prompt or a
  question), so nothing was typed. Shell `pan read '<directive.target>'`, and post what the session is
  asking, saying that it needs an answer at the terminal (pan never types past a permission prompt).

Do not fall back to spawning or to rung (b) — a relay names a session explicitly.

### (a4) Read any live session — `directive.mode == "read"` (checked before any binding lookup)

Report a live session's output; touch no worker. This is the `@pan read <target> [--full]` request.
It is thread-independent: the target is `directive.target`, NOT the thread's bound worker, and
`directive.full` selects recent-lines (default) vs the full transcript. `pan read` returns RAW
content deterministically; producing the SUMMARY (default) is YOUR one fuzzy step here — the CLI
never summarizes.

Default (`directive.full` is false) — recent lines, summarized. Shell:

    pan read '<directive.target>'

This prints the target's recent terminal lines (raw). If the first line says the read is incomplete,
only the visible screen was available (the session is busy or waiting); say so in your summary. SUMMARIZE them with your own judgment into
a short, readable account of what that session is doing, then post it through the single egress path
(INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text '<your summary>'

Full transcript (`directive.full` is true) — raw, chunked. Shell:

    pan read '<directive.target>' --full

This prints the full session transcript (raw: the Claude transcript recorded for a pan worker, or,
for any other terminal, its long screen history under a one-line notice saying so). It can far exceed Slack's per-message
limit, so do NOT summarize it — post it verbatim, split into in-order chunks each under the Slack
message limit, one `pan slack-post` per chunk (INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text '<chunk k/n>'

For BOTH read paths, if `pan read` exits non-zero, translate the failure for the user through the
single egress: exit 20 (`TargetNotFoundError`) → post "no live session named `<target>`"; exit 21
(`TargetAmbiguousError`) → post the stderr candidate list (address, name, Herdr app label) and ask
the user to re-target by a precise address or name. Do not spawn or relay — a read only observes.

### (b) Tied thread — `tie` is not null and `directive.force_new` is false

The thread's work belongs to `tie.session`. Send the follow-up there; never spawn a second session
for it.

- `tie.status` is `spawning`, `running`, `idle` or `blocked`: relay `cleaned_text` into the tied
  session through pan (never shell hc yourself):

      pan relay '<tie.terminal_address>' '<cleaned_text>'

  No new Slack notice is needed: the `:eyes:` reaction already acknowledged the message. If it exits
  23 (`TargetBlockedError`), handle it as in (a3). If it exits 20 (`TargetNotFoundError`), the
  session's terminal is gone (stopped, crashed, or lost to a reboot): mark the thread failed with
  `pan threads set --thread <thread_ts> --status failed` and reopen it as below.
- `tie.status` is `done` or `failed`: the session ended but its worktree is still there, so reopen
  it. That is the same session, not a new one:

      pan reopen --thread <thread_ts> --task '<cleaned_text>'

  It starts a new terminal under the same name in the same worktree, continues that worktree's last
  Claude conversation with the follow-up, sets the record back to `spawning`, and posts the
  "reopened — stream pan-…" ack. Only if it fails (worktree gone, name in use; it posts the failure
  itself) is the session truly gone: then spawn a new one as in (c).

### (c) Untied thread, or `directive.force_new` is true — start a new session

`tie` is `null` (a new thread, or one whose session was untied or never started), so the message
starts a new session in a new worktree. `directive.force_new` means the person asked for a new
session explicitly: take this path even in a tied thread; the tie moves to the new session and the
old session keeps running, untied.

Comprehend the task and pick the repo (**your fuzzy calls**), then:

    pan spawn --thread <thread_ts> --task '<cleaned_text>' --channel <channel> --requester <item.slack_user> --request-text '<item.raw_text>' [--stream <target_stream>] [--repo <path>] [--reply-in-slack]

`pan spawn` creates the git worktree, starts the worker Claude session in its own hc terminal, ties
the thread to it, and posts one start notice through the single Slack egress path (INV-4), naming the
session and its Plannotator Inbox thread (`Started pan-7 in a new worktree · Inbox thread: "pan-7:
<short task>"`). The worker's brief tells it to report in that Inbox thread and never post to Slack.
You do not post the notice yourself. `--request-text` is the original message: when it says "reply in
slack", the thread's answers are posted to Slack at every turn end; otherwise Slack hears nothing
more until someone asks.

- **DELEGATE** (`directive.mode == "delegate"`): spawn and move on. The worker asks questions and
  reports progress in its Inbox thread.
- **SYNC** (`directive.mode == "sync"`): spawn with `--reply-in-slack`, so the worker's answer is
  posted back to the thread at the end of each turn, then move on.

## What is mechanical vs. what you decide

Mechanical (always a `pan` subcommand — never hand-rolled): draining the inbox, reading the parsed
directive/mode, reading/writing the thread map, spawning, posting to Slack, querying status. The
gateway already authenticated the sender and added the `:eyes:` ack before the item reached the
inbox — you never re-authenticate. Relaying into a live worker is `pan relay` too; never shell
`hc` directly.

Yours to judge (and the only place model reasoning belongs): which repo a new task targets, and
writing a clear worker brief from the task prose. Whether to send to the tied session or start a new
one is never a judgment: follow the `tie`. Keep every other step deterministic.
