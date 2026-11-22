from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from hc_fakes import FakeHc, make_terminal

from pan.errors import TargetBlockedError, TerminalNameTakenError
from pan.lifecycle import ORCHESTRATOR_BOOT_BRIEF, UpReport, bring_up, reconcile_lost_workers
from pan.models import AgentStatus, TerminalState, ThreadRecord, WorkerStatus
from pan.watcher import WAKE_INSTRUCTION

_NOW = datetime(2026, 11, 15, 9, 0, 0, tzinfo=UTC)
_CWD = Path("/Users/me/dev/pan")
_COMMAND = ["claude", "--permission-mode", "acceptEdits"]


def _record(thread_ts: str, address: str, status: WorkerStatus) -> ThreadRecord:
    return ThreadRecord(
        thread_ts=thread_ts,
        terminal_name=f"pan-{thread_ts}",
        terminal_address=address,
        channel="C1",
        worktree_path=Path(f"/wt/pan-{thread_ts}"),
        status=status,
        created_at=_NOW,
        updated_at=_NOW,
    )


class FakeThreadMap:
    def __init__(self, records: list[ThreadRecord]) -> None:
        self._records = {record.thread_ts: record for record in records}

    def records(self) -> list[ThreadRecord]:
        return list(self._records.values())

    def update_status(self, thread_ts: str, status: WorkerStatus) -> None:
        self._records[thread_ts].status = status

    def status_of(self, thread_ts: str) -> WorkerStatus:
        return self._records[thread_ts].status


def _bring_up(hc: FakeHc, thread_map: FakeThreadMap | None = None) -> UpReport:
    return bring_up(
        hc,
        thread_map if thread_map is not None else FakeThreadMap([]),
        terminal="pan-orchestrator",
        cwd=_CWD,
        command=_COMMAND,
    )


def test_bring_up_starts_a_missing_orchestrator_with_the_boot_brief() -> None:
    hc = FakeHc()

    report = _bring_up(hc)

    (run,) = hc.runs
    assert run.name == "pan-orchestrator"
    assert run.cwd == _CWD
    assert run.argv == [*_COMMAND, ORCHESTRATOR_BOOT_BRIEF]
    assert run.labels == {"pan.role": "orchestrator"}
    assert report.started is True
    assert report.orchestrator_address == "t101"
    # A freshly started orchestrator drains on its own via the boot brief; a send to a
    # booting agent could be dropped, so no wake is sent.
    assert hc.sent == []


def test_bring_up_leaves_a_healthy_orchestrator_and_wakes_it() -> None:
    hc = FakeHc()
    hc.add(make_terminal("t42", name="pan-orchestrator", agent="claude-code"))

    report = _bring_up(hc)

    assert hc.runs == []
    assert report.started is False
    assert report.orchestrator_address == "t42"
    assert hc.sent == [("t42", WAKE_INSTRUCTION)]


def test_bring_up_replaces_a_finished_orchestrator() -> None:
    hc = FakeHc()
    hc.add(
        make_terminal(
            "t42",
            name="pan-orchestrator",
            state=TerminalState.FINISHED,
            agent_status=AgentStatus.NONE,
        )
    )

    report = _bring_up(hc)

    assert hc.removed == ["t42"]
    assert hc.timeline[:2] == ["remove", "run"]
    assert report.started is True


def test_bring_up_refuses_a_live_terminal_that_is_not_claude() -> None:
    # A live terminal holding the name but running something else is not pan's to kill.
    hc = FakeHc()
    hc.add(make_terminal("t3", name="pan-orchestrator", agent="zsh", agent_status=AgentStatus.NONE))

    with pytest.raises(TerminalNameTakenError):
        _bring_up(hc)

    assert hc.removed == []
    assert hc.runs == []


def test_bring_up_tolerates_a_blocked_orchestrator_on_wake() -> None:
    hc = FakeHc(errors={"send": TargetBlockedError("blocked")})
    hc.add(make_terminal("t42", name="pan-orchestrator", agent="claude-code"))

    report = _bring_up(hc)

    assert report.woke is False


def test_reconcile_marks_only_gone_live_workers_failed() -> None:
    hc = FakeHc()
    hc.add(make_terminal("t20", agent_status=AgentStatus.WORKING))
    hc.add(make_terminal("t21", state=TerminalState.FINISHED))
    records = [
        _record("alive", "t20", WorkerStatus.RUNNING),
        _record("finished-terminal", "t21", WorkerStatus.BLOCKED),
        _record("missing-terminal", "t22", WorkerStatus.SPAWNING),
        _record("never-launched", "", WorkerStatus.RUNNING),
        _record("already-done", "t23", WorkerStatus.DONE),
    ]
    thread_map = FakeThreadMap(records)

    lost = reconcile_lost_workers(hc, thread_map)

    assert lost == ["finished-terminal", "missing-terminal", "never-launched"]
    assert thread_map.status_of("alive") is WorkerStatus.RUNNING
    assert thread_map.status_of("missing-terminal") is WorkerStatus.FAILED
    assert thread_map.status_of("already-done") is WorkerStatus.DONE


def test_bring_up_reports_lost_workers() -> None:
    hc = FakeHc()
    thread_map = FakeThreadMap([_record("gone", "t30", WorkerStatus.RUNNING)])

    report = _bring_up(hc, thread_map)

    assert report.lost_threads == ["gone"]
