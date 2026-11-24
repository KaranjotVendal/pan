from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from pan.config import CONFIRMATION_MAX_CANDIDATES, CONFIRMATION_TIMEOUT
from pan.directive import is_decline, picks_number, says_yes
from pan.errors import (
    NoExactMatchError,
    SessionBusyError,
    TargetBlockedError,
    TargetNotFoundError,
    UnansweredQuestionError,
)
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.models import (
    ConfirmationCandidate,
    PendingConfirmation,
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
EXPIRED_REPLY = "Not sent: that question expired. Send the message again."
# `pan confirm --choice`: `yes` (or `y`) picks the only proposed session, a number picks one.
CONFIRM_YES = "yes"
_YES_WORDS = frozenset({"yes", "y"})


def confirmations_path(threads_path: Path) -> Path:
    # Kept next to the thread map.
    return threads_path.with_name("confirmations.json")


def _key(source: ThreadSource, thread_ts: str) -> str:
    return f"{source.value}:{thread_ts}"


class FilePendingConfirmations:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _read_all(self) -> dict[str, PendingConfirmation]:
        # Never fatal, because `pan inbox drain` reads it: an unreadable or malformed file
        # counts as no questions, and a malformed entry is skipped (both are dropped on the
        # next write).
        try:
            raw_store = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            logger.warning(f"confirmations unreadable, treating as none: {type(e).__name__}")
            return {}
        if not isinstance(raw_store, dict):
            logger.warning("confirmations file is not an object, treating as none")
            return {}
        pending_by_key: dict[str, PendingConfirmation] = {}
        for stored_key, raw_pending in raw_store.items():
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

    def all_pending(self) -> list[PendingConfirmation]:
        return list(self._read_all().values())

    def put(self, pending: PendingConfirmation) -> None:
        # Replaces the thread's question, and drops every question that had expired by the
        # time this one was asked, so the file stays small.
        oldest_live = pending.last_asked_at() - CONFIRMATION_TIMEOUT
        pending_by_key = {
            key: stored
            for key, stored in self._read_all().items()
            if stored.last_asked_at() > oldest_live
        }
        pending_by_key[_key(pending.source, pending.thread_ts)] = pending
        self._write_all(pending_by_key)
        logger.info(f"confirmation stored thread={pending.thread_ts}")

    def mark_answered(
        self, source: ThreadSource, thread_ts: str, answered_at: datetime, answer_text: str
    ) -> None:
        # The requester replied in the thread after the question; the latest reply wins, so a
        # change of mind ("yes" then "no wait") is what `pan confirm` sees. A no-op when there
        # is no question.
        self._set_answer(source, thread_ts, answered_at, answer_text)
        logger.info(f"confirmation answered thread={thread_ts}")

    def clear_answer(self, source: ThreadSource, thread_ts: str, asked_at: datetime) -> None:
        # pan asked again at `asked_at`: an earlier reply no longer answers the question.
        self._set_answer(source, thread_ts, None, None, asked_at=asked_at)
        logger.info(f"confirmation answer cleared thread={thread_ts}")

    def _set_answer(
        self,
        source: ThreadSource,
        thread_ts: str,
        answered_at: datetime | None,
        answer_text: str | None,
        *,
        asked_at: datetime | None = None,
    ) -> None:
        pending_by_key = self._read_all()
        key = _key(source, thread_ts)
        stored = pending_by_key.get(key)
        if stored is None:
            return
        update: dict[str, object] = {"answered_at": answered_at, "answer_text": answer_text}
        if asked_at is not None:
            update["asked_at"] = asked_at
        pending_by_key[key] = stored.model_copy(update=update)
        self._write_all(pending_by_key)

    def clear(self, source: ThreadSource, thread_ts: str) -> None:
        pending_by_key = self._read_all()
        if pending_by_key.pop(_key(source, thread_ts), None) is not None:
            self._write_all(pending_by_key)
            logger.info(f"confirmation cleared thread={thread_ts}")


def live_pending(pending: PendingConfirmation | None, now: datetime) -> PendingConfirmation | None:
    # The question, unless it has expired. Measured from when pan last asked, so asking again
    # keeps the question open longer.
    if pending is None or now - pending.last_asked_at() >= CONFIRMATION_TIMEOUT:
        return None
    return pending


def is_answer(pending: PendingConfirmation, slack_user: str, received_at: datetime) -> bool:
    # A message answers the question when the requester sent it after pan last asked and
    # before the question expired. A question stored without a requester (before pan kept one)
    # is never answered: it expires.
    return (
        pending.requester is not None
        and slack_user == pending.requester
        and pending.last_asked_at() < received_at
        and live_pending(pending, received_at) is not None
    )


def normalize_choice(choice: str) -> str | None:
    # `yes` for yes or y, the digit for a number from 1 to CONFIRMATION_MAX_CANDIDATES, any
    # case and surrounding spaces; None for anything else.
    cleaned = choice.strip().lower()
    if cleaned in _YES_WORDS:
        return CONFIRM_YES
    if cleaned.isdigit() and 1 <= int(cleaned) <= CONFIRMATION_MAX_CANDIDATES:
        return cleaned
    return None


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
                address=address,
                name=terminal.name,
                cwd=terminal.cwd,
                where=terminal.where,
                label=candidate_label(terminal),
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
    # The orchestrator's guess for a named session with no exact match: store the task and
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
    pending = pending.model_copy(update={"asked_at": pending.created_at})
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
    # The live terminal at the candidate's address is still the same session: same name, or
    # for a session with no name, same folder and Herdr label. Anything else now at that
    # address is a different session.
    for terminal in terminals:
        if terminal.address != candidate.address:
            continue
        if candidate.name is not None:
            return terminal.name == candidate.name
        return terminal.cwd == candidate.cwd and terminal.where == candidate.where
    return False


def _retry_hint(choice: str) -> str:
    return f"reply {choice} again later, or anything else to cancel."


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
    # The person's `yes` or number for the thread's open question, as the orchestrator read
    # their reply: send the stored task to the chosen session through the normal send path
    # (the thread is tied to it), then clear the question. Refused with nothing posted until
    # the drain has seen the requester reply (answered_at). A choice that picks nothing (`yes`
    # to several, a number out of range) asks again. A busy session or one waiting on a prompt is refused as usual, but
    # the question stays open and the reply says to answer again later. A chosen session no
    # longer running clears the question, gets a reply and TargetNotFoundError. An expired
    # question is cleared with a reply; with no question at all nothing is posted.
    stored = confirmations.get(ThreadSource.SLACK, thread_ts)
    if stored is None:
        logger.info(f"confirm with no open question thread={thread_ts}")
        return None
    pending = live_pending(stored, clock.now())
    if pending is None:
        confirmations.clear(ThreadSource.SLACK, thread_ts)
        slack_post(slack, channel, thread_ts, EXPIRED_REPLY)
        return None
    _require_answer_for(pending, choice)
    chosen_index = _chosen_index(choice, len(pending.candidates))
    if chosen_index is None:
        confirmations.clear_answer(ThreadSource.SLACK, thread_ts, clock.now())
        slack_post(slack, channel, thread_ts, _reask_text(pending.candidates))
        return None
    candidate = pending.candidates[chosen_index]
    gone_reply = f"{candidate.label} is no longer running; not sent"
    if not _still_live(candidate, _live_terminals(terminal_host)):
        confirmations.clear(ThreadSource.SLACK, thread_ts)
        slack_post(slack, channel, thread_ts, gone_reply)
        raise TargetNotFoundError(f"proposed session {candidate.address} is gone")
    logger.info(f"confirmed session thread={thread_ts} terminal={candidate.address}")
    try:
        tied = send_to_session(
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
            retry_hint=_retry_hint(choice),
        )
    except SessionBusyError, TargetBlockedError:
        # The question stays open and the reply asked for an answer later: a new one.
        confirmations.clear_answer(ThreadSource.SLACK, thread_ts, clock.now())
        raise
    except NoExactMatchError as error:
        # Gone between the check and the send.
        confirmations.clear(ThreadSource.SLACK, thread_ts)
        slack_post(slack, channel, thread_ts, gone_reply)
        raise TargetNotFoundError(f"proposed session {candidate.address} is gone") from error
    confirmations.clear(ThreadSource.SLACK, thread_ts)
    return tied


def _require_answer_for(pending: PendingConfirmation, choice: str) -> None:
    # The choice must rest on the requester's own reply since pan last asked: one that does
    # not decline and that says yes (for `yes`) or starts with the chosen number.
    answer_text = pending.answer_text
    if pending.answered_at is None or answer_text is None:
        logger.info(f"confirm refused, no reply since asked thread={pending.thread_ts}")
        raise UnansweredQuestionError(
            f"the requester has not replied to the question in thread {pending.thread_ts}"
        )
    if is_decline(answer_text):
        logger.info(f"confirm refused, the reply declines thread={pending.thread_ts}")
        raise UnansweredQuestionError(f"the reply in thread {pending.thread_ts} declines")
    picked = says_yes(answer_text) if choice == CONFIRM_YES else picks_number(answer_text, choice)
    if not picked:
        logger.info(f"confirm refused, the reply does not pick {choice} thread={pending.thread_ts}")
        raise UnansweredQuestionError(
            f"the reply in thread {pending.thread_ts} does not pick {choice}"
        )


def cancel_confirmation(
    *, thread_ts: str, channel: str, confirmations: PendingConfirmations, slack: SlackAdapter
) -> bool:
    # The person declined or moved on: drop the question and say nothing was sent. True when
    # there was one.
    if confirmations.get(ThreadSource.SLACK, thread_ts) is None:
        return False
    confirmations.clear(ThreadSource.SLACK, thread_ts)
    slack_post(slack, channel, thread_ts, NOT_SENT_REPLY)
    return True
