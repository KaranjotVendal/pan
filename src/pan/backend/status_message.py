from __future__ import annotations

import hashlib

from pan.backend.sessions import inbox_thread_known
from pan.core.errors import SlackPostError
from pan.core.events import AnswerReady, PrivateDetails, StatusChanged
from pan.core.models import (
    AgentStatus,
    ReplyMode,
    StatusState,
    Terminal,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.core.seams import Clock, Reporter, ThreadMap
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# The state `check` shows for a live terminal, from the agent status hc reports. hc's `done`
# means the turn finished, not that the session was closed: Closed comes only from `@pan done`.
_STATE_FOR_LIVE_AGENT: dict[AgentStatus, StatusState] = {
    AgentStatus.WORKING: StatusState.WORKING,
    AgentStatus.BLOCKED: StatusState.WAITING,
    AgentStatus.IDLE: StatusState.FINISHED,
    AgentStatus.DONE: StatusState.FINISHED,
    AgentStatus.ERROR: StatusState.STOPPED,
}
# A live terminal reporting no status or an unknown one (a fresh Claude session before its
# first turn, say) shows what pan recorded; the terminal is live, so never Closed or Stopped.
_STATE_FOR_LIVE_RECORD: dict[WorkerStatus, StatusState] = {
    WorkerStatus.SPAWNING: StatusState.WORKING,
    WorkerStatus.RUNNING: StatusState.WORKING,
    WorkerStatus.BLOCKED: StatusState.WAITING,
    WorkerStatus.IDLE: StatusState.FINISHED,
    WorkerStatus.DONE: StatusState.FINISHED,
    WorkerStatus.FAILED: StatusState.FINISHED,
}


def check_state(terminal: Terminal | None, recorded: WorkerStatus) -> StatusState:
    # The status message's state on `check`. A terminal that is gone (or finished) is Stopped,
    # unless pan recorded the session as closed by `@pan done` (DONE is only set there).
    if terminal is None or terminal.state is TerminalState.FINISHED:
        return StatusState.CLOSED if recorded is WorkerStatus.DONE else StatusState.STOPPED
    live_state = _STATE_FOR_LIVE_AGENT.get(terminal.agent_status)
    return live_state if live_state is not None else _STATE_FOR_LIVE_RECORD[recorded]


def publish_status(
    *,
    reporter: Reporter,
    thread_map: ThreadMap,
    record: ThreadRecord,
    state: StatusState,
    clock: Clock,
    detail: str | None = None,
) -> ThreadRecord:
    # pan's one status message in the record's thread: the reporter edits it in place when the
    # record holds its id, else (none yet, or the edit failed: deleted, too old) posts it anew,
    # and the new id is stored. Best effort: the work it reports on is already done, so a
    # failure is logged and never raised. Returns the record with the message's id. No message
    # text is logged (INV-9).
    status = StatusChanged(
        state=state,
        session_name=record.terminal_name,
        label=record.status_label,
        task_started_at=record.task_started_at,
        updated_at=clock.now(),
        replies_in_thread=record.reply_mode is ReplyMode.SLACK,
        detail=detail,
        status_message_id=record.status_message_ts,
    )
    try:
        posted_message_id = reporter.report(record.conversation, status)
    except SlackPostError as e:
        logger.warning(
            f"status message could not be posted conversation={record.conversation.key}: {e}"
        )
        return record
    if posted_message_id is None:
        if record.status_message_ts is None:
            logger.warning(
                f"status posted without a message id to store "
                f"conversation={record.conversation.key} state={state}"
            )
        else:
            logger.info(f"status edited conversation={record.conversation.key} state={state}")
        return record
    thread_map.record_status_message(
        record.conversation, posted_message_id, session_name=record.terminal_name
    )
    logger.info(f"status posted conversation={record.conversation.key} state={state}")
    return record.model_copy(update={"status_message_ts": posted_message_id})


def answer_digest(answer: str) -> str:
    return hashlib.sha256(answer.encode()).hexdigest()


def post_answer(
    *, reporter: Reporter, thread_map: ThreadMap, record: ThreadRecord, answer: str
) -> bool:
    # Report the session's answer alone, unless it is the answer pan last posted to this
    # thread (the Stop hook already posted it, and `check` asks again). Stores its digest once
    # posted. True when posted. A failed post raises SlackPostError.
    digest = answer_digest(answer)
    if digest == record.last_answer_digest:
        logger.info(
            f"answer already posted, not posting again conversation={record.conversation.key}"
        )
        return False
    reporter.report(record.conversation, AnswerReady(text=answer))
    thread_map.record_posted_answer(record.conversation, digest, session_name=record.terminal_name)
    return True


def send_private_details(*, reporter: Reporter, record: ThreadRecord) -> None:
    # The session's Inbox thread and terminal, to the requester only. Skipped when the record
    # has no requester; a failure is logged, never raised.
    if not record.requester:
        logger.info(f"private details skipped, no requester conversation={record.conversation.key}")
        return
    details = PrivateDetails(
        requester=record.requester,
        session_name=record.terminal_name,
        terminal_address=record.terminal_address,
        inbox_thread_name=record.inbox_thread_name,
        inbox_thread_url=record.inbox_thread_url,
        inbox_thread_known=inbox_thread_known(record),
    )
    try:
        reporter.report(record.conversation, details)
    except SlackPostError as e:
        logger.warning(
            f"private details could not be sent conversation={record.conversation.key}: {e}"
        )
        return
    logger.info(f"private details sent conversation={record.conversation.key}")
