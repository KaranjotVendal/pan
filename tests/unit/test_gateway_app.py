from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError

import pytest
from pydantic import SecretStr
from slack_sdk.errors import SlackApiError

from pan.errors import SlackPostError
from pan.gateway.app import BoltSlackAdapter
from pan.models import (
    InboxItem,
    PanConfig,
    Route,
    RouteKind,
    SlackCredentials,
    ThreadRecord,
    ThreadSource,
)
from pan.threadmap import FileThreadMap

_FIXED_NOW = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)


class FakeClock:
    def now(self) -> datetime:
        return _FIXED_NOW


class FakeWebClient:
    def __init__(self, timeline: list[tuple[str, Any]]) -> None:
        self._timeline = timeline
        self.reaction_error: SlackApiError | None = None
        self.post_error: SlackApiError | None = None
        self.post_response: dict[str, Any] = {"ok": True, "ts": "1718000000.000900"}
        self.permalink_error: Exception | None = None
        self.permalink_response: dict[str, Any] = {
            "ok": True,
            "permalink": "https://example.slack.com/archives/C000TEST/p1700000000000100",
        }

    def reactions_add(self, *, channel: str, timestamp: str, name: str) -> None:
        if self.reaction_error is not None:
            raise self.reaction_error
        self._timeline.append(("reaction", (channel, timestamp, name)))

    def chat_postMessage(
        self, *, channel: str, thread_ts: str, text: str, blocks: Any = None
    ) -> dict[str, Any]:
        if self.post_error is not None:
            raise self.post_error
        if blocks is None:
            self._timeline.append(("post", (channel, thread_ts, text)))
        else:
            self._timeline.append(("post_blocks", (channel, thread_ts, blocks, text)))
        return self.post_response

    def chat_update(self, *, channel: str, ts: str, blocks: Any, text: str) -> None:
        if self.post_error is not None:
            raise self.post_error
        self._timeline.append(("update", (channel, ts, blocks, text)))

    def chat_postEphemeral(self, *, channel: str, thread_ts: str, user: str, text: str) -> None:
        if self.post_error is not None:
            raise self.post_error
        self._timeline.append(("ephemeral", (channel, thread_ts, user, text)))

    def chat_getPermalink(self, *, channel: str, message_ts: str) -> dict[str, Any]:
        if self.permalink_error is not None:
            raise self.permalink_error
        return self.permalink_response


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
            "orchestrator": {"cwd": "/tmp/pan", "worktree_base": "/tmp/wt"},
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
    assert item.source is ThreadSource.SLACK
    assert item.route == Route(text="do the thing")

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


_TEST_CHANNEL = "C000TEST"
_OTHER_CHANNEL = "C000OTHER"
_TEST_THREAD_TS = "1700000000.000100"
_ALLOWED_USERS = {"U1": {"channels": ["*"]}}


def _thread_record(
    terminal_name: str,
    channel: str = _TEST_CHANNEL,
    source: ThreadSource = ThreadSource.SLACK,
) -> ThreadRecord:
    return ThreadRecord(
        thread_ts=_TEST_THREAD_TS,
        terminal_name=terminal_name,
        channel=channel,
        source=source,
        worktree_path=Path("/tmp/wt") / terminal_name,
        created_at=_FIXED_NOW,
        updated_at=_FIXED_NOW,
    )


def _thread_map(tmp_path: Path, thread_state: str) -> FileThreadMap:
    # unknown: no record; tied: one tied record; untied: the record survives `done`/`forget`
    # untied; retied: an untied record plus the thread's newer tied session; tied-elsewhere /
    # tied-x: the thread ts is tied in another channel / for an X post; unreadable: the
    # thread map file is not JSON.
    threads_path = tmp_path / "threads.json"
    thread_map = FileThreadMap(threads_path, FakeClock())
    if thread_state in ("tied", "untied", "retied"):
        thread_map.tie(_thread_record("pan-first"))
    if thread_state == "untied":
        thread_map.untie(_TEST_THREAD_TS)
    if thread_state == "retied":
        thread_map.tie(_thread_record("pan-second"))
    if thread_state == "tied-elsewhere":
        thread_map.tie(_thread_record("pan-first", channel=_OTHER_CHANNEL))
    if thread_state == "tied-x":
        thread_map.tie(_thread_record("pan-first", source=ThreadSource.X))
    if thread_state == "unreadable":
        threads_path.write_text("{not json")
    return thread_map


class ExplodingThreadMap:
    def get(self, thread_ts: str, *, session_name: str | None = None) -> ThreadRecord | None:
        raise AssertionError("the thread map must not be read")


def _forwarding_adapter(
    thread_map: Any,
) -> tuple[BoltSlackAdapter, FakeInbox]:
    timeline: list[tuple[str, Any]] = []
    inbox = FakeInbox(timeline)
    adapter = BoltSlackAdapter(
        _credentials(),
        _config(_ALLOWED_USERS),
        inbox,
        FakeClock(),
        web_client=FakeWebClient(timeline),
        thread_map=thread_map,
    )
    return adapter, inbox


def _reply(**fields: str) -> dict[str, Any]:
    reply = {
        "type": "message",
        "user": "U1",
        "channel": _TEST_CHANNEL,
        "ts": "1700000000.000200",
        "thread_ts": _TEST_THREAD_TS,
        "text": "a reply",
    }
    return reply | fields


def _mention(thread_ts: str = _TEST_THREAD_TS) -> dict[str, Any]:
    return {
        "type": "app_mention",
        "user": "U1",
        "channel": _TEST_CHANNEL,
        "ts": "1700000000.000150",
        "thread_ts": thread_ts,
        "text": "<@Bbot> do the thing",
    }


@pytest.mark.parametrize(
    "event, thread_state, expected",
    [
        (_reply(), "tied", True),
        (_reply(), "retied", True),
        (_reply(subtype="file_share"), "tied", True),
        (_reply(subtype="thread_broadcast"), "tied", True),
        (_reply(), "unknown", False),
        (_reply(), "untied", False),
        (_reply(), "tied-elsewhere", False),
        (_reply(), "tied-x", False),
        (_reply(), "unreadable", False),
    ],
    ids=[
        "untagged-reply-in-tied-thread",
        "untagged-reply-in-thread-retied-to-a-newer-session",
        "reply-with-attachment-in-tied-thread",
        "reply-also-sent-to-channel-in-tied-thread",
        "untagged-reply-in-unknown-thread",
        "untagged-reply-in-untied-thread",
        "thread-ts-tied-in-another-channel",
        "thread-ts-tied-for-an-x-post",
        "unreadable-thread-map",
    ],
)
def test_should_forward_message_by_thread_state(
    tmp_path: Path, event: dict[str, Any], thread_state: str, expected: bool
) -> None:
    adapter, _inbox = _forwarding_adapter(_thread_map(tmp_path, thread_state))

    assert adapter._should_forward_message(event, "Bbot") is expected


@pytest.mark.parametrize(
    "event",
    [
        _reply(text="hey <@Bbot> look"),
        _reply(bot_id="Bbot"),
        _reply(subtype="message_changed"),
        _reply(subtype="message_deleted"),
        {"type": "message", "user": "U1", "channel": _TEST_CHANNEL, "text": "top-level"},
        _reply(user="U_STRANGER"),
    ],
    ids=[
        "mention-left-to-app-mention",
        "bot-own-message",
        "edited-message-subtype",
        "deleted-message-subtype",
        "non-thread-message",
        "sender-not-allowed",
    ],
)
def test_cheap_checks_drop_before_reading_the_thread_map(event: dict[str, Any]) -> None:
    adapter, _inbox = _forwarding_adapter(ExplodingThreadMap())

    assert adapter._should_forward_message(event, "Bbot") is False


def test_adapter_without_a_thread_map_forwards_no_untagged_reply() -> None:
    adapter, _client, _inbox, _timeline = _make_adapter(_ALLOWED_USERS)

    assert adapter._should_forward_message(_reply(), "Bbot") is False


def test_reply_right_after_a_mention_is_forwarded_before_the_tie(tmp_path: Path) -> None:
    adapter, _inbox = _forwarding_adapter(_thread_map(tmp_path, "unknown"))

    adapter.handle_event(_mention(), "Ev1")

    assert adapter._should_forward_message(_reply(), "Bbot") is True
    assert adapter._should_forward_message(_reply(channel=_OTHER_CHANNEL), "Bbot") is False


def test_forwarded_reply_does_not_mark_its_thread_as_mentioned(tmp_path: Path) -> None:
    adapter, _inbox = _forwarding_adapter(_thread_map(tmp_path, "unknown"))

    adapter.handle_event(_reply(), "Ev1")

    assert adapter._should_forward_message(_reply(), "Bbot") is False


def test_mentioned_threads_keep_only_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pan.gateway.app._MENTIONED_THREADS_LIMIT", 2)
    adapter, _inbox = _forwarding_adapter(_thread_map(tmp_path, "unknown"))
    thread_stamps = ["1700000000.000301", "1700000000.000302", "1700000000.000303"]

    for idx, thread_ts in enumerate(thread_stamps):
        adapter.handle_event(_mention(thread_ts), f"Ev{idx}")

    forwarded = [
        adapter._should_forward_message(_reply(thread_ts=thread_ts), "Bbot")
        for thread_ts in thread_stamps
    ]
    assert forwarded == [False, True, True]


@pytest.mark.parametrize(
    "event",
    [
        _reply(subtype="file_share", text=""),
        _reply(subtype="file_share"),
        _reply(subtype="thread_broadcast"),
    ],
    ids=["attachment-without-text", "attachment-with-text", "also-sent-to-channel"],
)
def test_forwarded_reply_subtypes_reach_the_inbox(tmp_path: Path, event: dict[str, Any]) -> None:
    adapter, inbox = _forwarding_adapter(_thread_map(tmp_path, "tied"))

    adapter.handle_event(event, "Ev1")

    assert len(inbox.items) == 1
    assert inbox.items[0].thread_ts == _TEST_THREAD_TS
    assert inbox.items[0].is_thread_reply is True


@pytest.mark.parametrize(
    "thread_state, expected_level",
    [("unknown", logging.DEBUG), ("unreadable", logging.WARNING)],
    ids=["untied-thread", "unreadable-thread-map"],
)
def test_ignored_untagged_reply_logs_thread_and_channel_only(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    app_logger_capture: None,
    thread_state: str,
    expected_level: int,
) -> None:
    adapter, _inbox = _forwarding_adapter(_thread_map(tmp_path, thread_state))

    with caplog.at_level(logging.DEBUG, logger="pan.gateway.app"):
        adapter._should_forward_message(_reply(text="secret words"), "Bbot")

    assert expected_level in [record.levelno for record in caplog.records]
    assert f"thread={_TEST_THREAD_TS}" in caplog.text
    assert f"channel={_TEST_CHANNEL}" in caplog.text
    assert "secret words" not in caplog.text


@pytest.mark.parametrize(
    "text",
    ["<@B> help", "<@B> --help", "<@B> -h", "<@B> ?", "<@B> —help"],
    ids=["bare-help", "double-dash", "short-flag", "question-mark", "phone-em-dash"],
)
def test_help_mention_posts_help_and_skips_the_inbox(text: str) -> None:
    adapter, _client, inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "ts": "1718000000.000200",
        "text": text,
    }

    adapter.handle_event(event, "EvHelp")

    # The narrow, documented INV-1 exception: help is answered by the gateway itself
    # through the one egress (INV-4) — no :eyes:, no inbox append, no worker.
    kinds = [entry[0] for entry in timeline]
    assert kinds == ["post"]
    assert inbox.items == []
    posted_channel, posted_thread_ts, posted_text = timeline[0][1]
    assert posted_channel == "C1"
    assert posted_thread_ts == "1718000000.000200"
    assert posted_text.startswith("```")
    assert posted_text.rstrip().endswith("```")
    assert "Usage: pan" in posted_text


def test_help_mention_for_a_named_command_posts_that_command_help() -> None:
    adapter, _client, inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "ts": "1718000000.000200",
        "text": "<@B> help relay",
    }

    adapter.handle_event(event, "EvHelpRelay")

    assert inbox.items == []
    assert "Usage: pan relay" in timeline[0][1][2]


def test_help_for_a_backtick_command_name_keeps_the_fence_intact() -> None:
    adapter, _client, _inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "ts": "1718000000.000200",
        "text": "<@B> help ```",
    }

    adapter.handle_event(event, "EvHelpFence")

    # A user-supplied backtick run must not break out of the code block the help is posted in.
    posted_text = timeline[0][1][2]
    assert posted_text.count("```") == 2
    assert "unknown command" in posted_text


def test_non_help_mention_still_acks_and_appends() -> None:
    adapter, _client, inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "ts": "1718000000.000200",
        "text": "<@B> add a --help flag to the tool and fix the login bug",
    }

    adapter.handle_event(event, "EvTask")

    # Guards the INV-1 scope: help is the ONLY thing the gateway answers itself.
    assert [entry[0] for entry in timeline] == ["reaction", "append"]
    assert len(inbox.items) == 1


def test_unauthorized_help_mention_is_still_dropped() -> None:
    adapter, _client, inbox, timeline = _make_adapter({"U1": {"channels": ["C1"]}})
    event = {
        "type": "app_mention",
        "user": "U_stranger",
        "channel": "C1",
        "ts": "1718000000.000200",
        "text": "<@B> help",
    }

    adapter.handle_event(event, "EvHelpDenied")

    # Auth precedes help detection: no post, no append, no reaction.
    assert timeline == []
    assert inbox.items == []


_BLOCKS = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Working*"}}]


@pytest.mark.parametrize(
    "call",
    [
        lambda adapter: adapter.add_reaction("C1", "1718000000.000200", "eyes"),
        lambda adapter: adapter.post_message("C1", "1718000000.000200", "text"),
        lambda adapter: adapter.post_blocks("C1", "1718000000.000200", _BLOCKS, "Working"),
        lambda adapter: adapter.update_message("C1", "1718000000.000900", _BLOCKS, "Working"),
        lambda adapter: adapter.post_ephemeral("C1", "1718000000.000200", "U1", "details"),
    ],
    ids=["add_reaction", "post_message", "post_blocks", "update_message", "post_ephemeral"],
)
def test_client_failure_becomes_slack_post_error(call: Any) -> None:
    adapter, client, _inbox, _timeline = _make_adapter({"U1": {"channels": ["*"]}})
    api_error = SlackApiError("boom", {"ok": False, "error": "boom"})
    client.reaction_error = api_error
    client.post_error = api_error

    with pytest.raises(SlackPostError):
        call(adapter)


def test_post_blocks_posts_blocks_with_a_fallback_and_returns_the_ts() -> None:
    adapter, _client, _inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})

    message_ts = adapter.post_blocks("C1", "1718000000.000200", _BLOCKS, "Working")

    assert message_ts == "1718000000.000900"
    assert timeline == [("post_blocks", ("C1", "1718000000.000200", _BLOCKS, "Working"))]


def test_post_blocks_without_a_ts_in_the_response_is_an_error() -> None:
    adapter, client, _inbox, _timeline = _make_adapter({"U1": {"channels": ["*"]}})
    client.post_response = {"ok": True}

    with pytest.raises(SlackPostError):
        adapter.post_blocks("C1", "1718000000.000200", _BLOCKS, "Working")


def test_update_message_and_post_ephemeral_delegate_to_the_client() -> None:
    adapter, _client, _inbox, timeline = _make_adapter({"U1": {"channels": ["*"]}})

    adapter.update_message("C1", "1718000000.000900", _BLOCKS, "Finished")
    adapter.post_ephemeral("C1", "1718000000.000200", "U1", "details")

    assert timeline == [
        ("update", ("C1", "1718000000.000900", _BLOCKS, "Finished")),
        ("ephemeral", ("C1", "1718000000.000200", "U1", "details")),
    ]


@pytest.mark.parametrize(
    "text, expected_route",
    [
        ("<@B> new: fix the login page", Route(kind=RouteKind.NEW, text="fix the login page")),
        ("<@B> to pan-7: add tests", Route(kind=RouteKind.TO, session="pan-7", text="add tests")),
        ("<@B> check", Route(kind=RouteKind.CHECK)),
    ],
)
def test_gateway_writes_the_parsed_route_on_the_item(text: str, expected_route: Route) -> None:
    adapter, _client, inbox, _timeline = _make_adapter({"U1": {"channels": ["*"]}})
    event = {"user": "U1", "channel": "C1", "ts": "1718000000.000200", "text": text}

    adapter.handle_event(event, "Ev1")

    (item,) = inbox.items
    assert item.route == expected_route
    assert item.raw_text == text


def test_permalink_returns_the_message_link() -> None:
    adapter, _client, _inbox, _timeline = _make_adapter({})

    link = adapter.permalink("C000TEST", "1700000000.000100")

    assert link == "https://example.slack.com/archives/C000TEST/p1700000000000100"


@pytest.mark.parametrize(
    "error, response",
    [
        (SlackApiError("boom", {"ok": False, "error": "channel_not_found"}), None),
        (URLError("connection reset"), None),
        (TimeoutError("timed out"), None),
        (None, {"ok": True}),
    ],
    ids=["api-error", "transport-error", "timeout", "no-link"],
)
def test_permalink_failure_is_logged_not_raised(
    error: Exception | None,
    response: dict[str, Any] | None,
    app_logger_capture: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, client, _inbox, _timeline = _make_adapter({})
    client.permalink_error = error
    if response is not None:
        client.permalink_response = response

    link = adapter.permalink("C000TEST", "1700000000.000100")

    assert link is None
    assert "permalink" in caplog.text
