from __future__ import annotations

from typing import Any

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from pan.config import CODE_FENCE
from pan.errors import SlackPostError, UnauthorizedSenderError
from pan.gateway.auth import auth_check
from pan.gateway.slack_post import slack_post
from pan.help import parse_help_request, render_cli_help
from pan.logging import initialise_logger
from pan.models import InboxItem, PanConfig, SlackCredentials
from pan.seams import Clock, InboxStore

logger = initialise_logger(__name__)

_EYES = "eyes"


class BoltSlackAdapter:
    def __init__(
        self,
        credentials: SlackCredentials,
        config: PanConfig,
        inbox_store: InboxStore,
        clock: Clock,
        *,
        web_client: WebClient | None = None,
    ) -> None:
        self._config = config
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

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        try:
            self._client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
        except SlackApiError as error:
            raise SlackPostError(f"failed to post message to thread {thread_ts}") from error

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
        )
        self._inbox.append(item)
        logger.info(f"gateway appended event id={event_id} channel={channel}")

    def _should_forward_message(self, event: dict[str, Any], bot_user_id: str | None) -> bool:
        # The gateway stays dumb but must not double-append: a message event is
        # forwarded only when it is a human thread reply that does NOT mention the
        # bot. A bot's own post (bot_id) or an edit/system message (subtype) is
        # ignored; a top-level (non-thread) message is ignored; and a message that
        # mentions the bot is left to the app_mention handler — Slack delivers an
        # in-thread mention as BOTH an app_mention and a message with distinct event
        # ids, so forwarding both would append the same request twice.
        mentions_bot = bot_user_id is not None and f"<@{bot_user_id}>" in event.get("text", "")
        return (
            event.get("bot_id") is None
            and event.get("subtype") is None
            and event.get("thread_ts") is not None
            and not mentions_bot
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
