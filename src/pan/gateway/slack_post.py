from __future__ import annotations

from pan.logging import initialise_logger
from pan.seams import SlackAdapter
from pan.slack_format import to_slack_mrkdwn

logger = initialise_logger(__name__)


def slack_post(adapter: SlackAdapter, channel: str, thread_ts: str, text: str) -> None:
    # The one function every worker->thread post routes through (INV-4). The message
    # body is never logged — only its length — so no payload leaks (INV-9).
    logger.info(f"slack-post thread={thread_ts} len={len(text)}")
    # Normalize GFM to Slack mrkdwn at the single egress, so every source (orchestrator
    # summaries, worker hook output, acks, relay/read replies) renders correctly (INV-4).
    adapter.post_message(channel, thread_ts, to_slack_mrkdwn(text))
