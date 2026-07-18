# AGENT_LOOP.md — Night Shift build loop for pan

You are a single autonomous Claude Code agent building `pan` overnight, unattended, under the
Night Shift paradigm (jamon.dev/night-shift). The human is asleep and reviews your commits in the
morning.

Ground rules for the whole run:

- You BUILD pan's code. You do NOT run pan, start the gateway, spawn workers, or drive any live
  orchestration overnight. Your product is committed source plus green gates plus a morning report.
- Work ONLY within `/Users/karanjot.vendal/dev/pan`. Never touch anything outside it.
- The branch is `night-shift`. All commits stack on it. No squash, no amend, no rebase, no remote.
- The task queue is the plan: `docs/superpowers/plans/2026-07-16-pan.md`. Work its tasks IN ORDER.
- The contracts, invariants (INV-1..9), and boundary rules (BR-1..6) are the tech spec:
  `docs/superpowers/specs/2026-07-16-pan-tech-spec.md`. The product rationale is the design spec:
  `docs/superpowers/specs/2026-07-16-pan-slack-orchestrator-design.md`.
- Read `AGENTS.md` first every session — it is the router to layout, conventions, and gates.

## The loop

0. Prep. Ensure the working tree is clean and you are on `night-shift`. Run the full gate:
   `uv run ruff check`; `uv run ruff format --check`; `uv run ty check`; `uv run pytest -q`. If
   anything is red, STOP and fix it before starting new work. Never build on a red harness.

1. Pick the next work. First, any open bug in `TODOS.md`. Otherwise, the next unchecked Task in the
   plan, in order, respecting dependencies. If the only remaining steps are DEFERRED or live (for
   example the Task 20 live smoke), skip them (see Hard Rules) and go to step 12.

2. Load only the relevant context: the task's slice of the tech spec and design spec, plus the code
   it touches. Do not load the whole repo into context.

3. Write the failing tests for the task's behaviors — MOCKED SEAMS ONLY. Run them and confirm they
   fail for the right reason (the code is absent, not a typo in the test).

4. Implement the minimal code to make them pass, applying the coding-standards skill and the
   conventions in `AGENTS.md`.

5. Run the strict gates and fix until all four are green:
   `uv run ruff check`; `uv run ruff format --check`; `uv run ty check`; `uv run pytest -q`.
   Never weaken a gate to pass — no new ruff ignores, no unjustified `# type: ignore`, no deleting
   or `xfail`-ing tests, no removing a behavior's coverage.

6. Review loop. Dispatch the six personas in `REVIEW_PERSONAS.md` as sub-agents against the task
   diff (`git diff`). Each returns a must-fix list and a verdict of APPROVE or BLOCK. Address every
   must-fix item, re-run the gates, and re-review until all six APPROVE. A persona may instead defer
   a genuinely non-blocking item to `TODOS.md` rather than block on it.

7. Update the docs the change affects — the `AGENTS.md` router and any skill it introduces or
   alters. Check off the completed steps in the plan file.

8. Record unrelated discoveries as new entries in `TODOS.md`. Do NOT fix them inline — no scope
   creep. The current task ships only its own behavior.

9. Append a human-facing entry under `## [Unreleased]` in `CHANGELOG.md` describing what changed
   and why.

10. Commit. Stage exactly the task's files and commit to `night-shift` with a DETAILED,
    human-facing message: a conventional prefix (`feat:`, `docs:`, `fix:`, ...), then what changed,
    why, and the context, and a list of the tests added. End the message with:
    `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
    No squash, no amend, no rebase — only new stacked commits.

11. Loop back to step 1.

12. Morning report. When the queue is exhausted (all non-deferred tasks done), write a concise
    report to `docs/superpowers/night-reports/<YYYY-MM-DD>.md` covering: tasks completed with their
    commit hashes; TODOs added; anything skipped or deferred (especially live items) and why;
    blockers hit; and suggested doc, spec, or skill improvements as postmortem hints for the human.
    Then STOP silently and wait.

## HARD RULES (non-negotiable)

- **LIVE TESTS ARE FORBIDDEN overnight.** Never open a real Slack or Socket Mode connection, never
  make a network call to Slack, never require the phone, and never run a live-verify write path
  against any real service. All tests use fakes or mocks at the seams. Any step that needs live
  verification is SKIPPED and logged in the morning report for the final human-driven session.
- **Never push and never touch a git remote.** Never rebase, force-push, `reset --hard`, amend, or
  rewrite existing commits. Only add new stacked commits on `night-shift`.
- **Never operate outside `/Users/karanjot.vendal/dev/pan`.**
- **Never write real secrets or tokens anywhere.** Credential tests use obviously-fake token
  strings, e.g. `xoxb-fake-...` / `xapp-fake-...`.
- **Never weaken a gate or a test to go green.** Fix the code, not the check.
- **When blocked, ambiguous, or a step needs a human decision or live access:** do not guess
  destructively. Add a TODO, note it in the report, and move to the next INDEPENDENT task. If none
  remain, write the report and stop.
- **Respect every invariant (INV-1..9) and boundary rule (BR-1..6)** from the tech spec on every
  task.
