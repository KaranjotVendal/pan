from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from pydantic import ValidationError
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from pan.config import CODE_FENCE
from pan.errors import SlackPostError, UnauthorizedSenderError
from pan.gateway.auth import auth_check, is_authorized
from pan.gateway.slack_post import slack_post
from pan.help import parse_help_request, render_cli_help
from pan.logging import initialise_logger
from pan.models import InboxItem, PanConfig, SlackCredentials, ThreadSource
from pan.seams import Clock, InboxStore, ThreadMap
from pan.slack_format import redact_block_paths, redact_paths

logger = initialise_logger(__name__)

_EYES = "eyes"
# Message subtypes that are still a person's reply: one with an attachment, and one also sent
# to the channel. Every other subtype (edits, deletes, joins, ...) is not a new message.
_FORWARDED_SUBTYPES = frozenset({"file_share", "thread_broadcast"})
# How many threads the gateway remembers accepting an @pan mention in (oldest dropped first).
_MENTIONED_THREADS_LIMIT = 500


class BoltSlackAdapter:
    def __init__(
        self,
        credentials: SlackCredentials,
        config: PanConfig,
        inbox_store: InboxStore,
        clock: Clock,
        *,
        web_client: WebClient | None = None,
        thread_map: ThreadMap | None = None,
    ) -> None:
        self._config = config
        # Only the gateway's message handler reads the thread map, to tell a follow-up in a
        # thread pan is already working in from an unrelated reply. An adapter built
        # without one (the posting-only commands) forwards no untagged reply.
        self._thread_map = thread_map
        # Threads (channel, thread_ts) this gateway accepted an @pan mention in. A follow-up
        # sent right after the mention, before the orchestrator ties the thread (tens of
        # seconds), is forwarded through this set. In memory only: after a restart the set
        # is empty and the tie alone decides. Bolt runs handlers on worker threads, so a lock
        # guards it.
        self._mentioned_threads: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._mentioned_threads_lock = threading.Lock()
        self._inbox = inbox_store
        self._clock = clock
        self._app_token = credentials.app_token
        # The single Slack-client construction point — the only place outside
        # credentials.py that reads a token's secret value (BR-3).
        self._client = (
            web_client
            if web_client is not None
            else WebClient(token=credentials.bot_token.get_secret_value())
        )

    def add_reaction(self, channel: str, ts: str, name: str) -> None:
        try:
            self._client.reactions_add(channel=channel, timestamp=ts, name=name)
        except SlackApiError as error:
            raise SlackPostError(f"failed to add reaction in channel {channel}") from error

    # Every public post and edit has local paths cut to their last part (redact_paths), so a
    # session's answer never shows the machine's folders in a channel. post_ephemeral is left
    # as is: only the requester sees it, and its details deliberately name the worktree.
    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        try:
            self._client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=redact_paths(text)
            )
        except SlackApiError as error:
            raise SlackPostError(f"failed to post message to thread {thread_ts}") from error

    def post_blocks(
        self, channel: str, thread_ts: str, blocks: list[dict[str, Any]], text: str
    ) -> str:
        try:
            response = self._client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=redact_block_paths(blocks),
                text=redact_paths(text),
            )
        except SlackApiError as error:
            raise SlackPostError(f"failed to post blocks to thread {thread_ts}") from error
        message_ts = response.get("ts")
        if not isinstance(message_ts, str) or not message_ts:
            raise SlackPostError(f"Slack returned no message ts for thread {thread_ts}")
        return message_ts

    def update_message(
        self, channel: str, ts: str, blocks: list[dict[str, Any]], text: str
    ) -> None:
        try:
            self._client.chat_update(
                channel=channel, ts=ts, blocks=redact_block_paths(blocks), text=redact_paths(text)
            )
        except SlackApiError as error:
            raise SlackPostError(f"failed to update message {ts}") from error

    def post_ephemeral(self, channel: str, thread_ts: str, user: str, text: str) -> None:
        try:
            self._client.chat_postEphemeral(
                channel=channel, thread_ts=thread_ts, user=user, text=text
            )
        except SlackApiError as error:
            raise SlackPostError(f"failed to post ephemeral to thread {thread_ts}") from error

    def permalink(self, channel: str, ts: str) -> str | None:
        # Best effort: a message without a link is still listed, so a failure is only logged.
        # Any failure, an API error or a transport one (timeout, connection reset), only loses
        # the link, never the reply it would go in.
        try:
            response = self._client.chat_getPermalink(channel=channel, message_ts=ts)
        except Exception as e:
            logger.warning(
                f"permalink unavailable channel={channel} ts={ts}: {type(e).__name__}: {e}"
            )
            return None
        link = response.get("permalink")
        if not isinstance(link, str) or not link:
            logger.warning(f"Slack returned no permalink channel={channel} ts={ts}")
            return None
        return link

    def handle_event(self, event: dict[str, Any], event_id: str) -> None:
        slack_user = event.get("user", "")
        channel = event.get("channel", "")

        try:
            auth_check(slack_user, channel, self._config.users)
        except UnauthorizedSenderError:
            # Dropped: a denied sender never reaches the inbox (INV-1). No :eyes:.
            logger.warning(f"dropped unauthorized event id={event_id} channel={channel}")
            return

        message_ts = event.get("ts", "")
        thread_ts_value = event.get("thread_ts")
        # A top-level app-mention has no thread_ts, so the thread is rooted at the
        # message ts; a reply carries the parent thread_ts.
        thread_ts = thread_ts_value or message_ts

        raw_text = event.get("text", "")
        help_request = parse_help_request(raw_text)
        if help_request is not None:
            # The SINGLE, deliberately narrow exception to INV-1 ("the gateway is dumb"): help
            # is answered here, before the inbox/watcher/orchestrator, because it is read-only,
            # spawns nothing and mutates no state — and instant help is the entire point. Do NOT
            # widen this to other event types; anything beyond a pure read belongs in the
            # orchestrator. No :eyes: either: the reply itself IS the acknowledgement.
            fenced_help = f"{CODE_FENCE}\n{render_cli_help(help_request.command)}\n{CODE_FENCE}"
            # Posted through the one egress (INV-4), so it runs through to_slack_mrkdwn like
            # every other message (a fence passes through verbatim, keeping the columns).
            slack_post(self, channel, thread_ts, fenced_help)
            # Value-free (INV-9): the requested name is Slack-supplied text, so only whether
            # one was given is logged here — help.py logs the sanitized name when it misses.
            named = help_request.command is not None
            logger.info(f"gateway answered help id={event_id} named={named}")
            return

        # Fast :eyes: ack, ordered BEFORE the append so the sender sees acknowledgement
        # immediately and before any downstream work (INV-1).
        self.add_reaction(channel, message_ts, _EYES)

        item = InboxItem(
            id=event_id,
            slack_user=slack_user,
            channel=channel,
            thread_ts=thread_ts,
            is_thread_reply=thread_ts_value is not None,
            raw_text=raw_text,
            received_at=self._clock.now(),
            source=ThreadSource.SLACK,
        )
        self._inbox.append(item)
        logger.info(f"gateway appended event id={event_id} channel={channel}")
        if event.get("type") == "app_mention":
            self._remember_mentioned_thread(channel, thread_ts)

    def _remember_mentioned_thread(self, channel: str, thread_ts: str) -> None:
        with self._mentioned_threads_lock:
            self._mentioned_threads[(channel, thread_ts)] = None
            self._mentioned_threads.move_to_end((channel, thread_ts))
            while len(self._mentioned_threads) > _MENTIONED_THREADS_LIMIT:
                self._mentioned_threads.popitem(last=False)

    def _was_mentioned_in(self, channel: str, thread_ts: str) -> bool:
        with self._mentioned_threads_lock:
            return (channel, thread_ts) in self._mentioned_threads

    def _should_forward_message(self, event: dict[str, Any], bot_user_id: str | None) -> bool:
        # The gateway stays dumb but must not double-append, and must not pick up
        # unrelated chatter: a message event is forwarded only when it is an allowed
        # person's thread reply that does NOT mention the bot, in a thread pan is working
        # in — tied in the thread map, or one this gateway just accepted an @pan mention in
        # and the orchestrator has not tied yet. A bot's own post (bot_id) and an
        # edit/system message (any subtype but file_share and thread_broadcast) are ignored;
        # a top-level (non-thread) message is ignored; a sender the allowlist refuses is
        # ignored; a reply in any other thread (unknown, or untied by done, forget, a move
        # to another session or a failed start) is ignored, so starting work there still
        # needs an @pan mention; and a message that mentions the bot is left to the
        # app_mention handler — Slack delivers an in-thread mention as BOTH an app_mention
        # and a message with distinct event ids, so forwarding both would append the same
        # request twice. The cheap checks run first; the thread map is read last.
        mentions_bot = bot_user_id is not None and f"<@{bot_user_id}>" in event.get("text", "")
        subtype = event.get("subtype")
        thread_ts = event.get("thread_ts")
        channel = event.get("channel", "")
        if (
            event.get("bot_id") is not None
            or (subtype is not None and subtype not in _FORWARDED_SUBTYPES)
            or thread_ts is None
            or mentions_bot
            or not is_authorized(event.get("user", ""), channel, self._config.users)
        ):
            return False
        if self._was_mentioned_in(channel, thread_ts) or self._is_tied_thread(channel, thread_ts):
            return True
        logger.debug(
            f"gateway ignored untagged reply in untied thread={thread_ts} channel={channel}"
        )
        return False

    def _is_tied_thread(self, channel: str, thread_ts: str) -> bool:
        # A thread lookup returns the thread's tied record when it has one, so the thread is
        # tied exactly when that record is, for this Slack channel.
        if self._thread_map is None:
            return False
        try:
            record = self._thread_map.get(thread_ts)
        except (OSError, ValueError, ValidationError) as e:
            # An unreadable thread map must not take the gateway down, and a reply we cannot
            # place is safer dropped than forwarded: forwarding could start a session in a
            # stranger's thread, while a dropped follow-up can be resent with @pan.
            logger.warning(
                f"gateway could not read the thread map thread={thread_ts} "
                f"channel={channel}: {type(e).__name__}"
            )
            return False
        return (
            record is not None
            and record.tied
            and record.channel == channel
            and record.source == ThreadSource.SLACK
        )

    def start(self) -> None:  # pragma: no cover - live Socket Mode run, not unit-tested
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        app = App(client=self._client)
        bot_user_id = self._client.auth_test().get("user_id")

        @app.event("app_mention")
        def _on_app_mention(event: dict[str, Any], body: dict[str, Any]) -> None:
            self.handle_event(event, body.get("event_id", ""))

        @app.event("message")
        def _on_message(event: dict[str, Any], body: dict[str, Any]) -> None:
            if self._should_forward_message(event, bot_user_id):
                self.handle_event(event, body.get("event_id", ""))

        handler = SocketModeHandler(app, self._app_token.get_secret_value())
        logger.info("gateway starting socket mode")
        handler.start()
