from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pan.core.models import InboxThread
from pan.data.adapters.plannotator_inbox import ShellPlannotatorInboxAdapter, find_thread_in_listing

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
