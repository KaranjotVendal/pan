from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from pan.backend.confirmations import is_answer, live_pending
from pan.backend.directive import clean_message
from pan.core.models import (
    InboxItem,
    PendingConfirmation,
    ThreadRecord,
    ThreadSource,
    WorkerStatus,
)
from pan.core.seams import Clock, DrainedLog, InboxStore, PendingConfirmations, ThreadMap
from pan.data.threadmap import tie_note
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)


class ThreadTie(BaseModel, frozen=True):
    session: str
    terminal_address: str
    status: WorkerStatus
    note: str


class QuestionCandidate(BaseModel, frozen=True):
    number: int
    label: str
    address: str


class OpenQuestion(BaseModel, frozen=True):
    selector: str
    task: str
    requester: str | None
    # isoformat(), as the orchestrator has always read it (pydantic would write "Z" for UTC).
    asked_at: str
    candidates: list[QuestionCandidate]
    answered_by_requester: bool
    answer_text: str | None


class DrainedItem(BaseModel, frozen=True):
    # An inbox item as `pan inbox drain --json` prints it. The orchestrator's skill reads these
    # fields by name (item.thread_ts, item.channel, item.slack_user, ...), so they keep their
    # flat Slack-era names whatever shape pan stores items in. `channel` is None for a
    # conversation with no place.
    id: str
    slack_user: str
    channel: str | None
    thread_ts: str
    is_thread_reply: bool
    raw_text: str
    received_at: datetime
    source: ThreadSource
    mentioned: bool

    @classmethod
    def from_inbox_item(cls, item: InboxItem) -> DrainedItem:
        return cls(
            id=item.id,
            slack_user=item.author,
            channel=item.conversation.place,
            thread_ts=item.conversation.id,
            is_thread_reply=item.is_thread_reply,
            raw_text=item.raw_text,
            received_at=item.received_at,
            source=item.conversation.source,
            mentioned=item.mentioned,
        )


class DrainedEntry(BaseModel, frozen=True):
    # Facts only: what the person typed (mention, signature and phone punctuation cleaned), the
    # thread's tie read at drain time (it can change after the gateway wrote the item), and
    # pan's open question in the thread. What the message means is the orchestrator's call.
    item: DrainedItem
    text: str
    tie: ThreadTie | None
    open_question: OpenQuestion | None


def drain_inbox(
    *,
    inbox_store: InboxStore,
    thread_map: ThreadMap,
    confirmations: PendingConfirmations,
    drained_log: DrainedLog,
    clock: Clock,
) -> list[DrainedEntry]:
    """Drain the inbox and describe each item. The items are recorded for the gates, and a
    reply from the requester after an open question marks it answered (pan confirm --choice
    needs that); neither write can cost the drained items."""
    # The open questions are read before the drain, and reading them never fails, so a bad
    # confirmations file can never cost drained items.
    open_questions = {pending.conversation.key: pending for pending in confirmations.all_pending()}
    items = inbox_store.drain()
    now = clock.now()
    _record_drained(drained_log, items, now)

    entries: list[DrainedEntry] = []
    for item in items:
        key = item.conversation.key
        pending = live_pending(open_questions.get(key), now)
        if pending is not None and is_answer(pending, item.author, item.received_at):
            answered = _mark_answered(confirmations, pending, item)
            if answered is not None:
                # The latest reply from the requester is the answer, also for later items.
                pending = answered
                open_questions[key] = answered
        entries.append(
            DrainedEntry(
                item=DrainedItem.from_inbox_item(item),
                text=clean_message(item.raw_text),
                tie=_thread_tie(thread_map.get(item.conversation)),
                open_question=_open_question(pending) if pending is not None else None,
            )
        )
    return entries


def _thread_tie(record: ThreadRecord | None) -> ThreadTie | None:
    note = tie_note(record)
    if record is None or note is None:
        return None
    return ThreadTie(
        session=record.terminal_name,
        terminal_address=record.terminal_address,
        status=record.status,
        note=note,
    )


def _open_question(pending: PendingConfirmation) -> OpenQuestion:
    answered = pending.answered_at is not None
    return OpenQuestion(
        selector=pending.selector,
        task=pending.task,
        requester=pending.requester,
        asked_at=pending.last_asked_at().isoformat(),
        candidates=[
            QuestionCandidate(number=number, label=candidate.label, address=candidate.address)
            for number, candidate in enumerate(pending.candidates, start=1)
        ],
        answered_by_requester=answered,
        answer_text=pending.answer_text if answered else None,
    )


def _mark_answered(
    confirmations: PendingConfirmations, pending: PendingConfirmation, item: InboxItem
) -> PendingConfirmation | None:
    # Runs after the drain, so a failed write must not cost the drained items: it is logged
    # and the question stays unanswered (`pan confirm --choice` then refuses).
    answer_text = clean_message(item.raw_text)
    try:
        confirmations.mark_answered(pending.conversation, item.received_at, answer_text)
    except OSError as e:
        logger.warning(
            f"could not mark the question answered conversation={pending.conversation.key}: "
            f"{type(e).__name__}"
        )
        return None
    return pending.model_copy(update={"answered_at": item.received_at, "answer_text": answer_text})


def _record_drained(drained_log: DrainedLog, items: list[InboxItem], now: datetime) -> None:
    # A failed write must not cost the drained items: the gates that read the log then refuse.
    try:
        drained_log.record(items, now)
    except OSError as e:
        logger.warning(f"could not record the drained items: {type(e).__name__}")
