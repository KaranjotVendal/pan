from __future__ import annotations

import hashlib

from pan.core.errors import SlackPostError
from pan.core.models import ReplyMode, StatusState, ThreadRecord
from pan.core.seams import Clock, SlackAdapter, ThreadMap
from pan.frontend.gateway.slack_post import slack_post_parts  # noqa: TID251
from pan.frontend.slack_render import (  # noqa: TID251
    private_details_text,
    status_blocks,
    status_text,
)
from pan.utils.logging import initialise_logger

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
            slack.update_message(
                record.conversation.slack_channel(),
                record.status_message_ts,
                blocks,
                fallback_text,
            )
            logger.info(f"status edited conversation={record.conversation.key} state={state}")
            return record
        except SlackPostError as e:
            logger.warning(
                f"status message could not be edited, posting a new one "
                f"conversation={record.conversation.key}: {e}"
            )
    try:
        message_ts = slack.post_blocks(
            record.conversation.slack_channel(), record.conversation.id, blocks, fallback_text
        )
    except SlackPostError as e:
        logger.warning(
            f"status message could not be posted conversation={record.conversation.key}: {e}"
        )
        return record
    thread_map.record_status_message(
        record.conversation, message_ts, session_name=record.terminal_name
    )
    logger.info(f"status posted conversation={record.conversation.key} state={state}")
    return record.model_copy(update={"status_message_ts": message_ts})


def answer_digest(answer: str) -> str:
    return hashlib.sha256(answer.encode()).hexdigest()


def post_answer(
    *, slack: SlackAdapter, thread_map: ThreadMap, record: ThreadRecord, answer: str
) -> bool:
    # Post the session's answer alone (split into parts when long), unless it is the answer
    # pan last posted to this thread (the Stop hook already posted it, and `check` asks
    # again). Stores its digest. True when posted. A Slack failure raises SlackPostError.
    digest = answer_digest(answer)
    if digest == record.last_answer_digest:
        logger.info(
            f"answer already posted, not posting again conversation={record.conversation.key}"
        )
        return False
    slack_post_parts(slack, record.conversation, answer)
    thread_map.record_posted_answer(record.conversation, digest, session_name=record.terminal_name)
    return True


def send_private_details(*, slack: SlackAdapter, record: ThreadRecord) -> None:
    # The session's Inbox thread and terminal, as an ephemeral message to the requester only.
    # Skipped when the record has no requester; a failure is logged, never raised.
    if not record.requester:
        logger.info(f"private details skipped, no requester conversation={record.conversation.key}")
        return
    try:
        slack.post_ephemeral(
            record.conversation.slack_channel(),
            record.conversation.id,
            record.requester,
            private_details_text(record),
        )
    except SlackPostError as e:
        logger.warning(
            f"private details could not be sent conversation={record.conversation.key}: {e}"
        )
        return
    logger.info(f"private details sent conversation={record.conversation.key}")
