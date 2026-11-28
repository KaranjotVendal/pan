# 0004: Hand project tasks to the project's orchestrator, and read the project map through a seam

Status: accepted

## Context

pan's project map (`backend/project_map.py`) groups live agent sessions into projects: an
orchestrator session and the streams it started (hc parent links), plus standalone sessions.
Until now pan only used it to describe sessions (`pan sessions`, the private Slack list). People
now aim Slack tasks at a project ("acme-shop: add the export") rather than at one session, and
the project's own orchestrator, not pan, knows which stream should take the work.

At the same time herdrmap (a separate CLI, `herdrmap map --json`) is being built to own that map
for every tool, pan included. It is not finished, so pan must not call it yet, but pan should be
able to switch to it without touching routing or rendering.

## Decision

### Project routing ("option A")

- `pan send --project <selector>` hands a task to the orchestrator of the one project the
  selector names exactly (its label in any letter case, or its orchestrator's exact address or
  name), and only when the person's message names it (the `--item` gate, exit 28 otherwise).
  What pan types is the task, the thread's link and a filled-in command:
  `pan handoff --thread <ts> --channel <id> --to <stream>`.
- Until the orchestrator runs it, the thread is tied to the project in an awaiting-handoff state
  (`ThreadRecord.handoff`, `ProjectHandoff`), not to the orchestrator's session: several threads
  may wait on one orchestrator, and a session tie elsewhere leaves them waiting. Nothing the
  orchestrator says reaches the thread: its status message says "Handed to <project>", `check`
  only refreshes it, and the orchestrator runs no pan hooks for the thread (the record is not
  `started_by_pan`). A follow-up goes to the orchestrator again with the same command.
- The hand text tells the orchestrator its job is to pick the stream, not to do the task (unless
  it hands the task to itself). A live test showed an orchestrator told only to "pick or start
  the stream" doing the work itself and leaving the stream idle.
- `pan handoff --to <stream> [--note <text>]` (`backend/handoff.py`) passes the work on: it types
  the person's task (`ProjectHandoff.task`, stored by `pan send --project`, with any follow-ups
  sent while the thread waited), the orchestrator's note and the thread's link into the stream
  through `send_to_session`, so the stream gets exactly what a `to <name>` send would give it,
  with the same busy and prompt refusals (the thread keeps waiting), and the thread is tied to it.
  Handed to the orchestrator itself, nothing is typed: it already has the task. It lives in its
  own module because it builds on `session_control`, which imports `project_routing`. It matches
  only an exact
  address or terminal name, accepts the orchestrator itself (found by address, so it works after
  its last stream ended), and with `--item` counts only the requester's message. It refuses a
  thread that is not waiting (37), a name that is no live agent session (38) and a session
  outside the project (39).
- An ended orchestrator ends the wait: the next follow-up, `check` or watcher check unties the
  thread, marks its status message Stopped and posts once that the orchestrator has ended.
  Untying a waiting thread any other way (`forget`, `pan send --to`) also ends the wait. pan's
  own hooks skip a waiting thread, so an orchestrator that is pan's worker posts nothing there.
- The thread map's writers take an advisory `flock` on `threads.json.lock`, since the watcher's
  sweep now writes to it while `pan` commands run.
- Safety net: the watcher checks waiting threads every `HANDOFF_SWEEP_SECONDS`. When the
  orchestrator's turn ended (hc's turn count rose and it is idle, or it is gone) without a
  handoff, pan posts once: "No stream picked yet. Name one with `@pan to <stream>`." The thread
  is marked before the post, so a failed post is never repeated.
- Unchanged: an exact session selector (`to t48`, a terminal name) and any session in no project
  are routed directly; an inexact project name is proposed and confirmed through the existing
  `pan propose` / `pan confirm` flow (`--project-candidate`).

### The project map seam

- `core.seams.ProjectMapSource` has one method, `project_map(terminals) -> ProjectMap`. The
  caller passes the listing it works from, because it still needs that listing for turns,
  statuses and delivery; a source that reads the host itself may ignore it.
- `core.models.ProjectMap`, `MappedProject` and `MappedSession` follow herdrmap's JSON
  (`herdrmap map --json`, version 1): `projects` (`label`, `root`, `sessions` with the
  orchestrator first and each stream right after its parent), `standalone` and `other`.
- Routing (`backend/project_routing.py`, `backend/confirmations.py`), `collect_sessions` (and
  through it the Slack sessions list, which renders `SessionSummary`) get the map only through
  the seam. `frontend/wiring.py` builds the source once (`Wiring.project_map_source`) and the CLI
  injects it, as it does the terminal host and the git reader.
- Config `project_map` (`ProjectMapKind`, default `builtin`) picks the source in one registry,
  `backend/project_map_sources.PROJECT_MAP_SOURCES` (name to factory, like herdrmap's
  `HOSTS` / `DEFAULT_HOST`). `utils/config.py` rejects an unknown value when the config loads.
- `backend/project_map.py` is the built-in source, `BuiltinProjectMapSource`. It now also reads
  each session's branch and a stream's workset goal, which `collect_sessions` used to read, so
  the built-in source fills the same fields herdrmap does. ruff bans importing
  `pan.backend.project_map` (`TID251`) everywhere except the registry and
  `tests/unit/test_project_map.py`. The agent-session checks it shares with the Slack list
  (`is_agent_session`, `is_live_agent_session`) moved to `backend/delivery.py`, so they outlive
  the built-in map.

### Field mapping to herdrmap's JSON

The map models use herdrmap's names. Where pan's own types keep a different name, this is the
mapping:

| herdrmap | pan |
| --- | --- |
| session `status` | `Terminal.agent_status`, `SessionSummary.agent_status` (kept: used across pan) |
| session `goal` | `SessionSummary.goal` (renamed from `workset_goal`) |
| project `label` | `SessionSummary.project`, `ProjectHandoff.project` |
| project `root` | `SessionSummary.project_root`, `ProjectHandoff.project_root` |
| session `place` (`space`, `tab`) | `Terminal.where` ("space / tab"); not on `MappedSession` |
| session `machine` | the address prefix (`machine/t7`); not on `MappedSession` |
| `inbox_thread`, `inbox_folder`, `extras` | not carried yet |
| `cwd` (a string) | `MappedSession.cwd` (a `Path`) |

`SessionSummary.project` and `.project_root` now mean what herdrmap's `show --json` means by a
session's `project`: null for a session in no project (before this, a standalone session was its
own one-session project).

## Adding a project map source (herdrmap later)

1. Add `src/pan/data/adapters/herdrmap.py` with a class whose `project_map(terminals)` runs
   `herdrmap map --json --all` (read-only), checks `"version": 1`, and validates the output into
   `ProjectMap` (the keys pan does not carry are ignored). It shells `herdrmap`, so it lives with
   the other adapters, and no herdrmap type leaves it. A failure raises a `PanError` subclass.
2. Add `HERDRMAP = "herdrmap"` to `ProjectMapKind` and one entry to `PROJECT_MAP_SOURCES`
   (its factory ignores the git reader).
3. Set `"project_map": "herdrmap"` in `~/.config/pan/config.json`.
4. Compare: run both sources over the same `hc ls --json` listing (or `pan sessions --json` with
   each config value) and diff the maps until they agree.
5. Then delete `backend/project_map.py`, its registry entry, `ProjectMapKind.BUILTIN`, the ruff
   ban and `tests/unit/test_project_map.py`, and make `herdrmap` the default.

## Options considered

- Route a project task straight to a stream pan picks itself. pan would have to guess what the
  orchestrator knows (which stream owns the work); rejected.
- Hand to the orchestrator and tie the thread to it as a session. Its chatter would reach the
  thread, and a second thread for the same orchestrator would untie the first; rejected.
- Hand to the orchestrator with an awaiting-handoff tie and a one-time nudge (option A). Chosen.
- Call herdrmap now. It is unfinished; rejected until it is. Keep pan's map but behind a seam
  whose models already match herdrmap's JSON, so switching is one adapter and one config value.
  Chosen.

## Consequences

- `pan sessions --json`: `workset_goal` is now `goal`, and `project` / `project_root` are null for
  a session in no project.
- The routing entry points that resolve a project (`hand_to_project`, `handoff_thread`,
  `propose_sessions`, `confirm_session`) and `collect_sessions` take a `project_map_source`; a
  follow-up to a waiting thread finds its orchestrator by address or name and needs no map.
- The built-in map reads git branches whenever routing needs the map (one cheap read per
  distinct folder), not only for `pan sessions`.
