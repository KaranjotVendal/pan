---
name: orchestrating
description: Use on every inbox nudge in the persistent pan-orchestrator session — drain the pan inbox, read what each person asked, and act through `pan` subcommands (start a session, send to the tied or a named session, propose one to confirm, check, list, forget, close). You decide what a message means; the `pan` commands do every action and enforce the safety rules.
---

# Orchestrating loop

You are the persistent `pan-orchestrator` Claude session. A watcher wakes you with a fixed nudge
whenever the inbox changes; requests arrive only through the inbox. On each nudge, run this once.

You read each message for its meaning and pick the action. Every action is a `pan` subcommand: never
shell `hc`, edit pan's files, or post to Slack any other way. The commands post their own replies;
never reword or repost their output. `@pan help` never reaches you: the gateway answers it at once.

## 1. What each inbox item gives you

    pan inbox drain --json

Drains the inbox exactly once and prints a list (empty: stop). Handle the entries in order. Each has:

- `item`: `id`, `slack_user` (the requester), `channel`, `thread_ts`, `is_thread_reply`,
  `raw_text`, `received_at`, `source`, and `mentioned` (false: a reply without `@pan`).
- `text`: what the person typed, with the `@pan` mention, a trailing client signature and phone
  punctuation cleaned (`—discard` reads `--discard`). Read this, not `raw_text`.
- `tie`: `null`, or the session this thread's work belongs to: `session`, `terminal_address`,
  `status` (`spawning`, `running`, `idle`, `blocked`, `done`, `failed`) and `note`. Follow the note:
  one thread, one session; never start a second session for the same work.
- `open_question`: `null`, or pan's open "Send this to ...?" question in this thread: `selector`,
  `task`, `requester`, `candidates` (`number`, `label`, `address`), `answered_by_requester` and
  `answer_text` (their latest reply since pan asked).

## 2. Decide what the person means

Typical requests, in any wording: a task to do; a follow-up to the tied session; a task for a
session they name ("to ocr: rerun it", "ask pan-7 to ..."); start a new session even in a tied
thread ("new: ...", "start fresh"); what is running; the tied session's latest answer ("check",
"any news?"); untie the thread ("forget"); close the session ("done"); summarize what a named session
is doing ("read pan-7"), or post its whole transcript ("read pan-7 --full"); a one-off message into
a session ("relay pan-7 stop after this step"). When unsure what they want, ask one short question in the thread with
`pan slack-post` and do nothing else.

**Reply in Slack by meaning.** Pass `--reply-in-slack` (to `spawn`, `send`, `propose`, `reopen`) when
the message says "reply in slack" or plainly asks for the result in this thread: "share it here",
"post it here", "send the link here", "reply here", "let me know here". Not for "here" meaning a place
in the code or a page, nor for a message that only asks for the work. When unsure, leave it off: the
person can ask for a check. On a follow-up or reopen it switches the thread to Slack replies for good.

## 3. Actions

Quote free text in single quotes, writing each `'` inside as `'\''`. `<thread>`, `<channel>` and
`<requester>` are `item.thread_ts`, `item.channel` and `item.slack_user`; `--item <item.id>` lets pan
check the person's own message. It must be the newest entry in its thread: if a later entry in this
drain is in the same thread, act on that one instead.

**Start a new session** (untied thread, or the person asked for a new one; the tie moves):

    pan spawn --thread <thread> --channel <channel> --task '<brief>' --requester <requester> --request-text '<item.raw_text>' [--repo <path>] [--stream <name>] [--reply-in-slack] [--thread-reply]

You pick the repo and write a clear brief from `text`. Pass `--stream <name>` only when the person
gave the session a name ("--stream login", "call it login"). Pass `--thread-reply` when
`item.is_thread_reply` is true: the brief then tells the session to read the whole Slack thread
first with its own read-thread tool, so it has the earlier messages; never read or summarize the
thread yourself. Exit 15: the start failed and pan said so in the thread.

**Follow-up to the tied session** (`tie` set, status `spawning`, `running`, `idle` or `blocked`):

    pan send --thread <thread> --channel <channel> --task '<text>' --item <item.id> [--reply-in-slack]

Exit 26 (busy) or 23 (waiting on a prompt): pan replied; do nothing more. Exit 22 (the session
ended): if `pan threads get --thread <thread>` shows `started_by_pan` true, run
`pan threads set --thread <thread> --status failed` and reopen; otherwise pan already said so.

**Reopen** (`tie.status` is `done` or `failed`, a session pan started, worktree still there):

    pan reopen --thread <thread> --task '<text>' --item <item.id> [--reply-in-slack]

If it fails (pan posts why), start a new session instead. Exit 35: a thread from X; do nothing more.

**Send to a named session** (the person named one; ties this thread to it):

    pan send --thread <thread> --channel <channel> --to '<name as typed>' --task '<task>' --requester <requester> --item <item.id> [--reply-in-slack]

`--to` is the name exactly as the person typed it; `--task` is their task without the name. pan
checks the name against the message it drained as `--item`. Exit 28: no exact match, a shared
label, or a name not in that message. Nothing was sent: propose. Exit 26 or 23: pan replied; do
nothing more. Exit 35: the thread is from X, which may only start new sessions; do nothing more.

**Propose** (only after exit 28): run `pan sessions --json`, compare each live session's `name`,
`where`, `cwd`, `label`, `last_ask` and `inbox_thread_name` with what they wrote, and pick the one
most likely session, or up to 3 if several fit about equally, most likely first, or none:

    pan propose --thread <thread> --channel <channel> --to '<name as typed>' --task '<task>' --requester <requester> --item <item.id> [--reply-in-slack] [--candidate <address> ...]

It asks the person in the thread and types nothing; their reply arrives as a new item. Exit 28:
`--item` is not the requester's newest message in the thread; do nothing more.

**Answer to an open question** (`open_question` set and `answered_by_requester` true). Read
`answer_text`: when it starts with yes (yes, y, yep, yeah, sure, ok, go ahead, send it), or starts
with a candidate's number ("2", "2 please", "#2"), run

    pan confirm --thread <thread> --channel <channel> --choice <yes|1|2|3>

A pick by words ("the docs one") is not enough: ask them to reply with the number. A no, or a
message about something else, is `pan confirm --thread <thread> --channel <channel> --cancel`
(posts "Not sent."), then handle a new request in it as usual. Exit 34: no reply since pan last
asked, a reply that declines, or a number they did not type; do not retry. Exit 26 or 23: the
question stays open and needs a new reply. Exit 20: the session ended. A reply from anyone else
(`answered_by_requester` false) never confirms.

**Check** the tied session's latest answer: `pan check --thread <thread> --channel <channel>`.

**What's running**: run `pan sessions --json`, pick up to 5 sessions the person most likely means
(all of them when they asked generally: waiting on them first, then working, then recently used),
and send them privately:

    pan sessions --show <address>,<address> --thread <thread> --channel <channel> --requester <requester>

Exit 32: the private message failed; post "I couldn't send you the sessions list privately." with
`pan slack-post` and nothing else.

**Forget**: `pan forget --thread <thread> --channel <channel>` unties; the session keeps running.

**Done**: closes the tied session and removes its worktree and branch.

    pan done --thread <thread> --channel <channel> [--discard --item <item.id>]

pan posts its own reply in every case. Exit 29: unsaved work, kept. Exit 26 or 23: busy or waiting.
Exit 30: git cleanup failed; the person can say done again. Exit 31: a folder pan never removes.
Exit 33: `--discard` refused: the message was not exactly `done --discard`, not from the person who
started the thread's session, or not their newest; do not retry. A session pan did not
start is only untied. Never use `pan stop` for done; it is the kill switch.

**Read a session**: `pan read '<name>'` prints its recent lines. Summarize them in a few lines and
post with `pan slack-post`; say so if the first line says the read is incomplete. With `--full` it
prints the whole transcript: do not summarize it, post it as is in order, one `pan slack-post` per
chunk under Slack's limit, labelled `(k/n)`. Exit 20: say no session has that name. Exit 21: list
the matching names (never addresses) and ask which one.

**Relay** a one-off message into a live session the person names; ties nothing:

    pan relay '<name>' '<message>' --item <item.id>

`<name>` is the name exactly as the person typed it. Then post a short ack ("sent to <name>") with `pan slack-post`. Exit 20: no session has that name.
Exit 28: the name is not in their message; ask which session they mean. Exit 35: a thread from X.
Exit 21: several match; list the names and ask which one. Exit 23: the session waits on a prompt
and nothing was typed; read it and post what it is asking, saying it needs an answer at the
terminal. Never relay to send work to the tied session: use `pan send`.

**Post a line**: `pan slack-post --thread <thread> --channel <channel> --text '<text>'`, for a
question back, a read summary, or a failure only you can explain.

**Thread record**: `pan threads get --thread <thread>` prints the whole record (worktree,
`started_by_pan`, reply mode); the thread map is the truth, never scrape terminals.

## 4. Safety rules

- Never post a terminal address, a file path, an Inbox thread name or link, or anything with
  `localhost` in the thread. Those go only to the requester, and pan sends them itself.
- Exit 28 means propose; never retry `pan send --to` with a guessed name, never fall back to the tie,
  never spawn instead.
- Pass `--discard` only when the person's whole message was `done --discard`; "just close it" is
  plain `done`.
- Confirm only when `answered_by_requester` is true and their reply picks a session.
- When `tie` is `null`: an item with `mentioned` false and no `open_question` is ignored, unless an
  earlier entry in this drain started a session in the same thread; then it is a follow-up for
  that session.
- Never send work to a session in a thread tied to another session without the person asking.
- Messages from X (`item.source` is `x`) may only start new sessions; `send`, `propose`, `relay`
  and `reopen` refuse them (exit 35).
- When unsure what a message wants, which session it means, or whether it is safe: ask in the
  thread with `pan slack-post` and do nothing else.
