from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from pan.errors import SlackPostError
from pan.gateway.slack_post import slack_post


class FakeSlackAdapter:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._fail = fail

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        if self._fail:
            raise SlackPostError("boom")
        self.calls.append((channel, thread_ts, text))

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def slack_post_logger_capture(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    logger = logging.getLogger("pan.gateway.slack_post")
    logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        logger.removeHandler(caplog.handler)


def test_slack_post_delegates_to_post_message() -> None:
    adapter = FakeSlackAdapter()

    slack_post(adapter, "C1", "1718000000.000200", "work is done")

    assert adapter.calls == [("C1", "1718000000.000200", "work is done")]


def test_slack_post_normalizes_to_mrkdwn_before_post_message() -> None:
    # The single egress runs text through the GFM->mrkdwn converter (INV-4), so the
    # adapter receives Slack mrkdwn, not the raw GFM body.
    adapter = FakeSlackAdapter()

    slack_post(adapter, "C1", "1718000000.000200", "See **the report**")

    assert adapter.calls == [("C1", "1718000000.000200", "See *the report*")]


def test_adapter_failure_surfaces_as_slack_post_error() -> None:
    adapter = FakeSlackAdapter(fail=True)

    with pytest.raises(SlackPostError):
        slack_post(adapter, "C1", "1718000000.000200", "work is done")


@pytest.mark.usefixtures("slack_post_logger_capture")
def test_log_is_value_free(caplog: pytest.LogCaptureFixture) -> None:
    adapter = FakeSlackAdapter()
    secret_body = "super secret result body"

    with caplog.at_level(logging.INFO, logger="pan.gateway.slack_post"):
        slack_post(adapter, "C1", "1718000000.000200", secret_body)

    assert caplog.records, "expected a log record"
    for record in caplog.records:
        message = record.getMessage()
        assert secret_body not in message
        assert "thread=1718000000.000200" in message
        assert f"len={len(secret_body)}" in message
