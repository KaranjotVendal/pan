from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from reporter_fakes import RecordingReporter
from terminal_host_fakes import make_terminal

from pan.backend.status_message import (
    answer_digest,
    check_state,
    post_answer,
    publish_status,
    send_private_details,
)
from pan.core.errors import SlackPostError
from pan.core.events import AnswerReady, Event, PrivateDetails, StatusChanged
from pan.core.models import (
    AgentStatus,
    Conversation,
    ReplyMode,
    StatusState,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)

_NOW = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
_THREAD = "1700000000.000100"
_CONVERSATION = Conversation.slack(_THREAD, "C0TEST")


class FixedClock:
    def now(self) -> datetime:
        return _NOW


class RecordingThreadMap:
    def __init__(self) -> None:
        self.status_messages: list[tuple[str, str, str | None]] = []
        self.posted_answers: list[tuple[str, str, str | None]] = []

    def record_status_message(
        self, conversation: Conversation, message_ts: str, *, session_name: str | None = None
    ) -> None:
        self.status_messages.append((conversation.id, message_ts, session_name))

    def record_posted_answer(
        self, conversation: Conversation, answer_digest: str, *, session_name: str | None = None
    ) -> None:
        self.posted_answers.append((conversation.id, answer_digest, session_name))


def _record(**updates: object) -> ThreadRecord:
    record = ThreadRecord(
        conversation=_CONVERSATION,
        terminal_name="pan-login",
        terminal_address="t7",
        worktree_path=Path("/tmp/wt/pan-login"),
        created_at=_NOW,
        updated_at=_NOW,
        requester="U0TEST",
        status_label="fix the login page",
        task_started_at=_NOW,
    )
    return record.model_copy(update=updates)


def _publish(
    reporter: RecordingReporter, thread_map: RecordingThreadMap, record: ThreadRecord
) -> ThreadRecord:
    return publish_status(
        reporter=reporter,
        thread_map=thread_map,  # type: ignore[arg-type]
        record=record,
        state=StatusState.WAITING,
        clock=FixedClock(),
        detail="Allow edit?",
    )


@pytest.mark.parametrize(
    "updates, expected_message_id, expected_replies_in_thread",
    [
        ({}, None, False),
        (
            {"status_message_ts": "1700000000.000900", "reply_mode": ReplyMode.SLACK},
            "1700000000.000900",
            True,
        ),
    ],
    ids=["no-message-yet", "message-to-edit"],
)
def test_publish_status_reports_the_records_status(
    updates: dict[str, object], expected_message_id: str | None, expected_replies_in_thread: bool
) -> None:
    reporter = RecordingReporter()

    _publish(reporter, RecordingThreadMap(), _record(**updates))

    assert reporter.reports == [
        (
            _CONVERSATION,
            StatusChanged(
                state=StatusState.WAITING,
                session_name="pan-login",
                label="fix the login page",
                task_started_at=_NOW,
                updated_at=_NOW,
                replies_in_thread=expected_replies_in_thread,
                detail="Allow edit?",
                status_message_id=expected_message_id,
            ),
        )
    ]


def test_publish_status_stores_the_id_of_a_newly_posted_message() -> None:
    reporter, thread_map = RecordingReporter(), RecordingThreadMap()

    published = _publish(reporter, thread_map, _record())

    assert thread_map.status_messages == [(_THREAD, "status-1", "pan-login")]
    assert published.status_message_ts == "status-1"


def test_publish_status_stores_nothing_when_the_message_was_edited() -> None:
    reporter, thread_map = RecordingReporter(), RecordingThreadMap()
    record = _record(status_message_ts="1700000000.000900")

    published = _publish(reporter, thread_map, record)

    assert thread_map.status_messages == []
    assert published is record


def test_publish_status_never_raises_when_the_report_fails() -> None:
    reporter, thread_map = RecordingReporter(), RecordingThreadMap()
    reporter.error = SlackPostError("down")
    record = _record()

    published = _publish(reporter, thread_map, record)

    assert published is record
    assert thread_map.status_messages == []


@pytest.mark.parametrize(
    "last_answer_digest_of, expected_posted",
    [(None, True), ("the answer", False), ("an older answer", True)],
    ids=["none-posted-yet", "same-answer", "newer-answer"],
)
def test_post_answer_reports_an_answer_not_posted_before(
    last_answer_digest_of: str | None, expected_posted: bool
) -> None:
    reporter, thread_map = RecordingReporter(), RecordingThreadMap()
    digest = answer_digest(last_answer_digest_of) if last_answer_digest_of else None

    posted = post_answer(
        reporter=reporter,
        thread_map=thread_map,  # type: ignore[arg-type]
        record=_record(last_answer_digest=digest),
        answer="the answer",
    )

    assert posted is expected_posted
    assert reporter.events() == ([AnswerReady(text="the answer")] if expected_posted else [])
    expected_digests = [(_THREAD, answer_digest("the answer"), "pan-login")]
    assert thread_map.posted_answers == (expected_digests if expected_posted else [])


def test_post_answer_records_nothing_when_the_report_fails() -> None:
    reporter, thread_map = RecordingReporter(), RecordingThreadMap()
    reporter.error = SlackPostError("down")

    with pytest.raises(SlackPostError):
        post_answer(
            reporter=reporter,
            thread_map=thread_map,  # type: ignore[arg-type]
            record=_record(),
            answer="the answer",
        )

    assert thread_map.posted_answers == []


@pytest.mark.parametrize("requester", [None, ""], ids=["none", "empty"])
def test_send_private_details_is_skipped_without_a_requester(requester: str | None) -> None:
    reporter = RecordingReporter()

    send_private_details(reporter=reporter, record=_record(requester=requester))

    assert reporter.reports == []


@pytest.mark.parametrize(
    "updates, expected_known",
    [
        ({}, True),
        ({"started_by_pan": False}, False),
        ({"started_by_pan": False, "inbox_thread_id": "thr_1"}, True),
    ],
    ids=["own-session", "desk-session-suggested-name", "desk-session-found-thread"],
)
def test_send_private_details_reports_them_for_the_requester(
    updates: dict[str, object], expected_known: bool
) -> None:
    reporter = RecordingReporter()

    send_private_details(
        reporter=reporter,
        record=_record(
            inbox_thread_name="pan-login: fix the login page",
            inbox_thread_url="http://localhost:19441/#thread=abc",
            **updates,
        ),
    )

    assert reporter.reports == [
        (
            _CONVERSATION,
            PrivateDetails(
                requester="U0TEST",
                session_name="pan-login",
                terminal_address="t7",
                inbox_thread_name="pan-login: fix the login page",
                inbox_thread_url="http://localhost:19441/#thread=abc",
                inbox_thread_known=expected_known,
            ),
        )
    ]


def _warnings_logged(caplog: pytest.LogCaptureFixture, action: Callable[[], object]) -> list[str]:
    # propagate=False on this logger, so attach caplog's handler directly.
    status_logger = logging.getLogger("pan.backend.status_message")
    status_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="pan.backend.status_message"):
            action()
    finally:
        status_logger.removeHandler(caplog.handler)
    return [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]


class _ReporterWithoutIds(RecordingReporter):
    def report(self, conversation: Conversation, event: Event) -> str | None:
        super().report(conversation, event)
        return None


def test_publish_status_warns_when_a_new_status_comes_back_without_an_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    thread_map = RecordingThreadMap()
    record = _record()

    warnings = _warnings_logged(caplog, lambda: _publish(_ReporterWithoutIds(), thread_map, record))

    assert warnings == [
        f"status posted without a message id to store conversation={_CONVERSATION.key} "
        "state=waiting"
    ]
    assert thread_map.status_messages == []


def test_send_private_details_logs_a_failure_and_never_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    reporter = RecordingReporter()
    reporter.error = SlackPostError("user_not_in_channel")

    warnings = _warnings_logged(
        caplog, lambda: send_private_details(reporter=reporter, record=_record())
    )

    assert warnings == [
        f"private details could not be sent conversation={_CONVERSATION.key}: user_not_in_channel"
    ]


@pytest.mark.parametrize(
    "terminal_state, agent_status, recorded, expected_state",
    [
        (TerminalState.LIVE, AgentStatus.WORKING, WorkerStatus.IDLE, StatusState.WORKING),
        (TerminalState.LIVE, AgentStatus.BLOCKED, WorkerStatus.RUNNING, StatusState.WAITING),
        (TerminalState.LIVE, AgentStatus.IDLE, WorkerStatus.RUNNING, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.DONE, WorkerStatus.IDLE, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.DONE, WorkerStatus.DONE, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.ERROR, WorkerStatus.RUNNING, StatusState.STOPPED),
        (TerminalState.LIVE, AgentStatus.UNKNOWN, WorkerStatus.RUNNING, StatusState.WORKING),
        (TerminalState.LIVE, AgentStatus.UNKNOWN, WorkerStatus.IDLE, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.NONE, WorkerStatus.SPAWNING, StatusState.WORKING),
        (TerminalState.LIVE, AgentStatus.NONE, WorkerStatus.FAILED, StatusState.FINISHED),
        (TerminalState.LIVE, AgentStatus.NONE, WorkerStatus.BLOCKED, StatusState.WAITING),
        (TerminalState.FINISHED, AgentStatus.DONE, WorkerStatus.IDLE, StatusState.STOPPED),
        (None, None, WorkerStatus.IDLE, StatusState.STOPPED),
        (None, None, WorkerStatus.FAILED, StatusState.STOPPED),
        (None, None, WorkerStatus.DONE, StatusState.CLOSED),
    ],
    ids=[
        "working",
        "blocked-on-a-prompt",
        "idle",
        "hc-done-is-a-finished-turn",
        "hc-done-never-closed-while-live",
        "agent-error",
        "unknown-while-running",
        "unknown-while-idle",
        "no-status-while-starting",
        "no-status-never-stopped-while-live",
        "no-status-blocked-record",
        "terminal-finished",
        "terminal-gone",
        "terminal-gone-failed",
        "terminal-gone-after-pan-done",
    ],
)
def test_check_state(
    terminal_state: TerminalState | None,
    agent_status: AgentStatus | None,
    recorded: WorkerStatus,
    expected_state: StatusState,
) -> None:
    terminal = (
        make_terminal("t7", name="pan-login", state=terminal_state, agent_status=agent_status)
        if terminal_state is not None and agent_status is not None
        else None
    )

    assert check_state(terminal, recorded) is expected_state
