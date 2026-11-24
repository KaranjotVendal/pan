from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pan.models import StatusState, ThreadRecord, WorkerStatus
from pan.slack_render import (
    elapsed_text,
    escape_mrkdwn,
    private_details_text,
    slack_time,
    state_for_worker_status,
    status_blocks,
    status_label,
    status_text,
)

_NOW = datetime(2026, 7, 16, 10, 30, 0, tzinfo=UTC)
_NOW_SLACK_TIME = "<!date^1784197800^{time}|10:30 UTC>"


def _record(**updates: object) -> ThreadRecord:
    record = ThreadRecord(
        thread_ts="1700000000.000100",
        terminal_name="pan-login",
        terminal_address="t7",
        channel="C0TEST",
        worktree_path=Path("/tmp/wt/pan-login"),
        created_at=_NOW,
        updated_at=_NOW,
        inbox_thread_name="pan-login: fix the login page",
    )
    return record.model_copy(update=updates)


@pytest.mark.parametrize(
    "task, max_length, expected",
    [
        ("fix the login page", 80, "fix the login page"),
        ("  fix\n the   login\tpage ", 80, "fix the login page"),
        ("fix the login page", 10, "fix the..."),
        ("abcdefghij", 10, "abcdefghij"),
        ("", 80, ""),
    ],
    ids=["short", "whitespace-collapsed", "cut", "exactly-max", "empty"],
)
def test_status_label(task: str, max_length: int, expected: str) -> None:
    assert status_label(task, max_length) == expected


@pytest.mark.parametrize(
    "elapsed, expected",
    [
        (timedelta(seconds=59), None),
        (timedelta(minutes=1), "1m"),
        (timedelta(minutes=59, seconds=59), "59m"),
        (timedelta(hours=1), "1h"),
        (timedelta(hours=2, minutes=5), "2h 5m"),
        (timedelta(seconds=-5), None),
    ],
    ids=[
        "under-a-minute",
        "one-minute",
        "under-an-hour",
        "one-hour",
        "hours-and-minutes",
        "clock-skew",
    ],
)
def test_elapsed_text(elapsed: timedelta, expected: str | None) -> None:
    assert elapsed_text(elapsed) == expected


def test_slack_time_renders_in_the_readers_zone_with_a_utc_fallback() -> None:
    assert slack_time(_NOW) == _NOW_SLACK_TIME


@pytest.mark.parametrize(
    "text, expected",
    [("a < b & c > d", "a &lt; b &amp; c &gt; d"), ("plain", "plain")],
    ids=["control-characters", "plain"],
)
def test_escape_mrkdwn(text: str, expected: str) -> None:
    assert escape_mrkdwn(text) == expected


@pytest.mark.parametrize(
    "status, expected_state",
    [
        (WorkerStatus.SPAWNING, StatusState.WORKING),
        (WorkerStatus.RUNNING, StatusState.WORKING),
        (WorkerStatus.BLOCKED, StatusState.WAITING),
        (WorkerStatus.IDLE, StatusState.FINISHED),
        (WorkerStatus.DONE, StatusState.CLOSED),
        (WorkerStatus.FAILED, StatusState.STOPPED),
    ],
)
def test_state_for_worker_status(status: WorkerStatus, expected_state: StatusState) -> None:
    assert state_for_worker_status(status) is expected_state


@pytest.mark.parametrize(
    "state, label, expected",
    [
        (StatusState.WORKING, "fix the login page", "Working: fix the login page"),
        (StatusState.WAITING, "fix the login page", "Waiting for you: fix the login page"),
        (StatusState.FINISHED, "fix the login page", "Finished: fix the login page"),
        (StatusState.CLOSED, "fix the login page", "Closed: fix the login page"),
        (StatusState.STOPPED, "fix the login page", "Stopped: fix the login page"),
        (StatusState.FINISHED, None, "Finished"),
    ],
    ids=["working", "waiting", "finished", "closed", "stopped", "no-label"],
)
def test_status_text(state: StatusState, label: str | None, expected: str) -> None:
    assert status_text(state, label) == expected


@pytest.mark.parametrize(
    "state, expected_title",
    [
        (StatusState.WORKING, "Working"),
        (StatusState.WAITING, "Waiting for you"),
        (StatusState.FINISHED, "Finished"),
        (StatusState.CLOSED, "Closed"),
        (StatusState.STOPPED, "Stopped"),
    ],
)
def test_status_blocks_per_state(state: StatusState, expected_title: str) -> None:
    blocks = status_blocks(
        state,
        session_name="pan-login",
        label="fix the login page",
        started_at=_NOW - timedelta(minutes=4),
        now=_NOW,
    )

    assert blocks == [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*{expected_title}*\nfix the login page"},
        },
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"pan-login · 4m elapsed · updated {_NOW_SLACK_TIME}"}
            ],
        },
    ]


@pytest.mark.parametrize(
    "label, started_at, replies_in_thread, detail, expected_section, expected_context",
    [
        (
            None,
            None,
            False,
            None,
            "*Working*",
            f"pan-login · updated {_NOW_SLACK_TIME}",
        ),
        (
            "fix <the> page",
            _NOW - timedelta(seconds=20),
            True,
            None,
            "*Working*\nfix &lt;the&gt; page",
            f"pan-login · updated {_NOW_SLACK_TIME} · answers posted here",
        ),
        (
            "fix the login page",
            _NOW - timedelta(hours=1, minutes=2),
            False,
            "Claude needs your permission to use Bash",
            "*Working*\nfix the login page\n_Claude needs your permission to use Bash_",
            f"pan-login · 1h 2m elapsed · updated {_NOW_SLACK_TIME}",
        ),
    ],
    ids=["old-record-no-label", "escaped-label-replies-here", "with-detail"],
)
def test_status_blocks_optional_parts(
    label: str | None,
    started_at: datetime | None,
    replies_in_thread: bool,
    detail: str | None,
    expected_section: str,
    expected_context: str,
) -> None:
    blocks = status_blocks(
        StatusState.WORKING,
        session_name="pan-login",
        label=label,
        started_at=started_at,
        now=_NOW,
        replies_in_thread=replies_in_thread,
        detail=detail,
    )

    assert blocks[0]["text"]["text"] == expected_section
    assert blocks[1]["elements"][0]["text"] == expected_context


@pytest.mark.parametrize(
    "updates, expected_inbox_line",
    [
        (
            {"inbox_thread_url": "http://localhost:19441/#thread=abc"},
            "Inbox thread: <http://localhost:19441/#thread=abc|pan-login: fix the login page>",
        ),
        (
            {"inbox_thread_url": "http://localhost:19441/#thread=abc", "inbox_thread_name": None},
            "Inbox thread: <http://localhost:19441/#thread=abc|open>",
        ),
        ({}, 'Inbox thread: "pan-login: fix the login page"'),
        (
            {"started_by_pan": False},
            'Inbox thread: its existing one, or "pan-login: fix the login page"',
        ),
        (
            {"started_by_pan": False, "inbox_thread_id": "thr_1"},
            'Inbox thread: "pan-login: fix the login page"',
        ),
        ({"inbox_thread_name": None}, "Inbox thread: none yet"),
    ],
    ids=[
        "link",
        "link-without-name",
        "own-session-name",
        "desk-session-suggested-name",
        "desk-session-found-thread",
        "none",
    ],
)
def test_private_details_text_names_the_inbox_thread(
    updates: dict[str, object], expected_inbox_line: str
) -> None:
    assert private_details_text(_record(**updates)) == (
        f"Details for pan-login (only visible to you)\n{expected_inbox_line}\nTerminal: t7"
    )


def test_private_details_text_without_a_terminal_address() -> None:
    assert private_details_text(_record(terminal_address="")) == (
        'Details for pan-login (only visible to you)\nInbox thread: "pan-login: fix the login page"'
    )
