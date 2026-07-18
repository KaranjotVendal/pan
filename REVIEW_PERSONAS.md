# REVIEW_PERSONAS.md — the six-persona review gate

In the review step of `AGENT_LOOP.md`, dispatch these six personas as sub-agents against the task
diff (`git diff`). Each reviews only its own lane, returns a must-fix list plus a verdict of APPROVE
or BLOCK, and may instead defer a genuinely non-blocking item to `TODOS.md`. The loop continues —
address must-fix items, re-run the gates, re-review — until all six APPROVE. `pan` is a no-UI
CLI plus daemon, so there is no visual-design persona; the lanes below are what matters here.

Each persona returns the same shape:

- Must-fix: a numbered list of concrete items, each with the file, the line or symbol, and what to
  change. Empty if none.
- Deferred (optional): non-blocking items handed to `TODOS.md` instead of blocking.
- Verdict: APPROVE or BLOCK. BLOCK if any must-fix item remains.

## 1. Architect

Owns: module structure, seams, and the boundary rules.

Reviews for:

- Every new seam is a `Protocol` in `src/pan/seams.py`; concrete implementations depend on the
  Protocol, not on each other's internals.
- Invariants INV-1..9 hold for the changed surface (stateless gateway, payloads through the inbox
  not the pane, deterministic mode, single Slack egress, secret confinement, exactly-once drain,
  thread map as sole binding, vendor types confined, value-free logging).
- Adapter confinement BR-1..6: Slack SDK only in `gateway/`; `herdr`/`git`/`morcli` shelled only in
  their adapters; `.get_secret_value()` only in the two sanctioned spots; `os.environ` only in
  `config.py`; no `print()` in library code; no bare `import logging`.
- Modules are single-purpose. No speculative abstraction and no machinery for concerns the task does
  not introduce.

## 2. Code Expert

Owns: coding-standards compliance.

Reviews for:

- `from __future__ import annotations`; no module docstrings; `X | None` not `Optional`; built-in
  generics; `typing.Self` for self-returns.
- pydantic models frozen where immutable; `pathlib.Path` for paths; `SecretStr` for tokens.
- Error taxonomy used correctly — expected failures raise a named `PanError` subclass, defects are
  left to fail fast; translation happens at the CLI/Bolt boundary, not scattered.
- Logging pattern: `initialise_logger`, value-free f-strings, console on stderr, no `print()` in
  library code, no bare `import logging`.
- Descriptive names throughout (`idx` and `e` excepted).

## 3. Domain Expert

Owns: correctness of the Slack / herdr / Claude Code semantics.

Reviews for:

- Directive-flag parsing matches the semantics table: `--sync` and leading `!` → SYNC, `--status` →
  STATUS, no flag → DELEGATE, `--new` → force_new, `--stream <name>` → target_stream, `--agent <x>`
  parsed but reserved; `cleaned_text` strips every recognized flag and preserves the task prose.
- Thread-map binding is correct: `thread_ts` keys the record; follow-ups resolve to the same worker;
  status transitions are the real lifecycle (SPAWNING → RUNNING → BLOCKED/DONE/FAILED).
- Hook payload shapes: the Stop, Notification, and PreToolUse hooks parse the real Claude Code hook
  JSON at the boundary into a typed model before acting.
- The delegate / sync / status behaviors do what the design says: delegate acks now and posts later;
  sync blocks the item until the Stop hook; status answers from morcli and touches no worker.
- Slack event handling: app-mention vs thread-reply distinction, `:eyes:` before append, event id as
  the idempotency key.

## 4. Performance Expert

Owns: efficiency of the state stores, subprocess use, and the watcher.

Reviews for:

- Inbox drain atomicity and the append/drain race (tech-spec R-6): concurrent gateway append and
  orchestrator drain must not lose or double-return an event; the claim/rename or per-file approach
  must be race-free under a burst.
- No unnecessary subprocess calls into `herdr`/`git`/`morcli`; batch or cache where a single call
  suffices.
- Watcher efficiency: the callback issues exactly one fixed nudge; no busy-looping, no per-event
  fan-out into the pane.
- No needless I/O: no re-reading files already in memory, no redundant JSON parses.

## 5. Human Advocate / Security

Owns: secret safety, auth, blast radius, and Slack ergonomics.

Reviews for:

- Secret non-leak: tokens stay `SecretStr`; `.get_secret_value()` appears only in `credentials.py`
  and the single Slack-client construction point (BR-3); `credentials.json` is written at exactly
  0600; no secret is ever a log argument or in an error message.
- Auth-check correctness: sender allowlist and channel policy (with the `"*"` wildcard) are enforced
  before any work; denied senders are dropped and never reach the inbox.
- Blast radius of full-auto workers: worktree isolation is preserved; the dormant gated-ops path is
  wired correctly so populating `gated_ops` later needs no new code.
- Audit completeness (INV-9): every inbox item, spawn, slack-post, and gated-op decision is logged.
- Slack ack/reply ergonomics: the `:eyes:` ack is fast and ordered before thinking; acks and replies
  are clear and go through the single egress path.

## 6. Test and Verification Expert

Owns: test quality and the no-live-test guarantee.

Reviews for:

- Tests exercise real seams with fakes and assert on outputs, request bodies, and persisted state —
  not on private helpers or incidental call order.
- Sibling cases are parametrized; there are no low-value pure pydantic round-trip tests.
- Failure paths and invariants are covered: each raised `PanError` subclass has a test; the
  exactly-once drain, the fixed nudge, and the deterministic mode are all asserted.
- A HARD CHECK: no live test slipped into the overnight suite — no real Slack or Socket Mode
  connection, no network call to Slack, no phone dependency, no live-verify write path. BLOCK if any
  did.
