from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.backend.sessions import collect_sessions, session_drift, worker_status_for
from pan.backend.spawn import worker_brief
from pan.core.models import AgentStatus, ReplyMode, TerminalState, ThreadRecord, WorkerStatus

_FIXED_NOW = datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC)


def _record(
    thread_ts: str,
    terminal_name: str,
    terminal_address: str,
    worktree_path: str,
    status: WorkerStatus = WorkerStatus.RUNNING,
) -> ThreadRecord:
    return ThreadRecord(
        thread_ts=thread_ts,
        terminal_name=terminal_name,
        terminal_address=terminal_address,
        channel="C1",
        worktree_path=Path(worktree_path),
        status=status,
        created_at=_FIXED_NOW,
        updated_at=_FIXED_NOW,
    )


class FakeThreadMap:
    def __init__(self, records: list[ThreadRecord]) -> None:
        self._records = records

    def records(self) -> list[ThreadRecord]:
        return self._records


def _terminal_host_with(*terminals: object) -> FakeTerminalHost:
    terminal_host = FakeTerminalHost()
    for terminal in terminals:
        terminal_host.add(terminal)  # type: ignore[arg-type]
    return terminal_host


def _desk(record: ThreadRecord) -> ThreadRecord:
    return record.model_copy(update={"started_by_pan": False})


@pytest.mark.parametrize(
    "terminal, record",
    [
        (
            make_terminal("t5", name="pan-a", cwd=Path("/tmp/elsewhere")),
            _record("t-1", "pan-a", "t5", "/tmp/worktree"),
        ),
        (
            make_terminal("t5", cwd=Path("/tmp/elsewhere")),
            _record("t-1", "pan-a", "t5", "/tmp/worktree"),
        ),
        (
            make_terminal("t6", name="pan-a", cwd=Path("/tmp/elsewhere")),
            _record("t-1", "pan-a", "", "/tmp/worktree"),
        ),
        (
            make_terminal("t6", name="pan-a", cwd=Path("/tmp/worktree")),
            _record("t-1", "pan-a", "t5", "/tmp/worktree"),
        ),
        (
            make_terminal("t7", cwd=Path("/tmp/worktree")),
            _record("t-1", "pan-z", "", "/tmp/worktree"),
        ),
    ],
    ids=[
        "address-and-name",
        "address-of-an-unnamed-terminal",
        "name-of-a-record-without-address",
        "name-of-a-worker-back-at-a-new-address-in-its-worktree",
        "worktree-of-a-record-without-address",
    ],
)
def test_collect_sessions_joins_pan_owned_terminals(terminal: object, record: ThreadRecord) -> None:
    summaries = collect_sessions(_terminal_host_with(terminal), FakeThreadMap([record]))

    (summary,) = summaries
    assert summary.is_pan_owned is True
    assert summary.thread_ts == "t-1"
    assert summary.pan_status is WorkerStatus.RUNNING
    assert summary.drift is False


@pytest.mark.parametrize(
    "terminal, record",
    [
        (
            make_terminal("t5", name="pan-b", cwd=Path("/tmp/elsewhere")),
            _record("t-1", "pan-a", "t5", "/tmp/worktree"),
        ),
        (
            make_terminal("t10", name="desk-test", cwd=Path("/tmp/desk/test")),
            _desk(_record("t-1", "desk-test", "t9", "/tmp/desk/test")),
        ),
        (
            make_terminal("t6", name="pan-a", cwd=Path("/tmp/elsewhere")),
            _record("t-1", "pan-a", "t5", "/tmp/worktree"),
        ),
        (
            make_terminal("t6", name="pan-b", cwd=Path("/tmp/worktree")),
            _record("t-1", "pan-a", "t5", "/tmp/worktree"),
        ),
        (
            make_terminal("t6", cwd=Path("/tmp/worktree")),
            _record("t-1", "pan-a", "t5", "/tmp/worktree"),
        ),
    ],
    ids=[
        "address-reused-by-another-name",
        "name-of-a-killed-desk-terminal",
        "name-of-a-worker-outside-its-worktree",
        "worktree-of-a-killed-worker-under-another-name",
        "worktree-of-a-killed-worker-unnamed-terminal",
    ],
)
def test_collect_sessions_does_not_join_a_record_of_another_terminal(
    terminal: object, record: ThreadRecord
) -> None:
    (summary,) = collect_sessions(_terminal_host_with(terminal), FakeThreadMap([record]))

    assert summary.thread_ts is None


def test_collect_sessions_shows_a_tied_desk_session_as_external_with_its_thread() -> None:
    terminal = make_terminal("t9", name="desk-api", cwd=Path("/tmp/desk/api"))
    desk_record = _record("t-1", "desk-api", "t9", "/tmp/desk/api").model_copy(
        update={"started_by_pan": False}
    )
    other_terminal = make_terminal("t10", cwd=Path("/tmp/desk/api"))

    desk_summary, other_summary = collect_sessions(
        _terminal_host_with(terminal, other_terminal), FakeThreadMap([desk_record])
    )

    assert desk_summary.is_pan_owned is False
    assert desk_summary.thread_ts == "t-1"
    assert desk_summary.pan_status is None
    assert desk_summary.drift is False
    # Another terminal in the desk session's folder is not joined to its record.
    assert other_summary.thread_ts is None


def test_collect_sessions_marks_external_terminal() -> None:
    terminal = make_terminal(
        "t2",
        where="my-app / 1",
        cwd=Path("/tmp/other"),
        agent="zsh",
        agent_status=AgentStatus.NONE,
    )
    record = _record("t-1", "pan-a", "t5", "/tmp/worktree")

    (summary,) = collect_sessions(_terminal_host_with(terminal), FakeThreadMap([record]))

    assert summary.address == "t2"
    assert summary.where == "my-app / 1"
    assert summary.agent == "zsh"
    assert summary.is_pan_owned is False
    assert summary.thread_ts is None
    assert summary.pan_status is None
    assert summary.drift is False


@pytest.mark.parametrize(
    "pan_status, agent_status, expected_drift",
    [
        (WorkerStatus.BLOCKED, AgentStatus.BLOCKED, False),
        (WorkerStatus.RUNNING, AgentStatus.WORKING, False),
        (WorkerStatus.BLOCKED, AgentStatus.IDLE, True),
        (WorkerStatus.RUNNING, AgentStatus.ERROR, True),
    ],
)
def test_collect_sessions_sets_drift_flag(
    pan_status: WorkerStatus, agent_status: AgentStatus, expected_drift: bool
) -> None:
    terminal = make_terminal("t5", agent_status=agent_status)
    record = _record("t-1", "pan-a", "t5", "/tmp/worktree", status=pan_status)

    (summary,) = collect_sessions(_terminal_host_with(terminal), FakeThreadMap([record]))

    assert summary.drift is expected_drift


@pytest.mark.parametrize(
    "agent_status, recorded, expected",
    [
        (AgentStatus.WORKING, WorkerStatus.SPAWNING, WorkerStatus.RUNNING),
        (AgentStatus.IDLE, WorkerStatus.RUNNING, WorkerStatus.IDLE),
        (AgentStatus.IDLE, WorkerStatus.IDLE, WorkerStatus.IDLE),
        (AgentStatus.WORKING, WorkerStatus.IDLE, WorkerStatus.RUNNING),
        (AgentStatus.BLOCKED, WorkerStatus.RUNNING, WorkerStatus.BLOCKED),
        (AgentStatus.DONE, WorkerStatus.RUNNING, WorkerStatus.DONE),
        (AgentStatus.ERROR, WorkerStatus.RUNNING, WorkerStatus.FAILED),
        (AgentStatus.NONE, WorkerStatus.SPAWNING, WorkerStatus.SPAWNING),
        (AgentStatus.NONE, WorkerStatus.RUNNING, WorkerStatus.FAILED),
        (AgentStatus.UNKNOWN, WorkerStatus.BLOCKED, WorkerStatus.BLOCKED),
    ],
)
def test_worker_status_for_a_live_terminal(
    agent_status: AgentStatus, recorded: WorkerStatus, expected: WorkerStatus
) -> None:
    assert worker_status_for(make_terminal("t5", agent_status=agent_status), recorded) is expected


@pytest.mark.parametrize(
    "recorded, expected",
    [
        (WorkerStatus.DONE, WorkerStatus.DONE),
        (WorkerStatus.RUNNING, WorkerStatus.FAILED),
        (WorkerStatus.IDLE, WorkerStatus.FAILED),
        (WorkerStatus.SPAWNING, WorkerStatus.FAILED),
    ],
)
@pytest.mark.parametrize("finished", [True, False], ids=["finished", "missing"])
def test_worker_status_for_a_gone_terminal(
    recorded: WorkerStatus, expected: WorkerStatus, finished: bool
) -> None:
    terminal = make_terminal("t5", state=TerminalState.FINISHED) if finished else None

    assert worker_status_for(terminal, recorded) is expected


@pytest.mark.parametrize(
    "pan_status, agent_status, expected",
    [
        (WorkerStatus.RUNNING, AgentStatus.IDLE, False),
        (WorkerStatus.IDLE, AgentStatus.WORKING, False),
        (WorkerStatus.IDLE, AgentStatus.IDLE, False),
        (WorkerStatus.IDLE, AgentStatus.BLOCKED, True),
        (WorkerStatus.DONE, AgentStatus.DONE, False),
        (WorkerStatus.RUNNING, AgentStatus.DONE, True),
        (WorkerStatus.SPAWNING, AgentStatus.ERROR, False),
        (WorkerStatus.RUNNING, AgentStatus.UNKNOWN, False),
    ],
)
def test_session_drift(pan_status: WorkerStatus, agent_status: AgentStatus, expected: bool) -> None:
    assert session_drift(pan_status, make_terminal("t5", agent_status=agent_status)) is expected


def _transcript_asking(tmp_path: Path, ask: str) -> Path:
    transcript = tmp_path / "session.jsonl"
    entry = {"type": "user", "message": {"role": "user", "content": ask}}
    transcript.write_text(json.dumps(entry) + "\n")
    return transcript


@pytest.mark.parametrize(
    "ask, expected",
    [
        ("add the tests", "add the tests"),
        (
            worker_brief("pan-a: fix the login page", "fix the\nlogin page", ReplyMode.ON_DEMAND),
            "fix the login page",
        ),
        ("x" * 100, "x" * 77 + "..."),
    ],
    ids=["plain", "brief-cut-to-its-task", "truncated"],
)
def test_collect_sessions_shows_the_last_ask(tmp_path: Path, ask: str, expected: str) -> None:
    record = _record("t-1", "pan-a", "t5", "/tmp/worktree")
    record.transcript_path = _transcript_asking(tmp_path, ask)
    terminal_host = _terminal_host_with(make_terminal("t5", name="pan-a"))

    (summary,) = collect_sessions(terminal_host, FakeThreadMap([record]))

    assert summary.last_ask == expected


def test_collect_sessions_leaves_the_last_ask_blank_without_a_transcript() -> None:
    terminal_host = _terminal_host_with(
        make_terminal("t5", name="pan-a"), make_terminal("t6", name="desk-api")
    )
    records = [_record("t-1", "pan-a", "t5", "/tmp/worktree")]

    summaries = collect_sessions(terminal_host, FakeThreadMap(records))

    assert [summary.last_ask for summary in summaries] == [None, None]


@pytest.mark.parametrize(
    "started_by_pan, inbox_thread_id, expected",
    [
        (True, None, "pan-a: fix the build"),
        (False, "thr_1", "pan-a: fix the build"),
        (False, None, None),
    ],
    ids=["pan-named-it", "found-for-a-desk-session", "only-suggested-to-a-desk-session"],
)
def test_collect_sessions_shows_the_inbox_thread_pan_knows(
    started_by_pan: bool, inbox_thread_id: str | None, expected: str | None
) -> None:
    record = _record("t-1", "pan-a", "t12", "/tmp/worktree").model_copy(
        update={
            "started_by_pan": started_by_pan,
            "inbox_thread_name": "pan-a: fix the build",
            "inbox_thread_id": inbox_thread_id,
        }
    )

    (summary,) = collect_sessions(
        _terminal_host_with(make_terminal("t12", name="pan-a", cwd=Path("/tmp/worktree"))),
        FakeThreadMap([record]),
    )

    assert summary.inbox_thread_name == expected


@pytest.mark.parametrize("status_label", ["Google Doc for the plan", None])
def test_collect_sessions_carries_the_thread_channel_and_label(status_label: str | None) -> None:
    record = _record("t-1", "pan-a", "t12", "/tmp/worktree").model_copy(
        update={"status_label": status_label}
    )

    (summary,) = collect_sessions(
        _terminal_host_with(make_terminal("t12", name="pan-a", cwd=Path("/tmp/worktree"))),
        FakeThreadMap([record]),
    )

    assert summary.thread_channel == "C1"
    assert summary.label == status_label
    assert summary.updated_at == record.updated_at


def test_collect_sessions_leaves_the_thread_channel_and_label_blank_without_a_record() -> None:
    (summary,) = collect_sessions(
        _terminal_host_with(make_terminal("t12", name="desk-api")), FakeThreadMap([])
    )

    assert summary.thread_channel is None
    assert summary.label is None
    assert summary.updated_at is None
