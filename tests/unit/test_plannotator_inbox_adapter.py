from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pan.adapters.plannotator_inbox import (
    ShellPlannotatorInboxAdapter,
    find_thread_in_listing,
    latest_agent_session_in,
    threads_in_listing,
)
from pan.models import InboxThread

# The envelope and row fields of a live `plannotator inbox call read_thread` listing (ids,
# names and paths replaced): the project, its threads newest first, and a total.
_LIVE_LISTING = {
    "project": {"id": "prj_1", "name": "desk-test", "root": "/tmp/desk/test"},
    "threads": [
        {
            "thread_id": "msg_1",
            "subject": "desk-test: reply with the word hello",
            "thread_name": "desk-test: reply with the word hello",
            "section": "quiet",
            "message_count": 1,
            "questions": {"open": 0, "picked": 0, "prompt": None},
            "last_author": "agent",
            "last_at": "2026-01-01T00:00:00.000Z",
            "last_person_at": None,
            "last_person_message_id": None,
            "unread": False,
        }
    ],
    "total": 1,
}
# The row fields `plannotator inbox --help` documents for read_thread with no thread id. The
# adapter searches for rows wherever they sit.
_LISTING = {
    "threads": [
        {
            "thread_id": "thr_2",
            "subject": "Fix the build",
            "thread_name": "desk-api: fix the build",
            "message_count": 3,
        },
        {
            "thread_id": "thr_1",
            "subject": "Older",
            "thread_name": "desk-api: older work",
            "url": "https://inbox.example/threads/thr_1",
        },
    ]
}


@pytest.mark.parametrize(
    "listing, thread_name, expected",
    [
        (
            _LISTING,
            "desk-api: fix the build",
            InboxThread(thread_id="thr_2", name="desk-api: fix the build"),
        ),
        (
            _LISTING,
            "DESK-API:  Fix The Build",
            InboxThread(thread_id="thr_2", name="desk-api: fix the build"),
        ),
        (
            _LISTING,
            "desk-api: older work",
            InboxThread(
                thread_id="thr_1",
                name="desk-api: older work",
                url="https://inbox.example/threads/thr_1",
            ),
        ),
        (
            _LISTING["threads"],
            "desk-api: fix the build",
            InboxThread(thread_id="thr_2", name="desk-api: fix the build"),
        ),
        (
            _LIVE_LISTING,
            "desk-test: reply with the word hello",
            InboxThread(thread_id="msg_1", name="desk-test: reply with the word hello"),
        ),
        (_LISTING, "desk-api: something else", None),
        ({"threads": [{"thread_id": "thr_3", "subject": "no name"}]}, "no name", None),
        ("not a listing", "desk-api: fix the build", None),
    ],
    ids=[
        "exact-name",
        "case-and-spacing-ignored",
        "row-with-a-link",
        "bare-list",
        "live-listing",
        "no-match",
        "unnamed-thread-never-matches-by-subject",
        "unexpected-shape",
    ],
)
def test_find_thread_in_listing(
    listing: Any, thread_name: str, expected: InboxThread | None
) -> None:
    assert find_thread_in_listing(listing, thread_name) == expected


def _fake_run(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, stdout: str = ""
) -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return commands


def test_find_thread_lists_the_projects_threads_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _fake_run(monkeypatch, stdout=json.dumps(_LISTING))

    found = ShellPlannotatorInboxAdapter().find_thread(
        Path("/tmp/desk/api"), "desk-api: fix the build"
    )

    assert found == InboxThread(thread_id="thr_2", name="desk-api: fix the build")
    (command,) = commands
    assert command[:4] == ["plannotator", "inbox", "call", "read_thread"]
    assert json.loads(command[4]) == {
        "asked_by": "anyone",
        "limit": 50,
        "project_path": "/tmp/desk/api",
    }


@pytest.mark.parametrize(
    "returncode, stdout",
    [(1, ""), (0, "not json")],
    ids=["refused-or-no-inbox", "non-json-output"],
)
def test_find_thread_failures_return_none(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    _fake_run(monkeypatch, returncode=returncode, stdout=stdout)

    assert ShellPlannotatorInboxAdapter().find_thread(Path("/tmp/x"), "any") is None


@pytest.mark.parametrize(
    "error",
    [OSError("no plannotator"), subprocess.TimeoutExpired(["plannotator"], 10.0)],
    ids=["missing-binary", "timeout"],
)
def test_find_thread_when_plannotator_cannot_run_returns_none(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def raise_error(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise error

    monkeypatch.setattr(subprocess, "run", raise_error)

    assert ShellPlannotatorInboxAdapter().find_thread(Path("/tmp/x"), "any") is None


# The envelope and message fields of a live `read_thread` call with a thread id and
# `format: "full"` (ids, names and bodies replaced): every message, oldest first, each with an
# `author` object that carries the Claude `session` for an agent's message.
def _message(kind: str, created_at: str, session: str | None = None) -> dict[str, Any]:
    author: dict[str, Any] = {"kind": kind, "name": kind}
    if session is not None:
        author["host"] = "host-1"
        author["session"] = session
    return {
        "id": f"msg_{created_at}",
        "thread_id": "thr_1",
        "author": author,
        "subject": "s",
        "body": "b",
        "created_at": created_at,
        "thread_name": "pan with Slack and X",
    }


def _full_thread(*messages: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread": {
            "thread_id": "thr_1",
            "project": {"id": "prj_1", "root": "/tmp/repo"},
            "thread_name": "pan with Slack and X",
            "resolved_at": None,
            "messages": list(messages),
        },
        "url": "https://inbox.example/threads/thr_1",
    }


@pytest.mark.parametrize(
    "thread, expected",
    [
        (
            _full_thread(
                _message("agent", "2026-01-01T00:00:00.000Z", "session-old"),
                _message("person", "2026-01-01T00:01:00.000Z"),
                _message("agent", "2026-01-01T00:02:00.000Z", "session-new"),
                _message("person", "2026-01-01T00:03:00.000Z"),
            ),
            "session-new",
        ),
        (
            _full_thread(
                _message("agent", "2026-01-01T00:05:00.000Z", "session-late"),
                _message("agent", "2026-01-01T00:02:00.000Z", "session-early"),
            ),
            "session-late",
        ),
        (_full_thread(_message("person", "2026-01-01T00:00:00.000Z")), None),
        (_full_thread(), None),
        ("not a thread", None),
    ],
    ids=["newest-agent-message", "by-time-not-order", "person-only", "empty", "unexpected-shape"],
)
def test_latest_agent_session_in(thread: Any, expected: str | None) -> None:
    assert latest_agent_session_in(thread) == expected


def test_threads_in_listing_skips_unnamed_threads() -> None:
    listing = {
        "threads": [
            {"thread_id": "thr_1", "thread_name": "pan with Slack and X"},
            {"thread_id": "thr_2", "subject": "no name"},
        ]
    }

    assert threads_in_listing(listing) == [
        InboxThread(thread_id="thr_1", name="pan with Slack and X")
    ]


def test_list_threads_lists_the_projects_open_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _fake_run(monkeypatch, stdout=json.dumps(_LIVE_LISTING))

    threads = ShellPlannotatorInboxAdapter().list_threads(Path("/tmp/desk/test"))

    assert threads == [InboxThread(thread_id="msg_1", name="desk-test: reply with the word hello")]
    (command,) = commands
    assert json.loads(command[4]) == {
        "asked_by": "anyone",
        "limit": 50,
        "project_path": "/tmp/desk/test",
    }


def test_latest_agent_session_reads_the_thread_in_full(monkeypatch: pytest.MonkeyPatch) -> None:
    thread = _full_thread(_message("agent", "2026-01-01T00:00:00.000Z", "session-1"))
    commands = _fake_run(monkeypatch, stdout=json.dumps(thread))

    assert ShellPlannotatorInboxAdapter().latest_agent_session("thr_1") == "session-1"
    (command,) = commands
    assert command[:4] == ["plannotator", "inbox", "call", "read_thread"]
    assert json.loads(command[4]) == {"thread_id": "thr_1", "format": "full"}


@pytest.mark.parametrize(
    "returncode, stdout",
    [(1, ""), (0, "not json")],
    ids=["refused-or-no-inbox", "non-json-output"],
)
def test_listing_and_thread_read_failures_yield_nothing(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str
) -> None:
    _fake_run(monkeypatch, returncode=returncode, stdout=stdout)
    adapter = ShellPlannotatorInboxAdapter()

    assert adapter.list_threads(Path("/tmp/x")) == []
    assert adapter.latest_agent_session("thr_1") is None
