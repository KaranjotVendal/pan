# TODOS.md — bug and discovery queue

This is the working queue the Night Shift agent consults before picking the next plan feature (see
`AGENT_LOOP.md` step 1). Rules:

- Open bugs here are picked BEFORE the next unchecked plan task.
- Unrelated problems discovered while working a task are APPENDED here, not fixed inline — no scope
  creep. The current task ships only its own behavior.
- Each entry: a short title, where it was found, what is wrong, and (if known) the fix direction.
  Move an entry to Done when its fix is committed, or delete it.

## Open bugs

_(none yet)_

## Discoveries / follow-ups

_(none yet)_

## Deferred to final human session (live)

These cannot run overnight — they need a real Slack connection, the phone, or a live-verify write
path — and are intentionally out of the Night Shift agent's reach. Hand them to the human for the
final session.

- **Task 20 live smoke (real Slack + phone).** From the phone: `@pan create /tmp/pan-hello.txt
  containing today's date, then tell me the contents`. Verify the `:eyes:` ack within ~1s, a worker
  spawning in its own fresh git worktree + herdr workspace, the file created and its contents posted
  back in-thread, a follow-up reply routing to the same worker, and `@pan --status` reporting the
  worker's live state. Gateway / spawn / hooks are not considered done until this passes.
- **R-5 — Slack Socket Mode retry / event-id stability (verify-live-api).** Confirm the Slack event
  id is stable across redelivery-after-wake so the inbox dedupe (INV-6) actually absorbs duplicates.
  Mocks cannot reproduce real Socket Mode retry payloads.
- **R-7 — morcli handle binding (verify-live-api).** Confirm whether `ThreadRecord.morcli_session`
  is known at launch or must be resolved on first status query, against a real morcli session.
- **R-6 — inbox append/drain race under live burst.** The atomic-drain design is unit-tested with
  fakes; confirm it is race-free under a real burst of concurrent mentions plus the watcher and
  `pan inbox drain` all touching the directory.
