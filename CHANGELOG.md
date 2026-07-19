# Changelog

All notable changes to `pan` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
Semantic Versioning. The Night Shift build agent appends one entry per task under `## [Unreleased]`
as it works (see `AGENT_LOOP.md` step 9).

## [Unreleased]

### Added

- `pan sessions` — a reconciled "what's running" view (Task 26, Milestone M10). It enumerates ALL
  live herdr claude sessions (a new `HerdrAdapter.list_workspaces()` shelling `herdr workspace list`
  plus a `pane list` per workspace, mapping vendor JSON to the confined `LiveSession` domain type —
  INV-8), left-joins the pan thread map to identify pan-owned sessions (by `workspace_name`, with a
  symlink-safe resolved-cwd fallback), and FLAGS status drift between pan's recorded `WorkerStatus`
  and herdr's live `AgentStatus` without healing it (report-only in v1; INV-7 stays read-only). The
  reconcile lives in a new pure `collect_sessions(herdr, thread_map, morcli)` core in
  `src/pan/sessions.py` with a directly-tested `session_drift` predicate (idle/working→running,
  blocked→blocked, done→done; `SPAWNING` and `UNKNOWN` excluded; the live-test case pan-`blocked`
  vs herdr-`idle` is drift). morcli enrichment is best-effort and tolerates `MorcliError` so a
  morcli hiccup never drops a session. New models: `AgentStatus` (StrEnum, vocabulary flagged R-9 as
  unverified against live herdr), frozen `LiveSession` and `SessionSummary`; new `ThreadMap.records()`
  read accessor. The command prints a human table or, with `--json`, the `SessionSummary` list for
  the orchestrator. Added tests: herdr `list_workspaces` argv/parse/agent-status-mapping/malformed
  and missing-cwd branches; the `collect_sessions` reconcile (pan-owned by name, resolved-cwd
  fallback, external, records-present-no-match, drift cross-product, morcli-error tolerated, no-morcli)
  and the `session_drift` matrix; and the `pan sessions` `--json` and human-table CLI paths.
- The `@pan sessions` Slack path and real morcli session linkage (Task 27, Milestone M10). Added a
  `TaskMode.SESSIONS` mode and taught `parse_directive` to route it: the explicit `--sessions` flag
  (canonical) and a fixed, deterministic soft-trigger phrase set ("what's running", "list threads",
  "list all the threads", ...) matched by pure string logic (INV-3), never model judgment. Precedence
  is deterministic — an explicit `--sessions`/`--status` flag is authoritative (`--sessions` wins
  when both appear), and the soft trigger outranks `--sync`/delegate but not an explicit `--status`.
  The orchestrating skill gained a sessions route that runs `pan sessions --json`, formats the result,
  and posts it in-thread through the single Slack egress (INV-4), touching no worker. Closed the R-7
  null-`morcli_session` gap: `spawn_worker` now takes a `morcli` adapter and best-effort captures the
  session handle at spawn via the new `MorcliAdapter.resolve_session(workspace_id) -> str | None`
  (returns None on the indexing lag, tolerates a `MorcliError` so a morcli hiccup never fails the
  already-launched worker). Added tests: the directive `--sessions`/soft-trigger/precedence cases;
  `resolve_session` returns the session id / None on no-match / raises `MorcliError` on subprocess
  failure; and `spawn_worker` captures the handle, tolerates the lag (None), and tolerates a
  `MorcliError` without failing the spawn.

  DEFERRED to a human session (mocks cannot exercise it): the live `@pan sessions` round-trip — from
  the phone, the orchestrator running `pan sessions --json` and posting the reconciled summary through
  the real Slack egress.

### Changed

- Reconciled the tech spec (`docs/superpowers/specs/2026-07-16-pan-tech-spec.md`) with what was
  actually built (Task 25, Milestone M9 — doc-only, no code changed; the spec is a local working
  artifact). Brought the contracts back in line with the implementation: added `MorcliError` to the
  exception taxonomy (now ten, mapped to exit 19 at the CLI boundary) and mirrored `errors.py`
  declaration order; added `channel: str` to the `ThreadRecord` domain model; corrected the seam and
  CLI signatures that changed during the build — `AgentLauncher.launch(worktree, pane_id, brief) ->
  None`, `ThreadMap.get_by_worktree`, `FileThreadMap(threads_path, clock)`,
  `WatchdogInboxWatcher(herdr, orchestrator_pane_id, inbox_dir)`, `status`/`stop` keyed on
  `--thread`, `spawn`/`slack-post` taking `--channel`, `threads set` requiring `--status`
  (update-only), and the new `pan watcher` and `pan hook stop|notification` commands; and recorded
  the corrected behaviors (the watcher sends a fixed `WAKE_INSTRUCTION` then Enter rather than a bare
  Enter, `ShellHerdrAdapter._run` uses `expect_json=False` for no-output commands, the thread-map
  read tolerates legacy/malformed records, and `get_by_worktree` matches on resolved paths).

### Fixed

- Completion-hook cwd match now resolves symlinks (Task 24, Milestone M9).
  `FileThreadMap.get_by_worktree` compared the worker's cwd (delivered by a Claude Code Stop/
  Notification hook) to the stored `worktree_path` by exact string equality, which silently no-ops
  when the two differ only by a symlinked prefix — on macOS the hook's cwd arrives as
  `/private/tmp/...` while the stored path was recorded as `/tmp/...`, so the hook found no record,
  posted nothing, and never marked the thread DONE/BLOCKED. The lookup now compares `Path.resolve()`
  on both sides (symmetric, so it doesn't matter which side carries the symlink), keeping the
  normalization in one place so the CLI hook boundary stays free of path handling. INV-7 (the thread
  map as the sole binding) is preserved.

### Added

- Caffeinate policy helper, launchd templates, and README (Task 23, Milestone M9) — the buildable,
  non-live parts of the original always-on Task 20:
  - `should_caffeinate(active_worker_count, on_ac_power)` in `src/pan/power.py`: a pure R-3 policy
    predicate returning True only when on AC power AND at least one worker is active (so the machine
    is never forced awake for nothing, and never on battery). The actuation that would call
    `caffeinate`/`pmset disablesleep 1` off this decision stays deferred to the live human session;
    this milestone ships only the decision, and the README says so.
  - `launchd/com.pan.gateway.plist.template` and `launchd/com.pan.watcher.plist.template`:
    LaunchAgent templates for the two always-on processes (`pan gateway`, `pan watcher`), both
    `KeepAlive=true`/`RunAtLoad=true`, with a single `<PAN_HOME>` placeholder and no secrets (tokens
    stay in `~/.pan/credentials.json` at 0600).
  - `README.md`: install (`uv tool install --editable .`), the Slack app manifest at
    `slack/manifest.yaml`, `pan config set-token`, the `~/.config/pan/config.json` shape, loading the
    two LaunchAgents, the CLI command table, the directive-flag reference, and the example first task.

- `pan watcher` CLI command (Task 22, Milestone M9). The inbox watcher had no first-class
  entrypoint (it was run via an ad-hoc script during the smoke); it now has one. `pan watcher` builds
  `ShellHerdrAdapter` + `WatchdogInboxWatcher` from config (`orchestrator.pane_id`, `paths.inbox`) and
  calls `start()` — a blocking, launchd-supervised long-runner just like `pan gateway`. The
  `WatchdogInboxWatcher.start()` observer wiring and the `_InboxEventHandler` event forwarding, both
  previously `# pragma: no cover`, are now unit-tested by mocking the `watchdog` Observer (no real
  filesystem-event timing), and the pragmas are removed.

- Worker completion hooks wired for Slack auto-reply (Task 21, Milestone M8). Post-smoke, the worker
  now replies its result to the originating thread on finish and asks its question when blocked:
  - `ThreadRecord.channel: str` — the binding now carries the Slack channel so a completion hook can
    reply without being handed a channel out of band.
  - `ThreadMap.get_by_worktree(worktree_path)` (Protocol + `FileThreadMap`) — resolves the record
    whose `worktree_path` matches, so a hook finds its thread from the worker's cwd (the thread map
    stays the single source of truth, INV-7).
  - `spawn_worker` sets `channel` on both the success and failed `ThreadRecord`, and writes the worker
    `.claude/settings.json` into the worktree (before launch) registering the Claude Code `Stop` and
    `Notification` hooks to run `pan hook stop` / `pan hook notification`. The settings schema was
    confirmed against current Claude Code docs.
  - `pan hook stop` / `pan hook notification` CLI commands — each reads the hook JSON from stdin,
    extracts `cwd`, resolves the `ThreadRecord` via `get_by_worktree`, and calls the existing
    `stop_hook` / `notification_hook` with the record's `thread_ts` + `channel` (Stop → posts the
    summary and marks DONE; Notification → posts the question and marks BLOCKED). They reuse the same
    Slack-adapter construction as `slack-post` (no Socket Mode in a hook, single egress INV-4), and a
    hook that can't resolve its thread (unknown cwd or unparseable stdin) exits cleanly without
    posting so it never crashes the worker. Live-verify of the real hook invocation is deferred to a
    human session.

- Project scaffold: `uv` + `uv_build` packaging (`pyproject.toml`, `.python-version`, `.gitignore`
  excluding `/docs/*`) with the Typer console-script entry, runtime deps (`typer`, `pydantic`,
  `slack_bolt`, `watchdog`) and dev deps (`pytest`, `respx`, `ruff`, `ty`).
- `initialise_logger` (`src/pan/logging.py`): CLI-adapted logging — file handler at DEBUG under
  `~/.pan/logs`, console handler on stderr with level from `PAN_LOG_LEVEL` (default WARNING),
  idempotent, `propagate=False`.
- `PanError` taxonomy (`src/pan/errors.py`): the base plus `UnauthorizedSenderError`,
  `ConfigMissingError`, `CredentialsError`, `InboxError`, `ThreadNotFoundError`, `SpawnError`,
  `HerdrError`, `SlackPostError`, `GatedOpDeniedError`.
- Seam Protocols (`src/pan/seams.py`): `Clock` and `IdGen` as the single import point for injectable
  seams (the remaining seam Protocols land here as their tasks arrive).
- Clock and id-generator adapters (`src/pan/adapters/clock.py`): `SystemClock` and `UuidGen`.
- Domain and config models (`src/pan/models.py`): the `TaskMode`, `WorkerStatus`, `Autonomy`, and
  `Agent` `StrEnum`s; the frozen domain models `Directive` and `InboxItem` and the mutable
  `ThreadRecord`; and the config models `SlackCredentials`, `SlackConfig`, `UserPolicy`,
  `OrchestratorConfig`, `Defaults`, `PanPaths`, and the composed `PanConfig`. Tokens are typed
  `SecretStr`, paths are `pathlib.Path`, and models are frozen wherever the value is immutable so the
  gateway, config loader, and stores can parse untrusted JSON into typed domain objects at the
  boundary.
- `parse_directive` (`src/pan/directive.py`): pure, deterministic flag parser that maps a Slack
  message to a `Directive` — `--sync` / leading `!` → SYNC, `--status` → STATUS, no mode flag →
  DELEGATE (with STATUS winning over SYNC when both appear); `--new` → `force_new`; `--stream <name>`
  → `target_stream`; `--agent <x>` parsed but reserved for v2. `cleaned_text` strips every recognized
  flag (including a dangling value-flag and a value-flag whose value is another flag) while preserving
  the task prose. No I/O and no model judgment, so `TaskMode` is deterministic per INV-3.
- `load_config` (`src/pan/config.py`): the single config composition root. Reads
  `~/.config/pan/config.json` (or an explicit path), parses the JSON at the boundary into a
  `PanConfig`, expands `~` in the path-bearing fields (`paths.*` and `orchestrator.worktree_base`),
  and falls back to model defaults for omitted optional sections. A missing file, non-object or
  malformed JSON, or a missing required field is translated to `ConfigMissingError`; error messages
  carry only the path, never file contents. `PanConfig` holds no secrets (tokens live in
  `credentials.json`), so nothing here calls `.get_secret_value()`.
- Credentials store (`src/pan/credentials.py`): `save_credentials` / `load_credentials` over
  `~/.pan/credentials.json` (the gccli-style 0600 file, not the OS keychain — the deliberate,
  BR-3-enforced deviation for Linux-VPS portability). `save_credentials` is the sole sanctioned
  `.get_secret_value()` call site outside Slack-client construction; it writes the token payload to a
  fresh `O_EXCL` temp file created at 0600 and atomically `os.replace`s it into place, so a secret
  never touches disk at looser-than-0600 permissions even when overwriting a pre-existing loose file.
  `load_credentials` parses the JSON at the boundary into `SlackCredentials` (`SecretStr` fields),
  raises `CredentialsError` on a missing or malformed file, and warns (via the named logger, mode and
  path only — never a token) when the file is group/other-readable. Tokens stay `SecretStr` end to
  end and mask as `**********` in repr/str.
- `FileInboxStore` (`src/pan/inbox.py`), implementing the new `InboxStore` seam Protocol (added to
  `src/pan/seams.py`): a file-per-event on-disk inbox over `~/.pan/inbox/`. `append` writes each event
  to `{event_id}.json` via a temp-file + atomic replace, using the Slack event id as the filename so a
  redelivered event before a drain resolves to one file and is drained exactly once (INV-6); it rejects
  an event id that is not a safe single path segment (no `/`, `\`, `..`, NUL) so a hostile id cannot
  escape the inbox directory. `drain` claims each entry with an atomic rename (so a concurrent gateway
  append or a second drainer cannot lose or double-return an event — R-6), returns the items ordered by
  `received_at`, and empties the store. A malformed entry is quarantined to `.corrupt` and surfaced as
  `InboxError`, while cleanly-parsed siblings are restored for a later drain rather than lost. Logs are
  value-free (id/channel/count only, never `raw_text`).
- `FileThreadMap` (`src/pan/threadmap.py`), implementing the new `ThreadMap` seam Protocol (added to
  `src/pan/seams.py`): the single source of truth for the thread-to-worker binding (INV-7), a JSON
  object at `~/.pan/threads.json` keyed by `thread_ts`. `put` upserts a `ThreadRecord`, `get` returns
  the record or `None`, and `update_status` transitions the worker status and bumps `updated_at` from
  an injected `Clock` seam (so the timestamp is deterministic in tests), preserving every other field;
  it raises `ThreadNotFoundError` when the thread is unknown. Writes are atomic (temp-file + replace)
  and `Path`/enum/`datetime` fields survive the disk round-trip. Logs are value-free (thread/status).
- `ShellHerdrAdapter` (`src/pan/adapters/herdr.py`), implementing the new `HerdrAdapter` seam Protocol
  (added to `src/pan/seams.py`): shells the real `herdr` CLI over its JSON socket-API helpers, the only
  module permitted to invoke `herdr`/`subprocess` (BR-2). `create_workspace` runs `herdr workspace
  create --cwd <path> --label <label> --no-focus` and, because that command returns only workspace
  metadata (no pane id), resolves the created pane with a follow-up `herdr pane list --workspace <id>`
  (selecting the pane in the workspace's active tab, else the first pane), returning `(workspace_id,
  pane_id)`. `nudge` issues the fixed, content-free `herdr pane send-keys <pane> Enter` (no payload
  crosses the pane — INV-2); `send_text`/`kill_pane` map to `pane send-text` / `pane close`. All herdr
  JSON is parsed inside the adapter and only plain `pan` domain values cross the boundary (INV-8);
  subprocess failures, non-JSON output, and OS errors become `HerdrError`. Logs are value-free (pane/
  workspace ids and text length only — never the message body). The exact nudge keystroke and JSON key
  names are flagged for live reconfirmation (R-2).
- `ShellGitWorktreeAdapter` (`src/pan/adapters/git_worktree.py`), implementing the new
  `GitWorktreeAdapter` seam Protocol: the only module permitted to shell `git` (BR-2).
  `create_worktree(repo, branch, base)` runs `git -C <repo> worktree add <base>/<branch> -b <branch>`
  and returns the worktree `Path`, rejecting (`SpawnError`) a branch whose target would escape `base`
  (traversal guard via `is_relative_to`). `remove_worktree(path)` runs `git -C <path> worktree remove
  --force <path>` — teardown forces removal because a worker leaves its worktree dirty, and its result
  is already captured by then. Git failures (non-zero exit with stderr detail, or a missing git
  binary) translate to `SpawnError`; only a `pathlib.Path` crosses the boundary (INV-8). Subprocess is
  a list argv (no shell injection). Logs are value-free.
- `ShellMorcliAdapter` (`src/pan/adapters/morcli.py`), implementing the new `MorcliAdapter` seam
  Protocol: the only module permitted to shell `morcli` (BR-2). `session_status(handle)` runs
  `morcli streams --json`, finds the stream whose `session_id` or `workspace_id` matches the handle
  (absorbing the open R-7 handle-binding ambiguity), and maps the agent status to a `WorkerStatus`
  (`working`/`idle` → RUNNING, `blocked` → BLOCKED, `done` → DONE, `unknown` → FAILED). An
  unrecognized status, an unknown handle, non-JSON/non-list output, a non-zero exit, or a missing
  `morcli` binary all raise `MorcliError`; only a `WorkerStatus` crosses the boundary (INV-8).
- New `MorcliError` (`src/pan/errors.py`): a `PanError` subclass for morcli subprocess failures. This
  extends the tech-spec's original nine-item taxonomy to ten — no existing error fit (reusing
  `HerdrError`, whose meaning is "herdr subprocess failed", would be a misnomer), and the morcli
  adapter genuinely introduces this failure mode.
- `auth_check` (`src/pan/gateway/auth.py`): the gateway's safety gate 2 — pure allowlist + channel
  policy enforced before any work. Returns `None` when the sender is in the `users` mapping and the
  channel is permitted (the `"*"` wildcard means all channels, or the channel is explicitly listed);
  raises `UnauthorizedSenderError` otherwise. Fail-closed: an unknown sender, an empty `users` map,
  and an explicitly empty `channels` list all deny. Pure logic — no Slack SDK import — and the
  denial is logged value-free (channel only, no sender payload).
- `BoltSlackAdapter` (`src/pan/gateway/app.py`), implementing the new `SlackAdapter` seam Protocol:
  the always-on Bolt Socket Mode gateway and the sole importer of `slack_sdk`/`slack_bolt` (BR-1) and
  the single Slack-client construction point (`.get_secret_value()` for the bot token in `__init__`
  and the app token in `start()` — BR-3). Its `handle_event` parses an untrusted Slack event dict into
  a typed `InboxItem` at the boundary, auth-checks the sender, adds a fast `:eyes:` reaction to the
  message ts BEFORE appending (INV-1 ack ordering), and appends to the `InboxStore` — it never spawns,
  classifies, or touches the thread map (stateless gateway, INV-1). A denied sender is dropped with no
  reaction and no append. A top-level app-mention roots its thread at the message ts
  (`is_thread_reply=False`); a reply carries the parent `thread_ts`. `add_reaction`/`post_message`
  translate `SlackApiError` to `SlackPostError`. `start()` (live Socket Mode, not unit-tested) wires
  the app-mention and message handlers, using the pure `_should_forward_message` filter to forward
  only human thread replies that don't mention the bot — avoiding the double-append when Slack
  delivers an in-thread mention as both an `app_mention` and a `message` event.
- `slack_post` (`src/pan/gateway/slack_post.py`): the single Slack egress path (INV-4). Every
  worker→thread post routes through this one function, which logs value-free (thread ts + text
  length, never the body) and delegates to `SlackAdapter.post_message`; an adapter failure surfaces
  as `SlackPostError`. Depends on the `SlackAdapter` Protocol, not a concrete — no Slack SDK import.
- `spawn_worker` + `ClaudeLauncher` (`src/pan/spawn.py`), adding the `AgentLauncher` seam Protocol:
  the worker-spawn orchestration. `spawn_worker` derives the stream label (`pan-<stream>`, or
  `pan-<short-id>` when no stream is given) and runs create_worktree → create_workspace → launch →
  `ThreadMap.put` (the SPAWNING record — the sole thread→worker binding, INV-7) → ack via `slack_post`
  ("on it — stream pan-…", INV-4). A `SpawnError`/`HerdrError` from any step records a FAILED
  `ThreadRecord`, posts a spawn-failed notice through the single egress path, and re-raises
  `SpawnError`. `ClaudeLauncher.launch` starts the worker by sending `claude <shell-quoted brief>`
  into the pane (herdr `send_text`) and submitting it with the fixed Enter nudge; the brief is
  `shlex.quote`d so arbitrary task text can never run as a shell command. The `AgentLauncher` seam is
  `launch(worktree, pane_id, brief) -> None` — a deliberate refinement of the plan's illustrative
  `launch(…, workspace, …) -> ThreadRecord`, since the launcher needs the pane id (not the workspace
  id) to send the brief and the orchestrator owns record construction (the record's required
  thread_ts/channel are not available to the launcher). Spawn logs are value-free (label + thread ts).
- `WatchdogInboxWatcher` (`src/pan/watcher.py`), implementing the final seam Protocol `InboxWatcher`:
  the filesystem watcher that wakes the orchestrator. `on_inbox_changed` issues exactly one fixed,
  content-free nudge to the orchestrator pane via `HerdrAdapter.nudge` — no payload ever crosses the
  pane (INV-2); the request travels through the durable inbox. `start()` (live observer thread, not
  unit-tested) wires a `watchdog` observer over the inbox dir whose callback invokes
  `on_inbox_changed`. Value-free logs.
- Completion hooks (`src/pan/hooks/stop.py`, `src/pan/hooks/notification.py`): the reply layer that
  runs when a worker finishes or needs input. Each parses the Claude Code hook JSON from stdin into a
  frozen pydantic boundary model, resolves the `ThreadRecord` (raising `ThreadNotFoundError` — and
  posting/transitioning nothing — when the thread is unknown), posts to the thread through the single
  `slack_post` egress (INV-4), and transitions the worker status. `stop_hook` posts the final summary
  (the last assistant text block extracted from the transcript JSONL, else a default) and marks
  `DONE`; `notification_hook` posts the Notification `message` (the question, else a default) and
  marks `BLOCKED`. The thread routing (`thread_ts`/`channel`) is injected — baked into the hook
  command at spawn — since the Claude Code payloads carry no thread context; `stdin` is an injectable
  seam for testing. Logs are value-free (thread + status). The transcript shape and the spawn-time
  correlation are flagged for live reconfirmation.
- Dormant gated-ops gate (`src/pan/hooks/pretooluse_gate.py`): safety gate 4, built but inert in v1.
  `pretooluse_gate` parses the Claude Code PreToolUse hook JSON into a frozen boundary model, matches
  the pending command against `PanConfig.gated_ops`, and — with `gated_ops` empty (the v1 default) —
  emits an allow decision and touches no Slack, so populating `gated_ops` later activates the gate
  with no code change. On a match it posts an approval request through the single `slack_post` egress
  and blocks on the decision (the inbox round-trip in production, an injected callable in tests):
  approve emits an allow decision; deny emits a deny decision to stdout (the sanctioned BR-5
  exception, via `stdout.write` + flush — never `print`) and raises `GatedOpDeniedError`. The gated-op
  log is value-free (the matched config pattern + the allow/deny bool, never the full command). The
  decision-JSON format and the required fail-closed behavior on approval-infra failure are flagged in
  TODOS.md for when the gate is activated.
- Typer CLI toolbelt (`src/pan/cli.py`, `src/pan/__main__.py`): the `pan` console entry with sub-apps
  `config`/`inbox`/`threads` and commands `gateway`, `config set-token`, `config show`, `inbox drain
  --json`, `spawn`, `threads get`, `threads set`, `slack-post`, `status`, `stop`, `pause`. Each command
  is a thin boundary that loads `PanConfig`, constructs the adapters it needs, and calls the pure core.
  `_run()` is the single error boundary: it maps each `PanError` subclass to a documented exit code
  (10–19, the base → 1) via `_exit_code_for`, translates the vendored click exceptions, and lets
  unclassified exceptions surface as tracebacks (Principle 4); `main()` = `SystemExit(_run())`. `config
  show` masks credentials (SecretStr serialization — never `.get_secret_value()`, R-4). Deliberate
  refinements over the plan's sketch: `spawn`/`slack-post` take `--channel` (channel isn't on
  `ThreadRecord`), `spawn` takes `--repo` (default cwd), and `status`/`stop` key on `--thread`
  (the thread map is keyed by `thread_ts`) with `status` resolving the morcli handle from the record.
- Orchestrating skill (`src/pan/skills/orchestrating/SKILL.md`): the prose the persistent
  `pan-orchestrator` session runs on each inbox nudge — a drain → read-directive → look-up-binding →
  route loop. It shells `pan inbox drain --json` (which now attaches the deterministically-parsed
  `directive` block per item, so mode is fixed in code per INV-3 — see below), then routes on a
  precedence ladder: STATUS first (answer from `pan status`, or a "no active worker" note; never relay
  an empty status into a pane), then a live-worker relay, then a fresh spawn for a finished/absent
  thread or `force_new`. The skill states explicitly that the model makes only the fuzzy calls
  (reuse-vs-spawn, repo, task comprehension) and every mechanical step is a `pan` subcommand.
- `pan inbox drain --json` now runs `parse_directive` in code and emits `{"item", "directive"}` per
  entry, so the orchestrator reads the mode/`cleaned_text` deterministically rather than re-deriving
  them by judgment (enforces INV-3; the flag table stays the single source of truth in `directive.py`).
