# 0001: Five layers with import direction enforced by ruff

Status: accepted

## Context

pan is adding a second message source next to Slack. Until now `src/pan` was one flat package
where any module could import any other, so Slack-specific code leaked into the modules that
spawn, steer and report on sessions. Adding a second source on top of that would spread the
coupling further.

The architecture principles this follows:

- One package, layered by responsibility.
- Imports point down: a layer may use the layers below it, never the ones above.
- Boundaries are lint errors, not conventions.
- `utils` is technical plumbing (logging, config, credentials, clock) that knows nothing about
  the domain beyond the shared vocabulary in `core`.
- `core` is the domain vocabulary (models, errors, seam protocols) and does no I/O.
- Messages between layers are fixed, serialisable types; how they travel is left open.
- A deviation from these rules is recorded as a new decision record in the pull request that
  makes it.

## Decision

`src/pan` is split into five packages. Imports may only point down this list:

    frontend -> backend -> data -> utils -> core

A layer may import any layer below it. `core` imports nothing from pan except `core`.

| Layer | Modules |
| --- | --- |
| `core` | `models`, `errors`, `seams` |
| `utils` | `logging`, `config`, `credentials`, `clock` |
| `data` | `adapters/` (`git_worktree`, `hc`, `launchd`, `plannotator_inbox`, `terminal_host`), `inbox`, `threadmap`, `drained`, `transcript` |
| `backend` | `spawn`, `session_control`, `sessions`, `lifecycle`, `interface`, `confirmations`, `directive`, `status_message`, `service`, `orchestrating_skill`, `gates`, `drain` |
| `frontend` | `cli`, `wiring`, `gateway/` (`app`, `auth`, `slack_post`), `help`, `hooks/`, `watcher`, `slack_format`, `slack_render` |

`src/pan/__main__.py` stays at the top as a thin entry that calls `pan.frontend.cli`. The
packaged data (`skills/orchestrating/SKILL.md` and the `launchd/` templates) stays at the top
of the package.

The rule is enforced by ruff's banned-api check (`TID251`). Each layer package has its own
`ruff.toml` that extends the root `pyproject.toml` and bans the layers above it, so
`ruff check` fails on an upward import. The rules apply when ruff discovers its config
normally, from the repo root; a run pointed at another config with `--config` does not see
them. The Slack libraries (`slack_bolt`, `slack_sdk`) are banned everywhere except
`frontend/gateway` and `tests/unit/test_gateway_app.py`, which needs a Slack error type.

### Temporary allow-list

Four backend modules import Slack posting and rendering from `frontend` today. Each listed
import carries `# noqa: TID251`, so the exemption covers only that import and the rest of the
file, including the Slack-library ban, is still checked. Step 4 (typed events) removes them by
replacing the direct calls with serialisable events that the frontend delivers:

- `backend/confirmations.py`: imports `slack_post` from `frontend.gateway.slack_post`.
- `backend/session_control.py`: imports `slack_post` and `slack_post_parts` from
  `frontend.gateway.slack_post`, and `check_state` and `status_label` from
  `frontend.slack_render`.
- `backend/spawn.py`: imports `slack_post` from `frontend.gateway.slack_post`, and
  `status_label` from `frontend.slack_render`.
- `backend/status_message.py`: imports `slack_post_parts` from `frontend.gateway.slack_post`,
  and `private_details_text`, `status_blocks` and `status_text` from `frontend.slack_render`.

The list only shrinks. A new entry needs a new decision record.

Update 2026-11-25: the list is empty. Step 4 (typed events) replaced the seven imports with
events the backend reports through a `Reporter` (decisions/0002-typed-events.md), so the
boundary now holds without exceptions: `grep -rn "noqa: TID251" src` finds nothing. The step adds
`events` to `core`, `text` to `utils` and `slack_reporter` to `frontend`, and moves
`thread_permalinks` from `backend/sessions` to `frontend/slack_reporter`, `check_state` from
`frontend/slack_render` to `backend/status_message`, and `status_label` from
`frontend/slack_render` to `utils/text`.

## Options considered

- Keep the flat package as it is. No cost now, but nothing stops new upward imports, and the
  second source would be wired through the same Slack-shaped modules.
- A uv workspace with one package per layer. Package boundaries enforce the direction for free,
  but each layer needs its own `pyproject.toml`, version and build. That overhead only pays off
  when layers ship separately, and pan ships as one tool. Rejected.
- One package with layer subpackages and a ruff rule. The direction is checked on every
  `ruff check` with no change to how pan is built or installed. Chosen.

## Consequences

- Module paths change (for example `pan.cli` is now `pan.frontend.cli`), and so do logger
  names, which follow module paths. Behaviour is unchanged.
- An upward import fails `ruff check`, with a message that points here.
- The allow-list above is technical debt with a named owner (step 4); `grep -rn "noqa: TID251" src`
  lists it.
- Where a module goes is a decision in its own right: moving one across layers is a change to
  this record or a new one.
