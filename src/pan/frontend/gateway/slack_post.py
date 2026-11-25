from __future__ import annotations

from pan.core.models import Conversation
from pan.core.seams import SlackAdapter
from pan.frontend.slack_format import split_for_slack, to_slack_mrkdwn
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)


def slack_post(adapter: SlackAdapter, conversation: Conversation, text: str) -> None:
    # The one function every worker->thread post routes through (INV-4). The message
    # body is never logged — only its length — so no payload leaks (INV-9). A conversation
    # with no channel raises SlackPostError.
    channel = conversation.slack_channel()
    logger.info(f"slack-post thread={conversation.id} len={len(text)}")
    # Normalize GFM to Slack mrkdwn at the single egress, so every source (orchestrator
    # summaries, worker hook output, acks, relay/read replies) renders correctly (INV-4).
    adapter.post_message(channel, conversation.id, to_slack_mrkdwn(text))


def slack_post_parts(adapter: SlackAdapter, conversation: Conversation, text: str) -> None:
    # A long message as several posts in order, each labelled "(k/n)" when there is more
    # than one. Every part still goes through slack_post (INV-4).
    parts = split_for_slack(text)
    if len(parts) == 1:
        slack_post(adapter, conversation, text)
        return
    for index, part in enumerate(parts, start=1):
        slack_post(adapter, conversation, f"({index}/{len(parts)})\n{part}")
