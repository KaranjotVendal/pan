from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from hc_fakes import FakeHc, make_terminal

from pan.models import AgentStatus, TerminalState, ThreadRecord, WorkerStatus
from pan.sessions import collect_sessions, session_drift, worker_status_for

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


def _hc_with(*terminals: object) -> FakeHc:
    hc = FakeHc()
    for terminal in terminals:
        hc.add(terminal)  # type: ignore[arg-type]
    return hc


@pytest.mark.parametrize(
    "terminal, record",
    [
        # Address recorded at spawn: the most precise join.
        (
            make_terminal("t5", name="renamed", cwd=Path("/tmp/elsewhere")),
            _record("t-1", "pan-a", "t5", "/tmp/worktree"),
        ),
        # Address unknown (e.g. recreated), but the stream name still matches.
        (
            make_terminal("t6", name="pan-a", cwd=Path("/tmp/elsewhere")),
            _record("t-1", "pan-a", "", "/tmp/worktree"),
        ),
        # Neither matches, but the terminal runs in the worker's worktree.
        (
            make_terminal("t7", cwd=Path("/tmp/worktree")),
            _record("t-1", "pan-z", "t99", "/tmp/worktree"),
        ),
    ],
    ids=["address", "name", "cwd"],
)
def test_collect_sessions_joins_pan_owned_terminals(terminal: object, record: ThreadRecord) -> None:
    summaries = collect_sessions(_hc_with(terminal), FakeThreadMap([record]))

    (summary,) = summaries
    assert summary.is_pan_owned is True
    assert summary.thread_ts == "t-1"
    assert summary.pan_status is WorkerStatus.RUNNING
    assert summary.drift is False


def test_collect_sessions_marks_external_terminal() -> None:
    terminal = make_terminal(
        "t2",
        where="my-app / 1",
        cwd=Path("/tmp/other"),
        agent="zsh",
        agent_status=AgentStatus.NONE,
    )
    record = _record("t-1", "pan-a", "t5", "/tmp/worktree")

    (summary,) = collect_sessions(_hc_with(terminal), FakeThreadMap([record]))

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

    (summary,) = collect_sessions(_hc_with(terminal), FakeThreadMap([record]))

    assert summary.drift is expected_drift


@pytest.mark.parametrize(
    "agent_status, recorded, expected",
    [
        (AgentStatus.WORKING, WorkerStatus.SPAWNING, WorkerStatus.RUNNING),
        (AgentStatus.IDLE, WorkerStatus.RUNNING, WorkerStatus.RUNNING),
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
        (WorkerStatus.DONE, AgentStatus.DONE, False),
        (WorkerStatus.RUNNING, AgentStatus.DONE, True),
        (WorkerStatus.SPAWNING, AgentStatus.ERROR, False),
        (WorkerStatus.RUNNING, AgentStatus.UNKNOWN, False),
    ],
)
def test_session_drift(pan_status: WorkerStatus, agent_status: AgentStatus, expected: bool) -> None:
    assert session_drift(pan_status, make_terminal("t5", agent_status=agent_status)) is expected
