# 0002: The backend reports typed events; the frontend shows them

Status: accepted

## Context

decisions/0001-layers.md left seven backend imports of `frontend` on an allow-list: the
backend modules that spawn, steer and close sessions (`confirmations`, `session_control`,
`spawn`, `status_message`) wrote their Slack wording and called the Slack posting code
themselves. pan is about to read a second source (X, read-only: pan never posts there), whose
work will be reported somewhere else again (a fixed Slack thread, or the Plannotator Inbox).
With wording and posting inside the backend, every new place to report would mean another
branch in each of those modules.

The principle: fix the message types, leave the transport open. The backend returns models and
reports what happened; it never builds rendered output.

## Decision

- `pan.core.events` holds one frozen pydantic model per thing the backend reports, joined in
  the union `Event`. Being pydantic models they are serialisable, but nothing serialises them
  yet, so no wire format (such as a type tag) is fixed until a reporter needs one. The set is
  exactly what the backend posted before: the status message (`StatusChanged`), an answer
  (`AnswerReady`), a session's screen lines (`ScreenCaptured`), the requester's private details
  (`PrivateDetails`), the private sessions list (`SessionsListed`), pan's question and its
  re-ask (`QuestionAsked`, `ChoiceUnclear`), and the notices. Notices whose wording is pan's
  own are typed reasons, not text: `ThreadNotice` (`ThreadNoticeReason`), `SessionNotice`
  (`SessionNoticeReason`), `NotDelivered` (`NotDeliveredReason`, with the choice to send again),
  `StartFailed` (`StartAction`), `NoSessionMatches`, `WorkKept`, `CleanupFailed` and
  `SessionClosed`. No event holds wording or markup. The one exception to wording living in
  the reporter is `spawn.start_failed_text`: the same sentence is the `SpawnError` message, so
  it stays in the backend and the Slack reporter reuses it.
- `pan.core.seams.Reporter` is the one seam: `report(conversation, event) -> str | None`. For a
  `StatusChanged` it returns the id of a newly posted status message (None when it edited the
  one the event named), so the backend can store it; for every other event it returns None. A
  failed delivery raises `SlackPostError`. `OpenReporter` (`Callable[[], Reporter]`) is for work
  that must finish even when no reporter can be built (`stop`, and the gates' refusal notice).
- The backend keeps every decision and all bookkeeping: what to report and when, what a
  failure does (logged or raised, as before), and what is recorded only after a successful
  report (`status_message_ts`, `last_answer_digest`).
- `pan.frontend.slack_reporter.SlackReporter` is the Slack reporter: it renders each event with
  `slack_render` into the exact text and blocks pan posted before and sends them through the
  `SlackAdapter`, editing the status message in place and falling back to a new post when the
  edit fails. `wiring.Wiring.reporter` builds it; the CLI and hooks pass it in.

## Options considered

- Keep the allow-list and add a second set of posting calls for the next reporter. No work now,
  but every module that reports would grow a branch per destination. Rejected.
- Have the backend return rendered text and let the frontend choose where to send it. The
  transport would be open, but the wording would still live in the backend, and another
  reporter could not word or lay out the same event differently. Rejected.
- Typed events through a reporter seam. The wording and transport live in one frontend module
  per destination, and the backend can be tested on what it reports. Chosen.

## Consequences

- The backend imports nothing from `frontend`; decisions/0001-layers.md's allow-list is empty.
- A new destination is a new `Reporter` in `frontend`; the backend does not change.
- A new kind of report is a new event in `core.events` and a rendering in each reporter.
- Backend tests assert the events reported (a recording `Reporter`); the reporter's tests pin
  each event's exact Slack output.
- `SlackPostError` is still the reporter's failure type; a transport-neutral name is left for
  when a second reporter exists.
