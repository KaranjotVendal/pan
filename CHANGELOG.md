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
