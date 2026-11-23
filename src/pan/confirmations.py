from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from pan.config import CONFIRMATION_MAX_CANDIDATES, CONFIRMATION_TIMEOUT
from pan.directive import CONFIRM_YES, parse_confirm_reply
from pan.errors import TargetNotFoundError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import (
    ConfirmationCandidate,
    PendingConfirmation,
    Route,
    RouteKind,
    Terminal,
    TerminalState,
    ThreadRecord,
    ThreadSource,
)
from pan.seams import (
    Clock,
    InboxDirectory,
    PendingConfirmations,
    SlackAdapter,
    TerminalHost,
    ThreadMap,
)
from pan.session_control import send_to_session

logger = initialise_logger(__name__)

# How to start new work instead, offered when a `to` selector matches no session at all.
NEW_WORK_HINT = "@pan new: <your text>"
NOT_SENT_REPLY = "Not sent."
EXPIRED_REPLY = "Not sent: that question expired. Send the `to <name>:` message again."


def confirmations_path(threads_path: Path) -> Path:
    # Kept next to the thread map.
    return threads_path.with_name("confirmations.json")


def _key(source: ThreadSource, thread_ts: str) -> str:
    return f"{source.value}:{thread_ts}"


class FilePendingConfirmations:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _read_all(self) -> dict[str, PendingConfirmation]:
        # A malformed entry is skipped (and dropped on the next write), never fatal.
        if not self._path.exists():
            return {}
        pending_by_key: dict[str, PendingConfirmation] = {}
        for stored_key, raw_pending in json.loads(self._path.read_text()).items():
            try:
                pending_by_key[stored_key] = PendingConfirmation.model_validate(raw_pending)
            except ValidationError:
                logger.warning(f"confirmations skipping unparseable entry key={stored_key}")
        return pending_by_key

    def _write_all(self, pending_by_key: dict[str, PendingConfirmation]) -> None:
        payload = {key: pending.model_dump(mode="json") for key, pending in pending_by_key.items()}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(self._path.name + ".tmp")
        temp_path.write_text(json.dumps(payload))
        temp_path.replace(self._path)

    def get(self, source: ThreadSource, thread_ts: str) -> PendingConfirmation | None:
        return self._read_all().get(_key(source, thread_ts))

    def put(self, pending: PendingConfirmation) -> None:
        # Replaces the thread's question, and drops every question that had expired by the
        # time this one was asked, so the file stays small.
        oldest_live = pending.created_at - CONFIRMATION_TIMEOUT
        pending_by_key = {
            key: stored
            for key, stored in self._read_all().items()
            if stored.created_at > oldest_live
        }
        pending_by_key[_key(pending.source, pending.thread_ts)] = pending
        self._write_all(pending_by_key)
        logger.info(f"confirmation stored thread={pending.thread_ts}")

    def clear(self, source: ThreadSource, thread_ts: str) -> None:
        pending_by_key = self._read_all()
        if pending_by_key.pop(_key(source, thread_ts), None) is not None:
            self._write_all(pending_by_key)
            logger.info(f"confirmation cleared thread={thread_ts}")


def live_pending(pending: PendingConfirmation | None, now: datetime) -> PendingConfirmation | None:
    # The question, unless it has expired.
    if pending is None or now - pending.created_at >= CONFIRMATION_TIMEOUT:
        return None
    return pending


def route_with_pending(
    route: Route, raw_text: str, pending: PendingConfirmation | None, now: datetime
) -> tuple[Route, bool]:
    # With a live question open in the thread, a `yes`/`y`/1-3 reply becomes a CONFIRM route;
    # any other message keeps its route and cancels the question (the bool). With no live
    # question (none, or expired) nothing changes, so a plain `yes` in a tied thread still
    # goes to the tied session.
    if live_pending(pending, now) is None:
        return route, False
    choice = parse_confirm_reply(raw_text)
    if choice is None:
        return route, True
    return Route(kind=RouteKind.CONFIRM, choice=choice), False


def candidate_label(terminal: Terminal) -> str:
    # The session as `sessions` shows it: name, then Herdr label and folder.
    name = terminal.name or terminal.address
    details = [terminal.where, f"folder {terminal.cwd.name}" if terminal.cwd.name else None]
    shown = ", ".join(detail for detail in details if detail)
    return f"{name} ({shown})" if shown else name


def no_match_reply(selector: str) -> str:
    return f'no session matches "{selector}"; to start new work send `{NEW_WORK_HINT}`'


def _numbers_phrase(count: int) -> str:
    numbers = [str(number) for number in range(1, count + 1)]
    return f"{', '.join(numbers[:-1])} or {numbers[-1]}"


def question_text(candidates: list[ConfirmationCandidate]) -> str:
    if len(candidates) == 1:
        return f"Send this to {candidates[0].label}? Reply yes."
    numbered = "\n".join(
        f"{number}. {candidate.label}" for number, candidate in enumerate(candidates, start=1)
    )
    return f"Which session? Reply {_numbers_phrase(len(candidates))}:\n{numbered}"


def _reask_text(candidates: list[ConfirmationCandidate]) -> str:
    if len(candidates) == 1:
        return "Reply yes to send it, or anything else to cancel."
    return f"Reply {_numbers_phrase(len(candidates))} to choose, or anything else to cancel."


def _live_terminals(terminal_host: TerminalHost) -> list[Terminal]:
    return [
        terminal
        for terminal in terminal_host.list_terminals()
        if terminal.state is TerminalState.LIVE
    ]


def _valid_candidates(
    candidate_addresses: list[str], terminals: list[Terminal]
) -> list[ConfirmationCandidate]:
    # The proposed addresses that are live terminals, in order, each once, at most
    # CONFIRMATION_MAX_CANDIDATES. An address that is not live is dropped with a log line.
    by_address = {terminal.address: terminal for terminal in terminals}
    candidates: list[ConfirmationCandidate] = []
    for address in candidate_addresses:
        terminal = by_address.get(address)
        if terminal is None:
            logger.warning(f"proposed session is not a live terminal address={address}")
            continue
        if any(candidate.address == address for candidate in candidates):
            continue
        candidates.append(
            ConfirmationCandidate(
                address=address, name=terminal.name, label=candidate_label(terminal)
            )
        )
    if len(candidates) > CONFIRMATION_MAX_CANDIDATES:
        logger.warning(
            f"proposal cut to {CONFIRMATION_MAX_CANDIDATES} of {len(candidates)} sessions"
        )
    return candidates[:CONFIRMATION_MAX_CANDIDATES]


def propose_sessions(
    *,
    thread_ts: str,
    channel: str,
    selector: str,
    task: str,
    requester: str | None,
    reply_in_slack: bool,
    candidate_addresses: list[str],
    terminal_host: TerminalHost,
    confirmations: PendingConfirmations,
    slack: SlackAdapter,
    clock: Clock,
) -> PendingConfirmation | None:
    # The orchestrator's guess for a `to <selector>:` with no exact match: store the task and
    # the proposed sessions as the thread's open question and ask the person to confirm.
    # Nothing is typed into any session here. Only live terminals count; with none, pan says
    # no session matches, stores nothing and clears any earlier question in the thread.
    candidates = _valid_candidates(candidate_addresses, _live_terminals(terminal_host))
    if not candidates:
        confirmations.clear(ThreadSource.SLACK, thread_ts)
        slack_post(slack, channel, thread_ts, no_match_reply(selector))
        logger.info(f"proposal found no session thread={thread_ts}")
        return None
    pending = PendingConfirmation(
        thread_ts=thread_ts,
        channel=channel,
        selector=selector,
        task=task,
        requester=requester,
        reply_in_slack=reply_in_slack,
        candidates=candidates,
        created_at=clock.now(),
    )
    confirmations.put(pending)
    slack_post(slack, channel, thread_ts, question_text(candidates))
    logger.info(f"proposed sessions thread={thread_ts} count={len(candidates)}")
    return pending


def _chosen_index(choice: str, candidate_count: int) -> int | None:
    # `yes` picks the only candidate; a number picks that candidate. Anything else is None.
    if choice == CONFIRM_YES:
        return 0 if candidate_count == 1 else None
    if choice.isdigit() and 1 <= int(choice) <= candidate_count:
        return int(choice) - 1
    return None


def _still_live(candidate: ConfirmationCandidate, terminals: list[Terminal]) -> bool:
    # The live terminal at the candidate's address is still the same session: a terminal
    # now at that address under another name is a different one.
    return any(
        terminal.address == candidate.address
        and (candidate.name is None or terminal.name == candidate.name)
        for terminal in terminals
    )


def confirm_session(
    *,
    thread_ts: str,
    channel: str,
    choice: str,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    inbox_directory: InboxDirectory,
    confirmations: PendingConfirmations,
    slack: SlackAdapter,
    clock: Clock,
) -> ThreadRecord | None:
    # The person's `yes` or number for the thread's open question: send the stored task to
    # the chosen session through the normal `to` send path (busy and prompt refusals apply,
    # and the thread is tied to it), and clear the question. A reply that picks nothing
    # (`yes` to several, a number out of range) asks again and keeps the question. A chosen
    # session that is no longer running gets a reply and TargetNotFoundError, nothing sent.
    pending = live_pending(confirmations.get(ThreadSource.SLACK, thread_ts), clock.now())
    if pending is None:
        confirmations.clear(ThreadSource.SLACK, thread_ts)
        slack_post(slack, channel, thread_ts, EXPIRED_REPLY)
        return None
    chosen_index = _chosen_index(choice, len(pending.candidates))
    if chosen_index is None:
        slack_post(slack, channel, thread_ts, _reask_text(pending.candidates))
        return None
    confirmations.clear(ThreadSource.SLACK, thread_ts)
    candidate = pending.candidates[chosen_index]
    if not _still_live(candidate, _live_terminals(terminal_host)):
        slack_post(slack, channel, thread_ts, f"{candidate.label} is no longer running; not sent")
        raise TargetNotFoundError(f"proposed session {candidate.address} is gone")
    logger.info(f"confirmed session thread={thread_ts} terminal={candidate.address}")
    return send_to_session(
        thread_ts=thread_ts,
        channel=channel,
        selector=candidate.address,
        text=pending.task,
        requester=pending.requester,
        reply_in_slack=pending.reply_in_slack,
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=inbox_directory,
        slack=slack,
        clock=clock,
    )


def cancel_confirmation(
    *, thread_ts: str, channel: str, confirmations: PendingConfirmations, slack: SlackAdapter
) -> bool:
    # Any other reply while a question is open: drop it and say nothing was sent. True when
    # there was one.
    if confirmations.get(ThreadSource.SLACK, thread_ts) is None:
        return False
    confirmations.clear(ThreadSource.SLACK, thread_ts)
    slack_post(slack, channel, thread_ts, NOT_SENT_REPLY)
    return True
