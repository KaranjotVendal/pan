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

- **Completion-hook cross-thread blast radius (Task 21, Security persona).** The `pan hook
  stop`/`notification` dispatch trusts the worker-process-supplied `cwd` to select which thread to
  post to and mark DONE/BLOCKED. `get_by_worktree` matches on the RESOLVED `worktree_path` (Task 24
  hardened it from exact string match to `Path.resolve()` on both sides — symmetric, preserves the
  injective real-worktree→record mapping), and `cwd` comes from Claude Code's runtime (not agent
  text), so within the trust boundary it's bounded to already-registered worktrees. Hardening idea: inject a tamper-resistant worker→thread token at
  spawn (env var / settings value) that the hook echoes back and cross-checks against the resolved
  record's `thread_ts`, so a spoofed/confused `cwd` can't drive another thread's lifecycle.
- **Completion-hook reads the thread-map file 3x per invocation (Task 21, Performance persona).**
  `_dispatch_completion_hook` calls `get_by_worktree` (read 1), then the existing `stop_hook`/
  `notification_hook` re-check `thread_map.get(thread_ts)` (read 2) before `update_status` (read 3).
  Negligible for a one-shot hook against a tiny single-user JSON, but the redundant `get` could be
  removed by threading the already-resolved record through to the hook function.
- **Type the completion-hook dispatcher precisely (Task 21, Architect/Code personas).**
  `_dispatch_completion_hook`'s `hook_fn: Callable[..., None]` erases the shared
  `(thread_ts, channel, ThreadMap, SlackAdapter, *, stdin: TextIO) -> None` signature (a plain
  `Callable` can't express the keyword-only `stdin`). A small `CompletionHook` Protocol would type
  the seam. Non-blocking.
- **Thin coverage on the completion-hook composition root (Task 21, Test persona).**
  `cli._run_completion_hook` (reads `sys.stdin`, loads config, builds `FileThreadMap` +
  `BoltSlackAdapter`) is unexercised, and `_extract_cwd`'s valid-JSON-dict-without-`cwd` / non-dict /
  non-str-`cwd` branches aren't directly asserted (they all reach the same clean-exit path). Add
  cases when convenient.
- **Document directive mode precedence in the tech spec.** `parse_directive` (Task 3,
  `src/pan/directive.py`) resolves STATUS > SYNC > DELEGATE when multiple mode flags are present
  (e.g. `--sync --status` → STATUS). This is deliberate (STATUS is the read-only, no-worker path, so
  it is the safe winner) and is covered by a test, but the tech spec's semantics description
  (`docs/.../pan-tech-spec.md`, "Types, Interfaces, and APIs") does not state the precedence. Add a
  one-line note there so the ordering is contract, not incidental. Non-blocking (Domain persona,
  Task 3 review).
- **`load_config` only translates `FileNotFoundError`.** `src/pan/config.py` wraps a missing file,
  malformed/non-object JSON, and validation errors as `ConfigMissingError`, but other OS-level
  failures on the config path (`PermissionError`, `IsADirectoryError`) would propagate as a raw
  `OSError` past the CLI boundary. Consider widening the caught exception if these become realistic.
  Non-blocking (Domain persona, Task 4 review).
- **Taxonomy comment vs behavior for malformed config JSON.** `ConfigMissingError`'s comment in
  `src/pan/errors.py` says "no config file / required field absent" but `load_config` also uses it
  for malformed / non-object JSON. Either broaden the comment to name "malformed" or add a
  `ConfigError` class and reclassify. No code change needed now. Non-blocking (Domain persona, Task 4).
- **Tests emit records into the real `~/.pan/logs/pan.log`.** Because `initialise_logger` sets
  `propagate=False` and attaches its own file/stderr handlers, unit runs write real log lines as a
  side effect (values are non-secret). Harmless, but if fully hermetic test runs are wanted later,
  point the log dir at a tmp path in a conftest fixture. Non-blocking (Code persona, Task 5).
- **Credentials save follows symlinks on the destination component only via os.replace.** The
  atomic temp-file write uses `O_EXCL` (safe), and `os.replace` swaps by rename so the destination
  is never opened for write. If hardening `~/.pan/` against a hostile local co-user later, consider
  `O_NOFOLLOW` and verifying the parent dir is owner-only. Low priority (Security persona, Task 5).
- **`~/.pan` is created at umask-default perms, not 0700.** `save_credentials` (and `logging.py`'s
  log dir) create `~/.pan` via `mkdir(parents=True)` with no explicit mode, so it lands ~0755. File
  contents stay protected at 0600, but a world-listable secrets dir leaks metadata. Consider
  creating/enforcing `~/.pan` at 0700. Non-blocking (Security persona, Task 5).
- **No `fsync` before `os.replace` in `save_credentials`.** A crash mid-save could leave the
  destination absent/partial after a reported success. Durability only, not a confidentiality issue.
  Non-blocking (Security persona, Task 5).
- **Inbox recovery sweep for stale `.claimed` / `.corrupt` files.** `FileInboxStore.drain`
  (`src/pan/inbox.py`) claims entries by renaming to `.claimed` and quarantines poison entries to
  `.corrupt`. A crash mid-drain (after claim, before unlink/restore) leaves orphaned `.claimed`
  files that future `*.json` globs never see (silent event loss), and `.corrupt` files accumulate
  with no cleanup/alert path. Add a startup sweep that re-claims stale `.claimed` back to `.json` and
  a runbook/metric for `.corrupt`. Ties into R-6 live verification. Non-blocking (Architect/Performance/
  Domain, Task 6).
- **Inbox drain is at-most-once (delete-before-ack).** `drain` unlinks the claim files before
  returning, so a crash after unlink but before the orchestrator commits the batch loses it. If an
  at-least-once contract is wanted, delete only after the caller acknowledges. Pre-existing design
  property; confirm against the intended durability contract during R-6 live work. Non-blocking
  (Performance persona, Task 6).
- **Consider hoisting the safe-event-id guard into `InboxItem`.** `FileInboxStore.append` validates
  `item.id` is a separator-free single path segment before using it as a filename. A pydantic field
  validator on `InboxItem.id` would enforce this at construction for every future consumer (defense
  in depth) rather than per-store. Weigh against over-constraining a legitimate Slack id. Non-blocking
  (Security persona, Task 6).
- **`FileThreadMap` read-modify-write is not concurrency-safe.** `put`/`update_status`
  (`src/pan/threadmap.py`) do a full read-all + write-all with a fixed `.tmp` temp name. Safe for the
  single-session orchestrator, but a second concurrent writer would risk a lost update and a temp-name
  collision. Revisit with per-record files or a lock if the orchestrator ever becomes multi-session.
  Also no `fsync` before `replace` (durability only). Non-blocking (Performance persona, Task 7).
- **Consider writing `threads.json` at 0600.** It holds no secrets (thread/workspace/pane ids,
  worktree paths, statuses), so this is not required, but if worktree paths/workspace ids are ever
  treated as sensitive on a shared host, mirror the credentials.json 0600 posture. Non-blocking
  (Security persona, Task 7).

- **Wire `should_caffeinate` into the always-on power actuation (Task 23, Architect persona).**
  `src/pan/power.py`'s `should_caffeinate` is the pure R-3 policy predicate (True only on AC with
  >=1 active worker) but has no caller yet — nothing invokes `caffeinate` / `pmset disablesleep 1`
  off it. The engagement layer that reads AC state + the live worker count and starts/stops
  `caffeinate` is the deferred, live-verified part of the original Task 20. Until it lands,
  lid-closed execution does not hold the machine awake (the README now says so). Non-blocking.
- **README first-run posture is maximum blast radius (Task 23, Security persona).** The example
  config ships `permission_mode: "bypass"` with `autonomy: "full"`, `channels: ["*"]`, `repos:
  ["*"]` as the starting point. This is the intended full-auto-in-isolated-worktree design, but a
  one-line nudge steering first-time users to scope `repos`/`channels` before enabling `bypass`
  would tighten security ergonomics. Non-blocking.

- **`get_by_worktree` resolves one `Path.resolve()` syscall per record (Task 24, Performance/
  Architect/Domain personas).** The lookup now canonicalizes both the target and every stored
  `worktree_path` in the scan loop; `resolve()` does a readlink/stat per record. Negligible at v1
  scale (single-user map, one active thread per worker, one-shot hook process, first-match
  short-circuit). If the map ever grows large or the lookup becomes hot-path, store the resolved
  path on `ThreadRecord` at `put` time so lookups compare pre-canonicalized paths. Non-blocking.
- **Add a mismatched-symlink negative test for `get_by_worktree` (Task 24, Test persona).** The new
  test proves a symlinked prefix matches; a companion case asserting that a symlink resolving to a
  DIFFERENT real dir does NOT match would harden the resolve() behavior against false positives.
  Non-blocking.
- **`_extract_cwd` accepts an empty-string `cwd` (Task 24, Security persona).** `cli._extract_cwd`
  passes `cwd == ""` (it satisfies `isinstance(cwd, str)`), which becomes `Path(".")` and now
  `.resolve()`s to the hook process's cwd rather than matching nothing. Harmless under the trust
  model (the hook runs inside the worker's worktree, so it resolves to that same legitimate thread),
  but treating `""`/relative `cwd` as "no cwd" (return `None`) would make the no-match path explicit.
  Non-blocking.

## Deferred to final human session (live)

These cannot run overnight — they need a real Slack connection, the phone, or a live-verify write
path — and are intentionally out of the Night Shift agent's reach. Hand them to the human for the
final session.

- **Task 20 live smoke (real Slack + phone).** From the phone: `@pan create /tmp/pan-hello.txt
  containing today's date, then tell me the contents`. Verify the `:eyes:` ack within ~1s, a worker
  spawning in its own fresh git worktree + herdr workspace, the file created and its contents posted
  back in-thread, a follow-up reply routing to the same worker, and `@pan --status` reporting the
  worker's live state. Gateway / spawn / hooks are not considered done until this passes.
- **Task 21 live smoke — worker auto-reply via completion hooks (DEFERRED, human session).**
  Trigger `@pan <task>` and confirm the worker posts its result back to the thread on completion
  (Stop hook) and posts a question when blocked (Notification hook). Mocks cannot catch the real
  Claude Code hook JSON shape or hook invocation. WATCH FOR: `get_by_worktree` exact-matches the hook
  JSON's `cwd` against the stored `worktree_path`; on macOS these can differ by symlink resolution
  (`/tmp` vs `/private/tmp`) — if the hook silently no-ops (no post, "no thread record for cwd" in the
  log), normalize both sides with `Path.resolve()` in `FileThreadMap.get_by_worktree` and/or the CLI
  `_extract_cwd`. Also confirm the worker's `cwd` at Stop is the worktree root (not a subdir).
- **R-2 — herdr nudge keystroke + create/pane-list output shapes (verify-live-api).**
  `ShellHerdrAdapter` (`src/pan/adapters/herdr.py`) was built against the real `herdr` CLI this
  session, but two things need a live orchestrator to confirm: (1) that `herdr pane send-keys <pane>
  Enter` actually WAKES an idle orchestrator Claude Code session — per the herdr overlay note, a bare
  Enter can be a no-op against an autosuggest overlay or an empty prompt; if it doesn't reliably
  register, revisit the nudge keystroke while keeping it content-free (INV-2); (2) the
  `workspace create` / `pane list` JSON key names (`workspace.workspace_id`, `workspace.active_tab_id`,
  `panes[].pane_id`/`tab_id`) are version-dependent — reconfirm against the running herdr. Ties into
  the Task 20 live smoke.
- **Git worktree adapter — live-verify semantics (verify-live-api).**
  `ShellGitWorktreeAdapter` (`src/pan/adapters/git_worktree.py`) is unit-tested with a mocked
  subprocess; several git behaviors need a real repo to confirm: (1) `git -C <worktree> worktree
  remove --force <worktree>` run from *inside* the worktree being removed resolves the main repo via
  the `.git` gitlink — verify it isn't fragile vs running from the main repo; (2) a *locked* worktree
  needs `--force --force`; (3) `create_worktree` fails if the branch already exists or a stale
  worktree registration lingers — decide whether a `git worktree prune`/existing-branch handling is
  needed; (4) confirm branching from repo HEAD (no start-ref) is the intended base, else add a
  start_ref to the Protocol; (5) teardown does not delete the `-b`-created branch, so branches
  accumulate — decide whether to `git branch -D`. Non-blocking (Domain/Code personas, Task 9); ties
  into the Task 20 live smoke.
- **Git worktree branch argument-injection hardening.** When branch naming loosens beyond the v1
  flat `pan-<slug>` scheme, add `--` before positionals in the git argv and/or reject branches
  starting with `-`, so a future name can never be parsed as a git flag. Not exploitable today
  (positional path is absolute; `-b <branch>` consumes the value). Non-blocking (Security persona,
  Task 9).
- **`MorcliError` extends the tech-spec taxonomy from 9 to 10.** Task 10 added `MorcliError` to
  `src/pan/errors.py` (no existing error fit a morcli subprocess failure; `HerdrError` is herdr-
  specific). Update the tech-spec taxonomy block (`docs/.../pan-tech-spec.md`, the exception-taxonomy
  code block still lists 9) to record it. Also: the CLI `_run` taxonomy→exit-code table (Task 18)
  MUST include `MorcliError` with a documented exit code. Non-blocking now (Architect, Task 10).
- **`unknown -> WorkerStatus.FAILED` in the morcli adapter is lossy.** Verified against morcli source
  (`sessions.py`: `status = agent_status or "unknown"`), morcli emits `"unknown"` when herdr reports
  no agent status — i.e. "not reported", not "crashed". Acceptable while STATUS mode is read-only
  (FAILED drives no action), but if FAILED ever triggers remediation, add a distinct signal (e.g. a
  `WorkerStatus.UNKNOWN` member) so a genuinely-alive-but-unreported session isn't treated as failed.
  Also (R-7): `session_status` matches on `session_id` OR `workspace_id` and returns the first match;
  confirm handle uniqueness live and prefer `session_id` on collision. Non-blocking (Domain, Task 10).
- **Gateway `start()` live hardening (verify-live-api).** `BoltSlackAdapter.start()`
  (`src/pan/gateway/app.py`) is live-only (`pragma: no cover`); the pure `_should_forward_message`
  filter is unit-tested, but several live behaviors need real Slack: (1) if `auth_test()` returns no
  `user_id`, `bot_user_id` is None and the in-thread-mention dedup degrades — consider failing fast
  in `start()` rather than silently re-enabling double-append; (2) human thread replies carrying a
  `subtype` (`thread_broadcast`, `file_share`) are currently dropped by the blanket `subtype is None`
  check — decide whether to forward them; (3) mention detection uses the substring `<@{bot_user_id}>`
  and won't match the legacy `<@U123|label>` form — harden with a regex if that form appears; (4) the
  real `WebClient(token=...)` construction branch (the BR-3 egress point) and the Bolt event
  registration are exercised only live. Ties into the Task 20 live smoke.
- **Gateway drop is double-logged; ack-before-append is fail-closed.** (1) A denied sender is logged
  by both `auth_check` and `BoltSlackAdapter.handle_event` — consolidate to one line (the handler's,
  which carries the event id). (2) `handle_event` adds `:eyes:` before appending, so if
  `add_reaction` raises `SlackPostError` the event is never appended (silently lost) — this upholds
  the INV-1 ack-first ordering but consider whether a transient reaction failure should still ingest.
  Non-blocking (Architect/Code personas, Task 12).
- **Worker launch command needs `--permission-mode` wired + live-verify (verify-live-api).**
  `ClaudeLauncher.launch` (`src/pan/spawn.py`) sends `claude <shell-quoted brief>` into the pane, but
  `Defaults.permission_mode` (default "bypass") is NOT wired into the command. A full-auto worker will
  stall at its first tool-permission prompt. Wire `permission_mode` into `ClaudeLauncher` and confirm
  the exact `claude` invocation (flags, permission mode, interactive vs `-p`) against a real session
  in the Task 20 smoke. Non-blocking (Domain persona, Task 14).
- **CLI spawn boundary must not double-surface a `SpawnError` from `spawn_worker`.** `spawn_worker`
  already posts "spawn failed…" via `slack_post` AND records a FAILED `ThreadRecord`, then re-raises
  `SpawnError`. The tech-spec Failure Flow shows the CLI boundary also posting + `update_status(FAILED)`.
  When Task 18 wires `pan spawn`, its catch block must treat a `SpawnError` from `spawn_worker` as
  already-surfaced-and-recorded and only map the exit code — otherwise the thread gets a double post
  and a redundant status write. Non-blocking (Architect/Domain/Code, Task 14).
- **Orphaned worktree + understated FAILED record on partial spawn failure.** When `create_worktree`
  succeeds but `create_workspace`/`launch` fails, `spawn_worker` does not remove the created worktree
  (the `remove_worktree` seam exists), and the FAILED `ThreadRecord` always stores `workspace_id=""`
  and `worktree_path=base/label` even though a worktree (and maybe workspace) were provisioned.
  Consider cleanup + recording the real provisioned state. Non-blocking (Architect/Domain/Code, Task 14).
- **Defense-in-depth: sanitize `stream` to an allowlist charset.** The worker label `pan-<stream>`
  derives from user directive text; today worktree-path safety rests on the git adapter's
  `is_relative_to` guard + git ref validation. Validating `stream` to `[A-Za-z0-9._-]` at the
  directive/spawn boundary would protect future/alternate adapters. Non-blocking (Security, Task 14).
- **`AgentLauncher.launch` ignores its `worktree` param.** `ClaudeLauncher` doesn't use `worktree`
  (the pane already cwds there); it's retained for the seam contract (sibling codex/pi launchers may
  need it). Confirm that's the intent or drop the param. Non-blocking (Architect, Task 14).
- **Watcher nudge storm / debounce.** `WatchdogInboxWatcher._InboxEventHandler.on_any_event`
  (`src/pan/watcher.py`) nudges on EVERY filesystem event in the inbox dir. One logical inbox write
  (temp-file + rename) plus the drain's `.claimed`/`.corrupt` renames each raise several fs events,
  so one logical change fans out to multiple nudges. Harmless (the nudge is content-free and the
  orchestrator drains all pending items on any nudge), but consider filtering to the settle/final
  event or debouncing so one logical change yields one nudge. Non-blocking (Performance persona, Task 15).
- **Hook payload shapes + thread correlation need live-verify (verify-live-api).**
  `stop_hook`/`notification_hook` (`src/pan/hooks/`) parse the Claude Code hook JSON at the boundary,
  but: (1) `stop_hook._last_assistant_text` assumes the transcript JSONL shape (`type=="assistant"`,
  `message.content[].type=="text"`) — confirm against a real Claude Code transcript before trusting
  the extracted summary (the default "Worker finished." is the only safety net if the format drifts);
  (2) the Stop/Notification payloads carry NO thread context, so `thread_ts`/`channel` are injected
  by baking them into the hook command at spawn — confirm this correlation fires correctly when a
  real worker Stop/Notification triggers. Both tie into the Task 20 live smoke. Also (Security, low
  risk): `_last_assistant_text` reads the transcript unbounded via `read_text()`; add a size cap if
  the `transcript_path` trust model ever weakens. Non-blocking (Domain/Test/Security, Task 16).
- **Gated-ops gate must fail CLOSED before `gated_ops` is ever populated.** `pretooluse_gate`
  (`src/pan/hooks/pretooluse_gate.py`) is dormant in v1 (`gated_ops == []` → allow), but on the match
  path, if `slack_post` (Slack down) or `decide` (inbox round-trip fails/times out) raises, the hook
  crashes with NO decision emitted to stdout → Claude Code treats it as non-blocking → the gated
  (dangerous) op RUNS. This gate is the sole brake on a bypass-permissions full-auto worker. Before
  populating `gated_ops`, wrap the match path so any failure to obtain approval emits a `deny`
  decision (fail-closed). Also: substring matching (`op in command`) is evadable (`rm  -rf`, `rm -fr`,
  `/bin/rm --recursive`) — use tokenized/normalized matching when activated; and gate only on
  `tool_name == "Bash"` so a future non-Bash tool with a `command`-named input can't match
  unintentionally. High priority when activating (Security/Domain, Task 17).
- **Live-verify the PreToolUse decision JSON format (verify-live-api).** `pretooluse_gate` emits
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"|"deny", ...}}`
  (the current Claude Code structured-output shape). Confirm against a running hook that Claude Code
  actually blocks on the emitted `deny` (and doesn't also require exit code 2). Non-blocking (Domain,
  Task 17).
- **Gateway must consult `pan pause`'s flag.** `pan pause` (`src/pan/cli.py`) toggles
  `<threads-dir>/paused.flag`, but `BoltSlackAdapter` never reads it, so the documented "new tasks
  acked 'paused' and dropped, running work untouched" behavior is inert. Wire the gateway ingest to
  check the flag; also consider promoting the flag path to a `PanPaths` field so writer and reader
  agree in one place instead of both re-deriving the magic path. Non-blocking (Architect/Domain/Code,
  Task 18).
- **`ThreadRecord` has no `channel`, so posts require `--channel` supplied by the caller.** `pan
  spawn`/`slack-post` and the completion hooks all take `--channel` because the thread map doesn't
  store it. Workable for the live single-run loop (the orchestrator holds the channel from the drained
  InboxItem), but a late/hook-driven post or a post-restart recovery carries only `thread_ts` and
  can't recover the channel from the binding alone. Consider adding `channel` to `ThreadRecord` so
  every post can resolve it from the sole binding (INV-7). Non-blocking (Domain, Task 18).
- **CLI `status`/`stop` are keyed on `--thread`, deviating from the spec's `--stream`.** This is more
  correct (the thread map and `pane_ids` are keyed by `thread_ts`; there is no stream-label index),
  but reconcile the tech-spec/design text, and note `status` resolves the morcli handle as
  `record.morcli_session or record.workspace_id` — live-verify that morcli's `streams --json`
  `workspace_id` matches herdr's workspace id during the spawn→first-report window (R-7). Non-blocking
  (Domain, Task 18).
- **Pin an upper bound on `typer`.** `cli._run` imports the vendored `typer._click.exceptions`
  (typer 0.27 has no standalone click); `pyproject.toml` pins `typer>=0.27.0` with no ceiling, so a
  future minor could relocate that private module and break the CLI error boundary. Bound it (e.g.
  `<0.28`). Non-blocking (Architect, Task 18).
- **`pan config set-token` accepts tokens as flags (shell-history exposure).** The `--bot-token`/
  `--app-token` options fall back to a hidden prompt, but a token could be passed on the command line
  and land in shell history / process listing. Consider prompt-only to remove the flag surface.
  Non-blocking (Security, Task 18).
- **Thin CLI-command test coverage.** `stop` (unknown-thread → ThreadNotFoundError, the
  highest-value gap), `config set-token` (0600 write), `slack-post` (single egress), `pause` toggle,
  and `threads set` have no CliRunner cases yet, and the `_run` PanError→exit mapping is proven by
  replacing `app` rather than driving a real command through Typer under `standalone_mode=False`. Add
  those cases. Non-blocking (Test, Task 18).
- **A `pan relay` command would centralize the worker-pane relay.** The orchestrating skill relays a
  follow-up into a live worker's pane with `herdr pane send-text` directly (there is no `pan` command
  for it). A `pan relay --thread <ts> --text <t>` wrapper (resolving pane_ids from the thread map)
  would keep herdr-shelling out of the orchestrator's hands and let the relay be audit-logged (INV-9),
  mirroring the single Slack egress. Non-blocking (Architect, Task 19).
- **`pan inbox drain` plain (non-JSON) branch mode suffix untested.** The human-readable drain line now
  appends `directive.mode.value`; only the `--json` path is asserted. Add a case. Non-blocking (Code/
  Test, Task 19).
- **R-5 — Slack Socket Mode retry / event-id stability (verify-live-api).** Confirm the Slack event
  id is stable across redelivery-after-wake so the inbox dedupe (INV-6) actually absorbs duplicates.
  Mocks cannot reproduce real Socket Mode retry payloads.
- **R-7 — morcli handle binding (verify-live-api).** Confirm whether `ThreadRecord.morcli_session`
  is known at launch or must be resolved on first status query, against a real morcli session.
- **R-6 — inbox append/drain race under live burst.** The atomic-drain design is unit-tested with
  fakes; confirm it is race-free under a real burst of concurrent mentions plus the watcher and
  `pan inbox drain` all touching the directory.
