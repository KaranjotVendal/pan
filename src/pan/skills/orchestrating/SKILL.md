---
name: orchestrating
description: Use on every inbox nudge in the persistent pan-orchestrator session — drain the pan inbox, read each item's deterministically-parsed directive, and route it (answer a status query, relay to a live worker, or spawn a new worker). Every mechanical step is a `pan` subcommand; the model makes only the fuzzy reuse-vs-spawn / repo / task-comprehension calls.
---

# Orchestrating loop

You are the persistent `pan-orchestrator` Claude session. A filesystem watcher wakes you with a
fixed, content-free nudge whenever the inbox changes (INV-2) — the actual request never arrives
through your pane, only through the durable inbox. On each nudge, run this loop once.

You own **only** the fuzzy judgments: whether a finished thread should be reopened or spawned fresh,
which repo a task belongs to, and comprehending the task well enough to write the worker's brief.
**Every other step is a deterministic `pan` subcommand** — never reimplement parsing, spawning,
state, or posting by hand, and never decide a task's *mode* yourself.

## 1. Drain the inbox

Shell:

    pan inbox drain --json

This atomically drains and clears the inbox (exactly-once, INV-6) and prints a JSON list. Each entry
has two objects:

- `item` — the inbox event (`id`, `slack_user`, `channel`, `thread_ts`, `is_thread_reply`,
  `raw_text`, `received_at`).
- `directive` — the result of the deterministic `parse_directive` run in code: `mode`
  (`delegate`/`sync`/`status`), `force_new`, `target_stream`, `agent`, and `cleaned_text` (the raw
  text with every recognized flag stripped — this is the worker brief / relay text).

If the list is empty, stop — there is nothing to do. Process the entries in the order returned.

## 2. The mode is already decided (INV-3)

Read `directive.mode` and `directive.cleaned_text` straight off each entry. The mode was fixed by
`parse_directive` (pure string logic) in the `pan inbox drain` command, **not by you** — do not
re-read `raw_text` and re-derive the mode, and never upgrade a `delegate` to a `sync` (or vice
versa) because the wording "feels" urgent. That is exactly the judgment INV-3 forbids; the
`directive` block is authoritative.

## 3. Look up the thread binding

The thread map is the single source of truth for the thread→worker binding (INV-7). For an item's
`thread_ts`, shell:

    pan threads get --thread <thread_ts>

It prints the `ThreadRecord` JSON (with `status`, `pane_ids`, `workspace_name`, `worktree_path`,
`morcli_session`) or `null` if no worker is bound to that thread yet. Never rediscover a worker by
scraping panes — the thread map is authoritative.

## 4. Route the item

Apply this precedence ladder, top to bottom, using `directive.mode`, `directive.force_new`, and the
binding from step 3:

### (a) Status query — `directive.mode == "status"` (checked FIRST, regardless of binding)

Report the worker's live state from morcli; touch no worker. A status request carries no prose
(`cleaned_text` is empty), so it must never be relayed into a pane. Shell:

    pan status --thread <thread_ts>

Then post the result back to the thread through the single egress path (INV-4):

    pan slack-post --thread <thread_ts> --channel <channel> --text '<status summary>'

If no worker is bound to the thread (`pan threads get` returned `null`, so `pan status` would error),
instead post a short "no active worker for this thread" reply via `pan slack-post` — do not spawn.

### (b) Follow-up to a live worker — a record exists with `status` spawning/running/blocked

Relay `cleaned_text` into that worker's pane. Read `pane_ids[0]` from the record and send it:

    herdr pane send-text <pane_id> '<cleaned_text>'
    herdr pane send-keys <pane_id> Enter

A live relay takes precedence over `force_new`: a follow-up to a still-live worker is relayed here,
not spawned — spawning would overwrite the single thread→worker binding (INV-7) and orphan the live
pane. `force_new` only forces the spawn path for a finished or absent thread (rung (c)).

### (c) A finished/absent thread — a new task, or `directive.force_new` is true

Spawn a fresh worker. If `directive.force_new` is true, always take this path (skip the reuse
judgment). Otherwise, if a record exists but is `done`/`failed`, this is **your fuzzy call**: reopen
the finished worker (relay as in (b)) or spawn fresh. Decide from the task and how long ago it
finished; when in doubt, spawn fresh — worktree isolation makes a new worker cheap and safe.

To spawn, comprehend the task and pick the repo (**your fuzzy calls**), then:

    pan spawn --thread <thread_ts> --task '<cleaned_text>' --channel <channel> [--stream <target_stream>] [--repo <path>]

`pan spawn` creates the git worktree + herdr workspace, launches the worker Claude session, records
the `SPAWNING` `ThreadRecord`, and posts the "on it — stream pan-…" ack through the single Slack
egress path (INV-4) — all mechanically. You do not post the ack yourself.

- **DELEGATE** (`directive.mode == "delegate"`): spawn and move on. The worker posts progress and its
  final result back to the thread later via its Stop/Notification hooks.
- **SYNC** (`directive.mode == "sync"`): spawn, then **block this item** until the worker's Stop hook
  fires (which posts the final summary and marks the thread `done`). Do not pick up unrelated new
  work for this thread until it completes; a SYNC request wants one substantive reply.

## What is mechanical vs. what you decide

Mechanical (always a `pan` subcommand — never hand-rolled): draining the inbox, reading the parsed
directive/mode, reading/writing the thread map, spawning, posting to Slack, querying status. The
gateway already authenticated the sender and added the `:eyes:` ack before the item reached the
inbox — you never re-authenticate. Relaying into a live worker's pane is the one step done with
`herdr` directly (there is no `pan` command for it yet).

Yours to judge (and the only place model reasoning belongs): reuse-vs-spawn for a finished thread
(when `force_new` is not set), which repo a task targets, and writing a clear worker brief from the
task prose. Keep every other step deterministic.
