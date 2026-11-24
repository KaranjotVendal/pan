from __future__ import annotations

from pan.errors import SlackPostError
from pan.logging import initialise_logger
from pan.models import ReplyMode, StatusState, ThreadRecord
from pan.seams import Clock, SlackAdapter, ThreadMap
from pan.slack_render import private_details_text, status_blocks, status_text

logger = initialise_logger(__name__)


def publish_status(
    *,
    slack: SlackAdapter,
    thread_map: ThreadMap,
    record: ThreadRecord,
    state: StatusState,
    clock: Clock,
    detail: str | None = None,
) -> ThreadRecord:
    # pan's one status message in the record's thread: edited in place when the record holds
    # its ts, else (none yet, or the edit failed: deleted, too old) posted anew and its ts
    # stored. Best effort: the work it reports on is already done, so a Slack failure is
    # logged and never raised. Returns the record with the message's ts. No message text is
    # logged (INV-9).
    blocks = status_blocks(
        state,
        session_name=record.terminal_name,
        label=record.status_label,
        started_at=record.task_started_at,
        now=clock.now(),
        replies_in_thread=record.reply_mode is ReplyMode.SLACK,
        detail=detail,
    )
    fallback_text = status_text(state, record.status_label)
    if record.status_message_ts:
        try:
            slack.update_message(record.channel, record.status_message_ts, blocks, fallback_text)
            logger.info(f"status edited thread={record.thread_ts} state={state}")
            return record
        except SlackPostError as e:
            logger.warning(
                f"status message could not be edited, posting a new one "
                f"thread={record.thread_ts}: {e}"
            )
    try:
        message_ts = slack.post_blocks(record.channel, record.thread_ts, blocks, fallback_text)
    except SlackPostError as e:
        logger.warning(f"status message could not be posted thread={record.thread_ts}: {e}")
        return record
    thread_map.record_status_message(
        record.thread_ts, message_ts, session_name=record.terminal_name
    )
    logger.info(f"status posted thread={record.thread_ts} state={state}")
    return record.model_copy(update={"status_message_ts": message_ts})


def send_private_details(*, slack: SlackAdapter, record: ThreadRecord) -> None:
    # The session's Inbox thread and terminal, as an ephemeral message to the requester only.
    # Skipped when the record has no requester; a failure is logged, never raised.
    if not record.requester:
        logger.info(f"private details skipped, no requester thread={record.thread_ts}")
        return
    try:
        slack.post_ephemeral(
            record.channel, record.thread_ts, record.requester, private_details_text(record)
        )
    except SlackPostError as e:
        logger.warning(f"private details could not be sent thread={record.thread_ts}: {e}")
        return
    logger.info(f"private details sent thread={record.thread_ts}")
