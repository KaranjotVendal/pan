# 0003: Lint rules that catch common agent mistakes

Status: accepted

## Context

Most of pan's code is written by coding agents. The mistakes they make again and again are
not the ones ruff's default set looks for: reaching into another module's private names (or a
test poking a private helper instead of the behaviour), boolean flags passed positionally
(`launch(name, True)`), functions that grow one branch at a time until nobody can follow them,
catch-all `except Exception` blocks, naive datetimes, `os.path` next to `pathlib`, stray
`print` calls and commented-out code. Review catches some of them; a lint rule catches all of
them, on every run.

References:

- The "quality" rules of `@nkzw/oxlint` 3.0: a curated set that targets exactly this kind of
  drift in agent-written TypeScript.
- `oxlint-plugin-go-internal`: Go's `internal/` folders for TypeScript, so a module is private
  to its folder unless it is made public on purpose.

Python already has that convention: a leading underscore marks a name as internal to its module.
Until now nothing checked it. pan has no `internal/` folders yet, and does not need them: its
modules are small and flat, and the layer rules of decisions/0001-layers.md already fix which
package may import which. The underscore is the boundary inside a layer.

## Decision

The root `pyproject.toml` turns these on, and every layer's `ruff.toml` extends it, so they
apply everywhere:

- Private by default, checked in two places:
  - `PLC2701` (importing a private name from another package) and `SLF001` (accessing a
    private member). `PLC2701` does not flag an import inside the same top-level package, so
    it covers imports from outside `pan`: the tests, and third-party privates in `src`.
  - Inside `pan`, the architecture test `tests/unit/test_private_names.py` covers it: it
    reads every module under `src/pan` with `ast` and fails, with `file:line`, on any
    `from pan.<module> import _name` (absolute or relative), `import pan.<..>._private`, or
    `module._name` on an imported pan module, made from a module other than the one that
    defines the name.

  A test goes through public behaviour; when another module genuinely needs a name, the name
  loses its underscore rather than the check being silenced. `PLC2701` is a preview rule, so
  `preview = true` is set with `explicit-preview-rules = true`: only preview rules named in
  `select` turn on. Preview mode also changes how some stable rules behave, so `ruff` is
  pinned to its current minor version in the dev dependencies (`>=0.15.21,<0.16`): a new
  minor, with whatever preview changes it brings, arrives by a deliberate bump.
- Boolean traps: `FBT001`, `FBT002`, `FBT003`. A boolean parameter is keyword-only. Two
  exceptions: in `frontend/cli.py`, Typer declares each CLI flag as a boolean parameter with a
  `typer.Option` default and Typer, not a caller, passes it, so each such parameter carries
  `# noqa: FBT001` on its line (`typer.Option` is also an allowed call for `FBT003`); and
  `tests/**` ignores `FBT001` in `per-file-ignores`, because pytest passes parametrized values
  to a test by name.
- Complexity: `C901` with `max-complexity = 10`. A function over it is split into named
  helpers; a flat dispatch (one `case` per type of a union, kept exhaustive by `assert_never`)
  may carry a `# noqa: C901` with the reason.
- Catch-alls: `BLE001`. An `except Exception` is kept only at a boundary that must never fail,
  with `# noqa: BLE001` and the reason on the line.
- `RUF` (ruff's own rules, including `RUF027`, a string that looks like a missing f-string).
  `RUF001` allows the en dash and curly single quotes, which pan matches on purpose because
  phone keyboards autocorrect `--` and quotes into them.
- `DTZ` (timezone-aware datetimes), `PTH` (pathlib over `os.path`), `PERF`, `FURB`, and from
  tryceratops only `TRY300`, `TRY301` and `TRY400`.
- Already clean, turned on so they stay clean: `ERA` (commented-out code), `RET`, `PIE`, `C4`,
  `T20` (`print`) and `SIM`.

Left off, and why:

- `TRY003` (long messages in exception constructors): pan's errors carry their message where
  they are raised, and the message is what a person reads.
- `S` (bandit): mostly flags `subprocess` calls, which pan makes on purpose with fixed argument
  lists; the noise would bury real findings.
- `PLR0913` and `PLR0917` (too many arguments): the backend takes its collaborators as
  keyword arguments on purpose, so every seam is visible at the call site.
- `PT` (pytest style): style preferences, not mistakes.
- `ARG` (unused arguments): fakes and protocol implementations must accept arguments they
  ignore.
- The broad `PL` family: most of it is style or duplicates rules already on; the useful rule
  (`PLC2701`) is selected by name.
- `G` (logging format): pan logs with f-strings on purpose.

## Consequences

- `ruff check` fails on a private import, a positional boolean, a function over the complexity
  limit, an unexplained catch-all, a naive datetime and the rest above, in every layer, and
  `pytest` fails on a pan module using another pan module's private name.
- Every suppression is a `# noqa` with its reason on the line, so `grep -rn "noqa" src` lists
  them all. At the time of writing: the private import of Typer's `ClickException` (Typer
  exports no public name for it), the Typer flag parameters in `frontend/cli.py`, the two
  deliberate catch-alls, and the flat dispatch in `slack_render.line_text`.
- `frontend/help` used `backend.directive`'s private `_LEADING_MENTION` and
  `_normalize_punctuation`; they are public now (`LEADING_MENTION`, `normalize_punctuation`).
- Tests check behaviour through public names. A helper that only a test needed either became
  public (`BoltSlackAdapter.should_forward_message`, the gateway's forwarding rule) or its tests
  moved to the public path (exit codes through `cli.main`, the completion hooks through
  `pan hook stop` and `pan hook notification`, the inbox event handler through
  `WatchdogInboxWatcher.start`).
- Adding or removing a rule family is a change to this record or a new one.
