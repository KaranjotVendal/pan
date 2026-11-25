from __future__ import annotations

from typing import Any

from pan.core.errors import SlackPostError


class StatusPost:
    def __init__(self, channel: str, thread_ts: str, blocks: list[dict[str, Any]], text: str):
        self.channel = channel
        self.thread_ts = thread_ts
        self.blocks = blocks
        self.text = text


class RecordingSlack:
    # Records every kind of Slack write. `posts` holds plain messages as (channel, thread, text);
    # status messages posted as blocks are in `status_posts`, edits in `status_updates`, and
    # ephemeral messages in `ephemerals` as (channel, thread, user, text). `permalinks` holds
    # the links `permalink` returns, by message ts. Set `update_error`
    # or `post_blocks_error` to make that call fail.
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []
        self.status_posts: list[StatusPost] = []
        self.status_updates: list[tuple[str, str, list[dict[str, Any]], str]] = []
        self.ephemerals: list[tuple[str, str, str, str]] = []
        self.update_error: SlackPostError | None = None
        self.post_blocks_error: SlackPostError | None = None
        self.ephemeral_error: SlackPostError | None = None
        # Message links by message ts; a ts not listed has no link.
        self.permalinks: dict[str, str] = {}
        self._next_ts = 0

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        self.posts.append((channel, thread_ts, text))

    def post_blocks(
        self, channel: str, thread_ts: str, blocks: list[dict[str, Any]], text: str
    ) -> str:
        if self.post_blocks_error is not None:
            raise self.post_blocks_error
        self.status_posts.append(StatusPost(channel, thread_ts, blocks, text))
        self._next_ts += 1
        return f"status-{self._next_ts}"

    def update_message(
        self, channel: str, ts: str, blocks: list[dict[str, Any]], text: str
    ) -> None:
        if self.update_error is not None:
            raise self.update_error
        self.status_updates.append((channel, ts, blocks, text))

    def post_ephemeral(self, channel: str, thread_ts: str, user: str, text: str) -> None:
        if self.ephemeral_error is not None:
            raise self.ephemeral_error
        self.ephemerals.append((channel, thread_ts, user, text))

    def permalink(self, channel: str, ts: str) -> str | None:
        return self.permalinks.get(ts)

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def texts(self) -> list[str]:
        return [text for _channel, _thread, text in self.posts]

    def status_texts(self) -> list[str]:
        # The fallback text of every status post and edit, in the order they happened per kind.
        return [post.text for post in self.status_posts] + [
            update[3] for update in self.status_updates
        ]
