from __future__ import annotations

from typing import assert_never

from pan.core.errors import SlackPostError
from pan.core.events import (
    AnswerReady,
    Event,
    PrivateDetails,
    ScreenCaptured,
    SessionsListed,
    StatusChanged,
)
from pan.core.models import Conversation, SessionSummary
from pan.core.seams import SlackAdapter
from pan.frontend.gateway.slack_post import slack_post, slack_post_parts
from pan.frontend.slack_render import (
    LineEvent,
    line_text,
    private_details_text,
    render_sessions_for_slack,
    screen_text,
    status_blocks,
    status_text,
)
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)


def thread_permalinks(slack: SlackAdapter, sessions: list[SessionSummary]) -> dict[str, str]:
    """Each session's thread link by thread ts, asked of Slack once per thread; a thread Slack
    gives no link for is left out."""
    permalinks: dict[str, str] = {}
    fetched_threads: set[str] = set()
    for summary in sessions:
        if not summary.thread_ts or not summary.thread_channel:
            continue
        if summary.thread_ts in fetched_threads:
            continue
        fetched_threads.add(summary.thread_ts)
        link = slack.permalink(summary.thread_channel, summary.thread_ts)
        if link:
            permalinks[summary.thread_ts] = link
    return permalinks


class SlackReporter:
    # The Reporter (core.seams) for a Slack thread: each event rendered by slack_render into
    # the text and blocks pan posts, and sent through the SlackAdapter. Plain lines and answers
    # go through slack_post (INV-4); the status message is posted as blocks or edited in place;
    # private details and the sessions list are ephemeral, for the requester alone. A failed
    # post raises SlackPostError, as does a conversation with no channel.
    def __init__(self, slack: SlackAdapter) -> None:
        self._slack = slack

    def report(self, conversation: Conversation, event: Event) -> str | None:
        if isinstance(event, StatusChanged):
            return self._report_status(conversation, event)
        if isinstance(event, LineEvent):
            slack_post(self._slack, conversation, line_text(event))
        elif isinstance(event, AnswerReady):
            slack_post_parts(self._slack, conversation, event.text)
        elif isinstance(event, ScreenCaptured):
            slack_post_parts(self._slack, conversation, screen_text(event.text))
        elif isinstance(event, PrivateDetails):
            self._slack.post_ephemeral(
                conversation.slack_channel(),
                conversation.id,
                event.requester,
                private_details_text(event),
            )
        elif isinstance(event, SessionsListed):
            self._report_sessions(conversation, event)
        else:
            assert_never(event)
        return None

    def _report_status(self, conversation: Conversation, status: StatusChanged) -> str | None:
        # Edited in place when the event names a posted message; when it names none, or the
        # edit fails (deleted, too old), posted anew and its ts returned. No message text is
        # logged (INV-9).
        blocks = status_blocks(
            status.state,
            session_name=status.session_name,
            label=status.label,
            started_at=status.task_started_at,
            now=status.updated_at,
            replies_in_thread=status.replies_in_thread,
            detail=status.detail,
        )
        fallback_text = status_text(status.state, status.label)
        if status.status_message_id:
            try:
                self._slack.update_message(
                    conversation.slack_channel(), status.status_message_id, blocks, fallback_text
                )
            except SlackPostError as e:
                logger.warning(
                    f"status message could not be edited, posting a new one "
                    f"conversation={conversation.key}: {e}"
                )
            else:
                return None
        return self._slack.post_blocks(
            conversation.slack_channel(), conversation.id, blocks, fallback_text
        )

    def _report_sessions(self, conversation: Conversation, listed: SessionsListed) -> None:
        text = render_sessions_for_slack(
            listed.sessions,
            thread_permalinks(self._slack, listed.sessions),
            any_running=listed.any_running,
        )
        self._slack.post_ephemeral(
            conversation.slack_channel(), conversation.id, listed.requester, text
        )
