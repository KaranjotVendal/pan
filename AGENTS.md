# AGENTS.md — router for building pan

This is a router, not a copy of the specs. It points you at where the truth lives and states the
conventions that are non-negotiable. Read it first, then load only the slice of the specs the
current task needs.

## What pan is

`pan` is a Slack-driven laptop coding-agent orchestrator. A thin always-on Bolt Socket Mode gateway
authenticates the sender, adds a fast `:eyes:` reaction, and appends a normalized event to an
on-disk inbox — it never classifies intent. A filesystem watcher nudges a persistent orchestrator
Claude session (fixed content-free nudge only), which drains the inbox, parses deterministic
directive flags, decides reuse-vs-spawn, and runs each task as its own Claude session in an isolated
git worktree plus herdr workspace. Workers post progress and results back to the originating Slack
thread through one `slack-post` egress path plus Stop and Notification hooks.

## Repository map

Current modules (Task 1, built):

- `src/pan/__init__.py` — package marker.
- `src/pan/logging.py` — `initialise_logger` (file DEBUG, console on stderr from `PAN_LOG_LEVEL`,
  default WARNING; idempotent; `propagate=False`).
- `src/pan/errors.py` — the `PanError` taxonomy (the original nine subclasses plus `MorcliError`,
  added in the morcli-adapter task for morcli subprocess failures — a deliberate extension of the
  tech-spec's 9-item list).
- `src/pan/seams.py` — the single import point for every seam `Protocol` (`Clock`, `IdGen`,
  `InboxStore`, `ThreadMap`, `HerdrAdapter`, `GitWorktreeAdapter`, `MorcliAdapter`, `SlackAdapter`,
  `AgentLauncher`, `InboxWatcher` — all seams now defined).
- `src/pan/adapters/__init__.py`, `src/pan/adapters/clock.py` — `SystemClock`, `UuidGen`.
- `src/pan/models.py` — enums (`TaskMode`, `WorkerStatus`, `Autonomy`, `Agent`) and domain/config
  pydantic models (`Directive`, `InboxItem`, `ThreadRecord`, `SlackCredentials`, `PanConfig`, ...).
- `src/pan/directive.py` — `parse_directive`, pure deterministic flag parsing (INV-3).
- `src/pan/config.py` — `load_config` composition root over `~/.config/pan/config.json` (BR-4).
- `src/pan/credentials.py` — load/save `SlackCredentials` at `~/.pan/credentials.json`, atomic 0600
  write; sole sanctioned `.get_secret_value()` call site outside Slack-client construction (BR-3).

- `src/pan/inbox.py` — `FileInboxStore`, atomic claim/rename drain, event-id dedupe (INV-6, R-6).
- `src/pan/threadmap.py` — `FileThreadMap` over `~/.pan/threads.json`, sole thread-to-worker binding
  (INV-7); `Clock` injected for deterministic `updated_at`.

- `src/pan/adapters/herdr.py` — `ShellHerdrAdapter`, shells the real `herdr` CLI (BR-2, INV-8).
- `src/pan/adapters/git_worktree.py` — `ShellGitWorktreeAdapter`, shells `git worktree`; `--force`
  teardown; branch-escape guard (BR-2, INV-8).
- `src/pan/adapters/morcli.py` — `ShellMorcliAdapter`, shells `morcli streams --json`, maps agent
  status to `WorkerStatus` (BR-2, INV-8); raises `MorcliError`.

- `src/pan/gateway/__init__.py`, `src/pan/gateway/auth.py` — pure `auth_check` (safety gate 2;
  fail-closed allowlist + channel policy; no Slack SDK import).

- `src/pan/gateway/app.py` — `BoltSlackAdapter`, Bolt Socket Mode gateway; `handle_event` auth →
  `:eyes:` ack → inbox append (INV-1); sole Slack SDK importer + client-construction point (BR-1/BR-3).

- `src/pan/gateway/slack_post.py` — `slack_post`, the single Slack egress path (INV-4); value-free.

- `src/pan/spawn.py` — `ClaudeLauncher` + `spawn_worker`: worktree → workspace → launch → thread-map
  put → ack (INV-4/INV-7); `AgentLauncher.launch(worktree, pane_id, brief) -> None`.

- `src/pan/watcher.py` — `WatchdogInboxWatcher`, fixed content-free nudge to the orchestrator (INV-2).

- `src/pan/hooks/{stop,notification}.py` — Stop/Notification hook cores: parse hook JSON → resolve
  thread → post via `slack_post` → mark DONE/BLOCKED; `thread_ts`/`channel` injected, `stdin` seam.

Planned modules (per the plan, one line each):

- `src/pan/hooks/pretooluse_gate.py` — dormant gated-ops PreToolUse gate (Task 17).
- `src/pan/cli.py`, `src/pan/__main__.py` — Typer app, sub-apps, single `_run` error boundary (Task 18).
- `src/pan/skills/orchestrating/SKILL.md` — drain-classify-route loop prose (Task 19).
- `src/pan/power.py`, `launchd/*.plist.template`, `README.md` — always-on and smoke (Task 20).

Other locations:

- `tests/unit/` — all unit and component tests (mocked seams).
- `docs/superpowers/specs/` — the tech spec and design spec.
- `docs/superpowers/plans/` — the plan (the task queue).
- `docs/superpowers/night-reports/` — the morning reports you write at end of run.
- Control files at the repo root: `AGENT_LOOP.md`, `AGENTS.md`, `REVIEW_PERSONAS.md`, `TODOS.md`,
  `CHANGELOG.md`.

## Where the truth lives

- Task queue and step-by-step order: the plan, `docs/superpowers/plans/2026-07-16-pan.md`.
- Contracts, invariants (INV-1..9, the "Invariants" section), boundary rules (BR-1..6, the "Boundary
  Rules" section), signatures, and call stacks: the tech spec,
  `docs/superpowers/specs/2026-07-16-pan-tech-spec.md`.
- Product rationale, message semantics, safety-gate design: the design spec,
  `docs/superpowers/specs/2026-07-16-pan-slack-orchestrator-design.md`.

## Non-negotiable coding conventions

Full standard: `/Users/karanjot.vendal/.claude/skills/coding-standards/SKILL.md`. The essentials:

- `from __future__ import annotations` at the top of every module. No module-level docstrings.
- `X | None`, never `Optional`; built-in generics (`list[str]`, `dict[str, int]`); `typing.Self` for
  self-returns.
- pydantic models by default; frozen where the value is immutable. `pathlib.Path` for paths, never
  `os.path`.
- Errors are named `PanError` subclasses; translate them to exit codes at the CLI boundary
  (`cli._run`) and drop/translate at the Bolt boundary. Bare `Exception` is for defects only.
- Logging via `initialise_logger`; value-free f-string logs (no payload body, no secret ever an
  argument); console handler on stderr, level from `PAN_LOG_LEVEL` default WARNING. No bare
  `import logging`.
- No `print()` in library code (the CLI stdout entrypoints and the hooks that emit decision JSON are
  the only exceptions).
- Descriptive names; avoid terse abbreviations (`idx` and `e` are the accepted exceptions).
- Tokens are typed `SecretStr`. Secrets live in `~/.pan/credentials.json` at mode `0600` — NOT the
  OS keychain. This is a deliberate deviation from coding-standards Principle 6 (the keychain is
  macOS-specific and non-portable to a Linux VPS), enforced by BR-3.

## Vendor confinement

- `slack_bolt` / `slack_sdk` may be imported ONLY under `src/pan/gateway/` (BR-1).
- `herdr`, `git`, and `morcli` are shelled ONLY inside their own adapter in `src/pan/adapters/`
  (BR-2). Vendor types never cross an adapter boundary (INV-8).
- `.get_secret_value()` only in `credentials.py` and the single Slack-client construction point
  (BR-3). `os.getenv` / `os.environ` only in `config.py` (BR-4).

## Testing conventions

- TDD: write the failing test first, then the minimal code.
- Test through real seams with fakes; assert on outputs, request bodies, and persisted state — not
  on private helpers or incidental call order.
- Parametrize sibling cases. Skip pure pydantic dump-then-validate round-trip tests (`ty` covers
  field shape); a disk round-trip that exercises our serialization wiring still earns its test.
- MOCKED SEAMS ONLY overnight. No live Slack, Socket Mode, network, or phone in the test suite.

## Gate commands

Run all four; all must be green before staging:

- `uv run ruff check`
- `uv run ruff format --check`
- `uv run ty check`
- `uv run pytest -q`

## Pointers

- The nightly workflow: `AGENT_LOOP.md`.
- The six review personas dispatched in the review step: `REVIEW_PERSONAS.md`.
