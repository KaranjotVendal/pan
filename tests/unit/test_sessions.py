from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pan.errors import MorcliError
from pan.models import (
    AgentStatus,
    LiveSession,
    ThreadRecord,
    WorkerStatus,
)
from pan.sessions import collect_sessions, session_drift

_FIXED_NOW = datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC)


def _live(
    workspace_name: str,
    workspace_id: str,
    pane_id: str,
    cwd: str,
    agent_status: AgentStatus,
) -> LiveSession:
    return LiveSession(
        workspace_name=workspace_name,
        workspace_id=workspace_id,
        pane_id=pane_id,
        cwd=Path(cwd),
        agent_status=agent_status,
    )


def _record(
    thread_ts: str,
    workspace_name: str,
    workspace_id: str,
    worktree_path: str,
    status: WorkerStatus = WorkerStatus.RUNNING,
    morcli_session: str | None = None,
) -> ThreadRecord:
    return ThreadRecord(
        thread_ts=thread_ts,
        workspace_name=workspace_name,
        workspace_id=workspace_id,
        channel="C1",
        worktree_path=Path(worktree_path),
        status=status,
        morcli_session=morcli_session,
        created_at=_FIXED_NOW,
        updated_at=_FIXED_NOW,
    )


class FakeHerdr:
    def __init__(self, sessions: list[LiveSession]) -> None:
        self._sessions = sessions

    def create_workspace(self, label: str, cwd: Path) -> tuple[str, str]:  # pragma: no cover
        raise NotImplementedError

    def nudge(self, pane_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def send_text(self, pane_id: str, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def kill_pane(self, pane_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def list_workspaces(self) -> list[LiveSession]:
        return self._sessions


class FakeThreadMap:
    def __init__(self, records: list[ThreadRecord]) -> None:
        self._records = records

    def get(self, thread_ts: str) -> ThreadRecord | None:  # pragma: no cover
        for record in self._records:
            if record.thread_ts == thread_ts:
                return record
        return None

    def get_by_worktree(self, worktree_path: Path) -> ThreadRecord | None:  # pragma: no cover
        raise NotImplementedError

    def put(self, record: ThreadRecord) -> None:  # pragma: no cover
        raise NotImplementedError

    def update_status(self, thread_ts: str, status: WorkerStatus) -> None:  # pragma: no cover
        raise NotImplementedError

    def records(self) -> list[ThreadRecord]:
        return self._records


class FakeMorcli:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises
        self.calls: list[str] = []

    def session_status(self, handle: str) -> WorkerStatus:
        self.calls.append(handle)
        if self._raises:
            raise MorcliError(f"no stream for {handle}")
        return WorkerStatus.RUNNING

    def resolve_session(self, workspace_id: str) -> str | None:  # pragma: no cover
        raise NotImplementedError


def test_collect_sessions_marks_pan_owned_by_workspace_name() -> None:
    # Name-primary match: the pane's cwd deliberately does NOT equal the record's
    # worktree path, so only the workspace_name join can identify pan ownership.
    live = _live("pan-a", "w1", "1-1", "/tmp/pane-cwd", AgentStatus.WORKING)
    record = _record("t-1", "pan-a", "w1", "/tmp/worktree", morcli_session="sess-1")
    morcli = FakeMorcli()

    summaries = collect_sessions(FakeHerdr([live]), FakeThreadMap([record]), morcli)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.is_pan_owned is True
    assert summary.thread_ts == "t-1"
    assert summary.pan_status is WorkerStatus.RUNNING
    assert summary.morcli_session == "sess-1"
    assert summary.drift is False


def test_collect_sessions_matches_by_resolved_cwd_fallback() -> None:
    # Workspace name differs (e.g. relabeled), but the pane cwd equals the worktree
    # path, so the resolved-cwd fallback still identifies the session as pan-owned.
    live = _live("relabeled", "w9", "9-1", "/tmp/worktree", AgentStatus.IDLE)
    record = _record("t-9", "pan-z", "w9", "/tmp/worktree")
    morcli = FakeMorcli()

    summaries = collect_sessions(FakeHerdr([live]), FakeThreadMap([record]), morcli)

    assert summaries[0].is_pan_owned is True
    assert summaries[0].thread_ts == "t-9"


def test_collect_sessions_marks_external_session() -> None:
    live = _live("some-external", "w2", "2-1", "/tmp/other", AgentStatus.WORKING)

    summaries = collect_sessions(FakeHerdr([live]), FakeThreadMap([]), FakeMorcli())

    summary = summaries[0]
    assert summary.is_pan_owned is False
    assert summary.thread_ts is None
    assert summary.pan_status is None
    assert summary.morcli_session is None
    assert summary.drift is False


def test_collect_sessions_records_present_but_no_match_is_external() -> None:
    # Thread records exist, but neither the workspace_name nor the resolved-cwd fallback
    # matches this live session, so it is still classified external.
    live = _live("some-external", "w2", "2-1", "/tmp/other", AgentStatus.WORKING)
    record = _record("t-1", "pan-a", "w1", "/tmp/worktree")

    summaries = collect_sessions(FakeHerdr([live]), FakeThreadMap([record]), FakeMorcli())

    assert summaries[0].is_pan_owned is False
    assert summaries[0].thread_ts is None


@pytest.mark.parametrize(
    "pan_status, agent_status, expected_drift",
    [
        (WorkerStatus.BLOCKED, AgentStatus.BLOCKED, False),
        (WorkerStatus.RUNNING, AgentStatus.WORKING, False),
        (WorkerStatus.BLOCKED, AgentStatus.IDLE, True),
        (WorkerStatus.RUNNING, AgentStatus.BLOCKED, True),
    ],
)
def test_collect_sessions_sets_drift_flag(
    pan_status: WorkerStatus, agent_status: AgentStatus, expected_drift: bool
) -> None:
    live = _live("pan-a", "w1", "1-1", "/tmp/worktree", agent_status)
    record = _record("t-1", "pan-a", "w1", "/tmp/worktree", status=pan_status)

    summaries = collect_sessions(FakeHerdr([live]), FakeThreadMap([record]), FakeMorcli())

    assert summaries[0].drift is expected_drift


def test_collect_sessions_tolerates_morcli_error() -> None:
    # A morcli hiccup must never drop a session from the view; the session is still
    # listed and morcli_session degrades to the recorded value.
    live = _live("pan-a", "w1", "1-1", "/tmp/worktree", AgentStatus.WORKING)
    record = _record("t-1", "pan-a", "w1", "/tmp/worktree", morcli_session="sess-1")
    morcli = FakeMorcli(raises=True)

    summaries = collect_sessions(FakeHerdr([live]), FakeThreadMap([record]), morcli)

    assert len(summaries) == 1
    assert summaries[0].is_pan_owned is True
    assert summaries[0].morcli_session == "sess-1"


def test_collect_sessions_without_morcli_keeps_recorded_handle() -> None:
    live = _live("pan-a", "w1", "1-1", "/tmp/worktree", AgentStatus.WORKING)
    record = _record("t-1", "pan-a", "w1", "/tmp/worktree", morcli_session="sess-1")

    summaries = collect_sessions(FakeHerdr([live]), FakeThreadMap([record]), None)

    assert summaries[0].morcli_session == "sess-1"


@pytest.mark.parametrize(
    "pan_status, agent_status, expected",
    [
        (WorkerStatus.BLOCKED, AgentStatus.BLOCKED, False),
        (WorkerStatus.RUNNING, AgentStatus.WORKING, False),
        (WorkerStatus.RUNNING, AgentStatus.IDLE, False),
        (WorkerStatus.DONE, AgentStatus.DONE, False),
        (WorkerStatus.BLOCKED, AgentStatus.IDLE, True),
        (WorkerStatus.RUNNING, AgentStatus.DONE, True),
        (WorkerStatus.SPAWNING, AgentStatus.WORKING, False),
        (WorkerStatus.SPAWNING, AgentStatus.IDLE, False),
        (WorkerStatus.RUNNING, AgentStatus.UNKNOWN, False),
    ],
)
def test_session_drift(pan_status: WorkerStatus, agent_status: AgentStatus, expected: bool) -> None:
    assert session_drift(pan_status, agent_status) is expected
