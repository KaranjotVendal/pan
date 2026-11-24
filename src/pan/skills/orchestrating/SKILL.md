---
name: orchestrating
description: Use on every inbox nudge in the persistent pan-orchestrator session — drain the pan inbox, read each item's route (parsed in code), directive and thread tie, and act on it (run check / sessions / forget / done, send to a named session or propose one for the person to confirm, start a new session, or send a follow-up to the tied session). Every mechanical step is a `pan` subcommand; the model makes only the repo / task-comprehension calls, the proposal of a session for an inexact `to <name>:`, and whether a message asks for its answer in the Slack thread.
---

# Orchestrating loop

You are the persistent `pan-orchestrator` Claude session. A filesystem watcher wakes you with a
fixed, content-free nudge whenever the inbox changes (INV-2) — the actual request never arrives
through your pane, only through the durable inbox. On each nudge, run this loop once.

You own **only** the fuzzy judgments: which repo a new task belongs to, comprehending the task
well enough to write the worker's brief, proposing which live session a `to <name>:` meant when
`<name>` matches none exactly (r6; the person confirms before anything is sent), and whether a
message asks for its answer in the Slack thread (the reply mode; step 2). Whether a message starts
a new session or goes to an existing one is **not** yours to judge: it follows from the route words
and the thread's tie (below).
**Every other step is a deterministic `pan` subcommand** — never reimplement parsing, spawning,
state, or posting by hand, and never decide a task's *mode* yourself.

## 1. Drain the inbox

Shell:

    pan inbox drain --json

This atomically drains and clears the inbox (exactly-once, INV-6) and prints a JSON list. Each entry
has five fields:

- `item` — the inbox event (`id`, `slack_user`, `channel`, `thread_ts`, `is_thread_reply`,
  `raw_text`, `received_at`, `source`).
- `route` — the route words, parsed in code by `parse_route` (the gateway parses them when the
  message arrives): `kind` (`task`, `new`, `to`, `sessions`, `forget`, `check`, `done`, `notice`
  or `confirm`), `session` (the name after `to`), `text` (the message without the mention and the
  route word: the task, or the text to send), `reply_in_slack` (the message says "reply in slack";
  a hint, see "Reply in Slack by meaning" in step 2), `notice` (for `kind == "notice"`, the exact
  reply to post), `choice` (for `kind == "confirm"`: `yes`, a number, or `no`) and `discard` (for
  `kind == "done"`: the message was `done --discard`). The drain sets `confirm` itself: a `yes`,
  `y` or 1-3 reply, or a
  decline (`no`, `n`, `cancel`, `stop`; choice `no`), in a thread where pan asked "Send this to
  ...?" or "Which session?" less than 30 minutes ago.
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
- `cancels_confirmation` — true when pan has an open "Send this to ...?" question in the thread and
  this message is not a `yes`/number/decline reply to it. Handle it before the route (step 4).

If the list is empty, stop — there is nothing to do. Process the entries in the order returned.

## 2. The route and mode are already decided (INV-3)

Read `route` and `directive` straight off each entry. Both were fixed by pure string logic in code,
**not by you** — do not re-read `raw_text` and re-derive them, and never turn a plain task into a
route (or a route into a task) because the wording "feels" like one. The route words are strict:
only the first word after the mention counts, spelled exactly, so "new login page" is a plain task
and `new: login page` is a route. That is exactly the judgment INV-3 forbids; `route` and
`directive` are authoritative.

INV-3 has one bounded exception, the target of a `to <name>:` that `pan send` cannot match exactly
(exit 28). There you read the message and the live sessions and propose up to 3 of them with
`pan propose`; you never send. `pan propose` keeps only live terminal addresses, the person must
reply `yes` or a number in the thread, and on that reply `pan confirm` checks the chosen session is
still the same live terminal before sending.

**Reply in Slack by meaning.** INV-3 has a second bounded exception, the reply mode only.
`route.reply_in_slack` is the exact phrase "reply in slack", matched in code; treat it as a hint,
not the whole rule. Pass `--reply-in-slack` (to `pan spawn`, `pan send` with or without `--to`,
`pan propose` and `pan reopen`) when `route.reply_in_slack` is true, AND also when the message's
meaning plainly asks for the answer or result in this Slack thread: "share it here", "post it here",
"send the link here", "reply here", "reply back with the link", "let me know here", "drop the
result in this thread". On a follow-up or a reopen the flag switches the thread to replying in Slack
from then on; leaving it off never switches it back.
Do not pass it when the message does not ask for that: "here" meaning a place in the code, a
repository or a page ("fix the bug here", "the config is here"), or a message that only asks for the
work to be done. When unsure, leave it off: the person can still `@pan check`. This judgment sets the
reply mode and nothing else; it never changes the route, the mode or the target.

Nothing else about routing is yours to judge.

## 3. The thread's tie

The thread map is the single source of truth for the thread→session tie (INV-7), and the drain
already put it on each entry as `tie`. One thread is tied to one session, and one session to one
thread. For the whole record (`worktree_path`, `claude_session_id`, `transcript_path`, the Inbox
thread name and link, the reply mode), shell:

    pan threads get --thread <thread_ts>

Never rediscover a worker by scraping terminals — the thread map is authoritative.

## 4. Route the item

If `cancels_confirmation` is true, first drop pan's open question in the thread (it posts
`Not sent.`), then carry on with the route below as usual:

    pan confirm --thread <thread_ts> --channel <channel> --cancel

Act on `route.kind` first. In (r2) to (r7) the `pan` command posts its own reply through the single
Slack egress (INV-4), as is — never summarize, reword or repost its output.

What Slack shows for a session: one status message per thread, which pan edits in place (Working,
Waiting for you, Finished, Closed, Stopped); the session's answers as their own messages with
nothing around them; and the session's Inbox thread and terminal only to the requester, as a message
only they can see. Never post an Inbox link, an Inbox thread name, a terminal address or anything
with `localhost` in the thread yourself.

### (r1) `route.kind == "notice"` — post the notice, do nothing else

The message used a retired flag (`--new`, `--status`, `--sync`, a leading `!`) or a route word with
no task. Post `route.notice` verbatim and stop; start nothing, send nothing:

    pan slack-post --thread <thread_ts> --channel <channel> --text '<route.notice>'

### (r2) `route.kind == "check"` — the tied session's latest answer

    pan check --thread <thread_ts> --channel <channel>

It brings the thread's status message up to date (working, waiting on a prompt, finished, stopped),
posts the tied session's latest answer as its own message (the last answer in its transcript, or its
recent screen lines labelled as screen text; nothing when there is neither), split into Slack-sized
parts, and sends the session's Inbox thread and terminal to the requester only. In an untied thread
it posts a one-line reply saying so. Touch no session.

### (r3) `route.kind == "sessions"`, or `directive.mode == "sessions"` — list live sessions

    pan sessions --thread <thread_ts> --channel <channel> --requester <item.slack_user> [--match '<route.text>']

Add `--match` only when `route.kind == "sessions"` and `route.text` is not empty (`@pan sessions ocr
review` gives `ocr review`); never on the `directive.mode == "sessions"` path (`--sessions`, "what's
running", "list sessions"), which lists without a filter. Quote `route.text` in single quotes as
the other free text here is, writing each `'` inside it as `'\''`.

It sends the requester alone (an ephemeral message, never a post in the thread) up to 5 sessions to
pick from, each with its state and a link to its Slack thread: those matching any of the words, or,
without words, the ones waiting for them, working, idle, then finished or stopped, the most recently
used first within each. Touch no session.

**Exit 32: the private message failed.** Nothing was posted. Post one line to the thread, with no
session names:

    pan slack-post --thread <thread_ts> --channel <channel> --text 'I couldn'\''t send you the sessions list privately.'

### (r4) `route.kind == "forget"` — untie the thread

    pan forget --thread <thread_ts> --channel <channel>

It unties the thread and posts one line. It never stops the session.

### (r5) `route.kind == "done"` — close the thread's session

    pan done --thread <thread_ts> --channel <channel> [--discard]

Pass `--discard` when `route.discard` is true (the message was `done --discard`). It posts its own
one-line reply in every case; do nothing more, whatever its exit code:

- Untied thread: "This thread has no session." Nothing is touched.
- A session pan did not start: it only unties the thread ("Untied this thread from desk-x; pan never
  closes sessions it didn't start."). pan never closes it.
- pan's own session while it is working (exit 26) or waiting on a prompt (exit 23): refused,
  unless `--discard`.
- pan's own session with unsaved work: kept, exit 29, unless `--discard` ("pan-7 has 2 uncommitted
  files and 1 unpushed commit; kept it. ..."). Unsaved work is uncommitted or untracked files (the
  untracked `.claude/settings.json` pan writes at spawn does not count; a tracked one the session
  changed does), or branch commits its upstream lacks. An upstream deleted after a merge counts as
  holding them; with no upstream, commits merged into neither the repo's default branch nor the
  branch it was created from count.
- Otherwise it removes the session's terminal (if still there), its worktree and its branch, then
  marks the record `done` and unties the thread ("Closed pan-7 and removed its worktree and
  branch."). With the worktree folder already gone it still deletes the branch from the repository.
- Exit 30: a git command failed. The terminal may be gone, but the thread stays tied with its status
  unchanged, so the person can send `@pan done` again to finish; the reply says so.
- Exit 31: the session's folder is outside pan's worktree folder or is a repository's main checkout.
  pan never removes it, and retrying will not change that; the reply says so.

`pan stop` stays the kill switch (it marks the thread `failed` and never touches the worktree);
never use it for `done`.

### (r6) `route.kind == "to"` — send to the named session and tie the thread to it

    pan send --thread <thread_ts> --channel <channel> --to '<route.session>' --task '<route.text>' --requester <item.slack_user> [--reply-in-slack]

Pass `--reply-in-slack` when `route.reply_in_slack` is true or the message's meaning asks for the
answer in this thread (step 2, "Reply in Slack by meaning"). `route.text` is sent verbatim — do not
rewrite it.
The session may be one pan started or one of the person's own desk sessions. `pan send` sends only
to an exact match: the session's address, name, or Herdr label or space. It types the text only if
the session is idle (with one line on top saying it came from Slack and naming its Inbox thread, or,
for a desk session whose thread pan does not know, saying to use its existing thread or start one),
ties this thread to that session (its previous thread is untied), posts the thread's status
message (Working) and sends the session's Inbox thread to the requester only. A busy session ("<name> is busy; try again, or @pan check") or one waiting on a prompt
gets the short reply from `pan send` itself, nothing typed, exit 26 or 23: do nothing more.

**Exit 28: no exact match** (or a label several sessions share). Nothing was sent or posted. Propose
the session the person most likely meant:

1. Run `pan sessions --json` and read each live session's `address`, `name`, `where`, `cwd`,
   `last_ask` and `inbox_thread_name`.
2. Compare them with `route.session` and `route.text`: a name, label, folder or Inbox thread that
   holds those words, or recent work on that topic. Pick the one most likely session; if two or
   three fit about equally, pick those, most likely first; if none plausibly fits, pick none. Never
   pick more than 3.
3. Post the question (same flags as `pan send`, plus one `--candidate <address>` per pick):

       pan propose --thread <thread_ts> --channel <channel> --to '<route.session>' --task '<route.text>' --requester <item.slack_user> [--reply-in-slack] [--candidate <address> ...]

   With one candidate it asks "Send this to stream-pan (work / 1, folder pan)? Reply yes."; with
   several, "Which session? Reply 1, 2 or 3:" and a numbered list; with none, it says no session
   matches and how to start new work. Do nothing more: the person's reply arrives as a new item.

Never spawn, retry `pan send`, or fall back to the tie for a `to` route.

`to <name>:` from X is refused before it reaches you (the route is a notice): X may only start new
sessions.

### (r7) `route.kind == "confirm"` — the person answered pan's question

When `route.choice` is `no`, the person declined. Run only this (it posts `Not sent.`), and do
nothing else with the message: it is not a task.

    pan confirm --thread <thread_ts> --channel <channel> --cancel

Otherwise (`yes` or a number):

    pan confirm --thread <thread_ts> --channel <channel> --choice <route.choice>

It sends the task stored by `pan propose` to the chosen session through the same path as `pan send`,
ties the thread and posts its status message. A busy session or one waiting on a prompt gets a reply
asking the person to answer again later, and the question stays open (exit 26 or 23). A reply that
picks nothing (`yes` to several, a number out of range) gets "Reply 1 or 2 to choose ..." and the
question stays open; a chosen session that has ended gets a one-line reply (exit 20). Do nothing
more in every case.

### (r8) `route.kind == "new"` — start a new session even in a tied thread

Go to (c) below with `directive.cleaned_text` as the task. In a tied thread the tie moves to the
new session; the old session keeps running, untied.

### (r9) `route.kind == "task"` — no route words

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
for it. An item without an `@pan` mention normally arrives only for a tied thread (or one just
started with `@pan`). If one arrives with no tie (for example, the thread was untied just before),
do not start a session for it; ignore it.

- `tie.status` is `spawning`, `running`, `idle` or `blocked`: send `cleaned_text` to the tied
  session through pan (never shell hc yourself, and never use `pan relay` for this):

      pan send --thread <thread_ts> --channel <channel> --task '<cleaned_text>' [--reply-in-slack]

  Pass `--reply-in-slack` under the same rule as everywhere else (step 2, "Reply in Slack by
  meaning"; a follow-up such as "reply back with the link" or "post it here" counts). Once the text
  is typed it switches the thread's reply mode to Slack, so the session's answer is posted to the
  thread; a refused send leaves the mode as it was. With no `--to`, it sends to the thread's tied session. On success
  it edits the thread's status message back to Working; post nothing yourself. A busy session (exit 26) or one waiting on
  a prompt (exit 23) gets a short reply from `pan send` itself, and nothing is typed: do nothing
  more. Exit 22 means the session has ended: when `pan threads get` shows `started_by_pan` true,
  mark the thread failed with `pan threads set --thread <thread_ts> --status failed` and reopen it as
  below; for a session pan did not start, `pan send` already said it ended, so do nothing more.
- `tie.status` is `done` or `failed`: the session ended. When pan started it (`started_by_pan` in
  `pan threads get`) and its worktree is still there, reopen it. That is the same session, not a
  new one. A session pan did not start is never reopened: `pan reopen` refuses it with a reply.

      pan reopen --thread <thread_ts> --task '<cleaned_text>' [--reply-in-slack]

  Pass `--reply-in-slack` under the same rule as for `pan send` above.

  It starts a new terminal under the same name in the same worktree, continues that worktree's last
  Claude conversation with the follow-up, sets the record back to `spawning`, and edits the
  thread's status message back to Working. Only if it fails (worktree gone, name in use; it posts the failure
  itself) is the session truly gone: then spawn a new one as in (c).

### (c) Start a new session — `route.kind == "new"`, or no route words in an untied thread

`route.kind == "new"` means the person asked for a new session explicitly: take this path even in a
tied thread. Otherwise `tie` is `null` (a new thread, or one whose session was untied or never
started), so a plain message starts a new session in a new worktree.

Comprehend the task and pick the repo (**your fuzzy calls**), then:

    pan spawn --thread <thread_ts> --task '<cleaned_text>' --channel <channel> --requester <item.slack_user> --request-text '<item.raw_text>' [--stream <target_stream>] [--repo <path>] [--reply-in-slack] [--thread-reply]

Pass `--reply-in-slack` when `route.reply_in_slack` is true or the message's meaning asks for the
answer in this thread (step 2, "Reply in Slack by meaning"). Pass `--thread-reply` when
`item.is_thread_reply` is true: the message was a reply inside a Slack thread, so the worker's brief
names the channel and thread ts and tells the session to read the whole thread first with its own
Slack read-thread tool (`slack_read_thread`), only to read. You do not read or summarize the thread
yourself. `pan spawn` creates the git worktree,
starts the worker Claude session in its own hc terminal, ties the thread to it, and posts the
thread's one status message ("Working" and the short task, with the session name in small text);
the session's Plannotator Inbox thread and terminal go to the requester only. The worker's brief
tells it to report in that Inbox thread, never post to Slack, and treat messages from
pan-orchestrator as the person's follow-ups (never answering them with `hc send`). You do not post
anything yourself. The session's hooks edit the status message as it works (Finished at each turn
end, Waiting for you on a prompt). When the thread asked for replies in Slack, each turn's answer is
also posted, alone; otherwise Slack hears nothing more until someone writes `@pan check`.

## What is mechanical vs. what you decide

Mechanical (always a `pan` subcommand — never hand-rolled): draining the inbox, reading the parsed
route and directive, reading/writing the thread map, spawning, sending to a named session
(`pan send`), asking and acting on a confirmation (`pan propose`, `pan confirm`), checking
(`pan check`), listing (`pan sessions`), untying (`pan forget`), closing a session (`pan done`),
posting to Slack. The
gateway already authenticated the sender and added the `:eyes:` ack before the item reached the
inbox — you never re-authenticate. Relaying into a live worker is `pan relay` too; never shell
`hc` directly.

Yours to judge (and the only place model reasoning belongs): which repo a new task targets,
writing a clear worker brief from the task prose, which sessions to propose after `pan send`
exits 28, and whether the message asks for its answer in the Slack thread (`--reply-in-slack`). Whether to send to the tied session, a named session or a new one is never a judgment:
follow the `route`, then the `tie`; a proposed session gets the task only after the person replies. Keep every other step deterministic.
