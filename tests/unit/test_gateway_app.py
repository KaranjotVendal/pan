from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import SecretStr
from slack_sdk.errors import SlackApiError

from pan.errors import SlackPostError
from pan.gateway.app import BoltSlackAdapter
from pan.models import InboxItem, PanConfig, SlackCredentials

_FIXED_NOW = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return _FIXED_NOW


class FakeWebClient:
    def __init__(self, timeline: list[tuple[str, Any]]) -> None:
        self._timeline = timeline
        self.reaction_error: SlackApiError | None = None
        self.post_error: SlackApiError | None = None

    def reactions_add(self, *, channel: str, timestamp: str, name: str) -> None:
        if self.reaction_error is not None:
            raise self.reaction_error
        self._timeline.append(("reaction", (channel, timestamp, name)))

    def chat_postMessage(self, *, channel: str, thread_ts: str, text: str) -> None:
        if self.post_error is not None:
            raise self.post_error
        self._timeline.append(("post", (channel, thread_ts, text)))


class FakeInbox:
    def __init__(self, timeline: list[tuple[str, Any]]) -> None:
        self.items: list[InboxItem] = []
        self._timeline = timeline

    def append(self, item: InboxItem) -> None:
        self.items.append(item)
        self._timeline.append(("append", item))

    def drain(self) -> list[InboxItem]:
        return []


def _config(users: dict[str, dict]) -> PanConfig:
    return PanConfig.model_validate(
        {
            "orchestrator": {"pane_id": "%3", "worktree_base": "/tmp/wt"},
            "defaults": {},
            "users": users,
            "paths": {
                "inbox": "/tmp/inbox",
                "threads": "/tmp/threads.json",
                "logs": "/tmp/logs",
                "credentials": "/tmp/creds.json",
            },
        }
    )


def _credentials() -> SlackCredentials:
    return SlackCredentials(
        bot_token=SecretStr("xoxb-fake-000"), app_token=SecretStr("xapp-fake-111")
    )


def _make_adapter(
    users: dict[str, dict],
) -> tuple[BoltSlackAdapter, FakeWebClient, FakeInbox, list[tuple[str, Any]]]:
    timeline: list[tuple[str, Any]] = []
    client = FakeWebClient(timeline)
    inbox = FakeInbox(timeline)
    adapter = BoltSlackAdapter(
        _credentials(), _config(users), inbox, FakeClock(), web_client=client
    )
    return adapter, client, inbox, timeline


@pytest.fixture
def app_logger_capture(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    logger = logging.getLogger("pan.gateway.app")
    logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        logger.removeHandler(caplog.handler)


def test_allowed_app_mention_acks_then_appends() -> None:
    adapter, _client, inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "ts": "1718000000.000200",
        "text": "<@B> do the thing",
    }

    adapter.handle_event(event, "Ev123")

    assert len(inbox.items) == 1
    item = inbox.items[0]
    assert item.id == "Ev123"
    assert item.slack_user == "U1"
    assert item.channel == "C1"
    assert item.thread_ts == "1718000000.000200"
    assert item.is_thread_reply is False
    assert item.raw_text == "<@B> do the thing"
    assert item.received_at == _FIXED_NOW

    kinds = [entry[0] for entry in timeline]
    assert kinds == ["reaction", "append"]  # :eyes: before append (INV-1)
    assert timeline[0][1] == ("C1", "1718000000.000200", "eyes")


def test_thread_reply_sets_thread_ts_and_flag() -> None:
    adapter, _client, inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})
    event = {
        "type": "message",
        "user": "U1",
        "channel": "C1",
        "ts": "1718000000.000500",
        "thread_ts": "1718000000.000200",
        "text": "and also this",
    }

    adapter.handle_event(event, "Ev456")

    item = inbox.items[0]
    assert item.thread_ts == "1718000000.000200"
    assert item.is_thread_reply is True
    # The :eyes: ack lands on the reply's own ts, not the thread root.
    assert timeline[0] == ("reaction", ("C1", "1718000000.000500", "eyes"))


@pytest.mark.usefixtures("app_logger_capture")
def test_denied_sender_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    adapter, _client, inbox, timeline = _make_adapter({"U1": {"channels": ["C1"]}})
    event = {
        "type": "app_mention",
        "user": "U_stranger",
        "channel": "C1",
        "ts": "1718000000.000200",
        "text": "let me in",
    }

    with caplog.at_level(logging.WARNING, logger="pan.gateway.app"):
        adapter.handle_event(event, "Ev789")

    assert inbox.items == []
    assert timeline == []  # no reaction, no append
    assert any(record.levelno == logging.WARNING for record in caplog.records)


def test_post_message_delegates_to_client() -> None:
    adapter, _client, _inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})

    adapter.post_message("C1", "1718000000.000200", "hello thread")

    assert timeline == [("post", ("C1", "1718000000.000200", "hello thread"))]


@pytest.mark.parametrize(
    "event, expected",
    [
        ({"thread_ts": "1.2", "text": "just a reply"}, True),
        ({"thread_ts": "1.2", "text": "hey <@Bbot> look", "user": "U1"}, False),
        ({"thread_ts": "1.2", "bot_id": "Bbot", "text": "x"}, False),
        ({"thread_ts": "1.2", "subtype": "message_changed", "text": "x"}, False),
        ({"text": "top-level, no thread"}, False),
    ],
    ids=[
        "human-thread-reply",
        "mention-left-to-app-mention",
        "bot-own-message",
        "edited-message-subtype",
        "non-thread-message",
    ],
)
def test_should_forward_message(event: dict[str, Any], expected: bool) -> None:
    adapter, _client, _inbox, _timeline = _make_adapter({"U1": {"channels": ["*"]}})

    assert adapter._should_forward_message(event, "Bbot") is expected


@pytest.mark.parametrize("method_name", ["add_reaction", "post_message"])
def test_client_failure_becomes_slack_post_error(method_name: str) -> None:
    adapter, client, _inbox, _timeline = _make_adapter({"U1": {"channels": ["*"]}})
    api_error = SlackApiError("boom", {"ok": False, "error": "boom"})
    client.reaction_error = api_error
    client.post_error = api_error

    with pytest.raises(SlackPostError):
        if method_name == "add_reaction":
            adapter.add_reaction("C1", "1718000000.000200", "eyes")
        else:
            adapter.post_message("C1", "1718000000.000200", "text")
