---
name: orchestrating
description: Use on every inbox nudge in the persistent pan-orchestrator session — drain the pan inbox, read each item's route (parsed in code), directive and thread tie, and act on it (run check / sessions / forget, send to a named session, start a new session, or send a follow-up to the tied session). Every mechanical step is a `pan` subcommand; the model makes only the repo / task-comprehension calls.
---

# Orchestrating loop

You are the persistent `pan-orchestrator` Claude session. A filesystem watcher wakes you with a
fixed, content-free nudge whenever the inbox changes (INV-2) — the actual request never arrives
through your pane, only through the durable inbox. On each nudge, run this loop once.

You own **only** the fuzzy judgments: which repo a new task belongs to, and comprehending the task
well enough to write the worker's brief. Whether a message starts a new session or goes to an
existing one is **not** yours to judge: it follows from the route words and the thread's tie (below).
**Every other step is a deterministic `pan` subcommand** — never reimplement parsing, spawning,
state, or posting by hand, and never decide a task's *mode* yourself.

## 1. Drain the inbox

Shell:

    pan inbox drain --json

This atomically drains and clears the inbox (exactly-once, INV-6) and prints a JSON list. Each entry
has four fields:

- `item` — the inbox event (`id`, `slack_user`, `channel`, `thread_ts`, `is_thread_reply`,
  `raw_text`, `received_at`, `source`).
- `route` — the route words, parsed in code by `parse_route` (the gateway parses them when the
  message arrives): `kind` (`task`, `new`, `to`, `sessions`, `forget`, `check` or `notice`),
  `session` (the name after `to`), `text` (the message without the mention and the route word: the
  task, or the text to send), `reply_in_slack` (the message says "reply in slack") and `notice`
  (for `kind == "notice"`, the exact reply to post).
- `directive` — the result of the deterministic `parse_directive`, run on `route.text`: `mode`
  (`delegate`/`sessions`/`relay`/`read`), `target_stream` (from `--stream <name>`), `agent`,
  `target` (the relay/read selector), `message` (the relay text), `full` (read the full transcript),
  and `cleaned_text` (the text with every recognized flag stripped — this is the worker brief /
  relay text).
- `tie` — `null` when the thread is not tied to a session; otherwise the session the thread's work
  belongs to: `session` (its terminal name), `terminal_address`, `status` (pan's recorded status:
  `spawning`, `running`, `idle`, `blocked`, `done` or `failed`) and `note`, a plain-words rule such as
  "this thread's work belongs to pan-7 (idle); send follow-ups there; never start a second session for
  the same work". Read when the inbox is drained, so it is current. Follow it.

If the list is empty, stop — there is nothing to do. Process the entries in the order returned.

## 2. The route and mode are already decided (INV-3)

Read `route` and `directive` straight off each entry. Both were fixed by pure string logic in code,
**not by you** — do not re-read `raw_text` and re-derive them, and never turn a plain task into a
route (or a route into a task) because the wording "feels" like one. The route words are strict:
only the first word after the mention counts, spelled exactly, so "new login page" is a plain task
and `new: login page` is a route. That is exactly the judgment INV-3 forbids; `route` and
`directive` are authoritative.

## 3. The thread's tie

The thread map is the single source of truth for the thread→session tie (INV-7), and the drain
already put it on each entry as `tie`. One thread is tied to one session, and one session to one
thread. For the whole record (`worktree_path`, `claude_session_id`, `transcript_path`, the Inbox
thread name and link, the reply mode), shell:

    pan threads get --thread <thread_ts>

Never rediscover a worker by scraping terminals — the thread map is authoritative.

## 4. Route the item

Act on `route.kind` first. In (r2) to (r5) the `pan` command posts its own reply through the single
Slack egress (INV-4), as is — never summarize, reword or repost its output.

### (r1) `route.kind == "notice"` — post the notice, do nothing else

The message used a retired flag (`--new`, `--status`, `--sync`, a leading `!`) or a route word with
no task. Post `route.notice` verbatim and stop; start nothing, send nothing:

    pan slack-post --thread <thread_ts> --channel <channel> --text '<route.notice>'

### (r2) `route.kind == "check"` — the tied session's latest answer

    pan check --thread <thread_ts> --channel <channel>

It posts the tied session's latest answer (the last answer in its transcript, or its recent screen
lines labelled as screen text), its Inbox thread, and whether it is still working or waiting on a
prompt at the terminal, split into Slack-sized parts. In an untied thread it posts a one-line reply
saying so. Touch no session.

### (r3) `route.kind == "sessions"`, or `directive.mode == "sessions"` — list live sessions

    pan sessions --thread <thread_ts> --channel <channel>

It posts the table of every live terminal (name, idle or busy, pan-owned or not, last ask) to the
thread. `directive.mode == "sessions"` covers the older spellings (`--sessions`, "what's running").
Touch no session.

### (r4) `route.kind == "forget"` — untie the thread

    pan forget --thread <thread_ts> --channel <channel>

It unties the thread and posts one line. It never stops the session.

### (r5) `route.kind == "to"` — send to the named session and tie the thread to it

    pan send --thread <thread_ts> --channel <channel> --to '<route.session>' --task '<route.text>' --requester <item.slack_user> [--reply-in-slack]

Pass `--reply-in-slack` when `route.reply_in_slack` is true. `route.session` may be several words
(`to pan with slack:`); pass it whole and quoted. `route.text` is sent verbatim — do not rewrite it.
The session may be one pan started or one of the person's own desk sessions. `pan send` resolves the
selector against the live sessions, first match wins: exact address, exact name, exact Herdr label
or space, the Plannotator Inbox thread the session writes (exact name, else every word), then every
word in the session's name, label and folder (also tried when the thread is found but its session
can't be reached). It types the text only if the session is idle
(with one line on top saying it came from Slack and naming its Inbox thread, or, for a desk session
whose thread pan does not know, saying to use its existing thread or start one), ties this thread
to that session (its previous thread is untied) and posts "Sent to desk-api · Inbox: ...". When it
refuses — an unknown selector (it lists close matches; for several words it also says to start new
work with `@pan new: <your text>`), an ambiguous one (it posts a pick list), an Inbox thread whose
session is not running, a busy session ("<name> is busy; try again, or @pan
check"), or a session waiting on a prompt — it posts the short reply itself, types nothing and exits
non-zero (20, 21, 26 or 23). Do nothing more: never retry, never fall back to spawning or to the
tie.

`to <name>:` from X is refused before it reaches you (the route is a notice): X may only start new
sessions.

### (r6) `route.kind == "new"` — start a new session even in a tied thread

Go to (c) below with `directive.cleaned_text` as the task. In a tied thread the tie moves to the
new session; the old session keeps running, untied.

### (r7) `route.kind == "task"` — no route words

Use `directive.mode`, then the tie:

- `relay` — (a3) below. `read` — (a4) below. `sessions` — (r3) above.
- `delegate` — the tie decides: `tie` is not null → (b); `tie` is null → (c).

### (a3) Relay to any live session — `directive.mode == "relay"`

A one-off: it ties nothing. Drive ANY live hc terminal — pan-owned or external — with a message;
this is the `@pan relay <target> <message>` request. The target is `directive.target` (an hc address
such as `t7`, a terminal name, or a Herdr app space label), NOT the thread's tied session. The
message is `directive.message` (identical to `cleaned_text`). This is fully mechanical — `pan relay`
resolves the target against the live terminals and sends the message; you make no targeting
judgment. Shell:

    pan relay '<directive.target>' '<directive.message>'

On success it prints `relayed into <name> terminal <address>`. Post a short ack back through the
single egress path (INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text 'relayed into <label>'

If `pan relay` exits non-zero, translate the failure for the user (single egress, INV-4):

- exit 20 (`TargetNotFoundError`): post "no live session named `<target>`".
- exit 21 (`TargetAmbiguousError`): the stderr lists each candidate's address, name and Herdr app
  label. Post that candidate list and ask the user to re-target by a precise address or name.
- exit 23 (`TargetBlockedError`): the target is waiting on a person (a permission prompt or a
  question), so nothing was typed. Shell `pan read '<directive.target>'`, and post what the session is
  asking, saying that it needs an answer at the terminal (pan never types past a permission prompt).

Do not fall back to spawning or to (b) — a relay names a session explicitly.

### (a4) Read any live session — `directive.mode == "read"`

A one-off: it ties nothing and touches no session. This is the `@pan read <target> [--full]`
request. The target is `directive.target`, NOT the thread's tied session, and `directive.full`
selects recent-lines (default) vs the full transcript. `pan read` returns RAW content
deterministically; producing the SUMMARY (default) is YOUR one fuzzy step here — the CLI never
summarizes.

Default (`directive.full` is false) — recent lines, summarized. Shell:

    pan read '<directive.target>'

This prints the target's recent terminal lines (raw). If the first line says the read is incomplete,
only the visible screen was available (the session is busy or waiting); say so in your summary.
SUMMARIZE them with your own judgment into a short, readable account of what that session is doing,
then post it through the single egress path (INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text '<your summary>'

Full transcript (`directive.full` is true) — raw, chunked. Shell:

    pan read '<directive.target>' --full

This prints the full session transcript (raw: the Claude transcript recorded for a pan worker, or,
for any other terminal, its long screen history under a one-line notice saying so). It can far exceed
Slack's per-message limit, so do NOT summarize it — post it verbatim, split into in-order chunks each
under the Slack message limit, one `pan slack-post` per chunk (INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text '<chunk k/n>'

For BOTH read paths, if `pan read` exits non-zero, translate the failure for the user through the
single egress: exit 20 (`TargetNotFoundError`) → post "no live session named `<target>`"; exit 21
(`TargetAmbiguousError`) → post the stderr candidate list (address, name, Herdr app label) and ask
the user to re-target by a precise address or name. Do not spawn or relay — a read only observes.

### (b) Tied thread, no route words — `route.kind == "task"`, `directive.mode == "delegate"`, `tie` is not null

The thread's work belongs to `tie.session`. Send the follow-up there; never spawn a second session
for it.

- `tie.status` is `spawning`, `running`, `idle` or `blocked`: send `cleaned_text` to the tied
  session through pan (never shell hc yourself, and never use `pan relay` for this):

      pan send --thread <thread_ts> --channel <channel> --task '<cleaned_text>'

  With no `--to`, it sends to the thread's tied session. No new Slack notice is needed on success:
  the `:eyes:` reaction already acknowledged the message. A busy session (exit 26) or one waiting on
  a prompt (exit 23) gets a short reply from `pan send` itself, and nothing is typed: do nothing
  more. Exit 22 means the session has ended: when `pan threads get` shows `started_by_pan` true,
  mark the thread failed with `pan threads set --thread <thread_ts> --status failed` and reopen it as
  below; for a session pan did not start, `pan send` already said it ended, so do nothing more.
- `tie.status` is `done` or `failed`: the session ended. When pan started it (`started_by_pan` in
  `pan threads get`) and its worktree is still there, reopen it. That is the same session, not a
  new one. A session pan did not start is never reopened: `pan reopen` refuses it with a reply.

      pan reopen --thread <thread_ts> --task '<cleaned_text>'

  It starts a new terminal under the same name in the same worktree, continues that worktree's last
  Claude conversation with the follow-up, sets the record back to `spawning`, and posts the
  "reopened — stream pan-…" ack. Only if it fails (worktree gone, name in use; it posts the failure
  itself) is the session truly gone: then spawn a new one as in (c).

### (c) Start a new session — `route.kind == "new"`, or no route words in an untied thread

`route.kind == "new"` means the person asked for a new session explicitly: take this path even in a
tied thread. Otherwise `tie` is `null` (a new thread, or one whose session was untied or never
started), so a plain message starts a new session in a new worktree.

Comprehend the task and pick the repo (**your fuzzy calls**), then:

    pan spawn --thread <thread_ts> --task '<cleaned_text>' --channel <channel> --requester <item.slack_user> --request-text '<item.raw_text>' [--stream <target_stream>] [--repo <path>] [--reply-in-slack]

Pass `--reply-in-slack` when `route.reply_in_slack` is true. `pan spawn` creates the git worktree,
starts the worker Claude session in its own hc terminal, ties the thread to it, and posts one start
notice through the single Slack egress path (INV-4), naming the session and its Plannotator Inbox
thread (`Started pan-7 in a new worktree · Inbox thread: "pan-7: <short task>"`). The worker's brief
tells it to report in that Inbox thread and never post to Slack. You do not post the notice
yourself. When the thread asked for replies in Slack, the session's answers are posted to Slack at
every turn end; otherwise Slack hears nothing more until someone writes `@pan check`.

## What is mechanical vs. what you decide

Mechanical (always a `pan` subcommand — never hand-rolled): draining the inbox, reading the parsed
route and directive, reading/writing the thread map, spawning, sending to a named session
(`pan send`), checking (`pan check`), listing (`pan sessions`), untying (`pan forget`), posting to
Slack. The
gateway already authenticated the sender and added the `:eyes:` ack before the item reached the
inbox — you never re-authenticate. Relaying into a live worker is `pan relay` too; never shell
`hc` directly.

Yours to judge (and the only place model reasoning belongs): which repo a new task targets, and
writing a clear worker brief from the task prose. Whether to send to the tied session, a named
session or a new one is never a judgment: follow the `route`, then the `tie`. Keep every other step deterministic.
