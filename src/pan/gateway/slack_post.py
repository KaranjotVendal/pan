from __future__ import annotations

from pan.logging import initialise_logger
from pan.seams import SlackAdapter

logger = initialise_logger(__name__)


def slack_post(adapter: SlackAdapter, channel: str, thread_ts: str, text: str) -> None:
    # The one function every worker->thread post routes through (INV-4). The message
    # body is never logged — only its length — so no payload leaks (INV-9).
    logger.info(f"slack-post thread={thread_ts} len={len(text)}")
    adapter.post_message(channel, thread_ts, text)
