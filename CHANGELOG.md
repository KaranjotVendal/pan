# Changelog

All notable changes to `pan` are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to follow
Semantic Versioning. The Night Shift build agent appends one entry per task under `## [Unreleased]`
as it works (see `AGENT_LOOP.md` step 9).

## [Unreleased]

### Added

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
