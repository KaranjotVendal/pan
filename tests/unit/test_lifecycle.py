from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.backend.lifecycle import UpReport, bring_up, reconcile_lost_workers
from pan.backend.orchestrating_skill import (
    SkillVersion,
    orchestrating_skill,
    orchestrator_boot_brief,
    wake_instruction,
)
from pan.core.errors import TargetBlockedError, TerminalNameTakenError
from pan.core.models import AgentStatus, TerminalState, ThreadRecord, WorkerStatus

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

    def update_status(
        self, thread_ts: str, status: WorkerStatus, *, session_name: str | None = None
    ) -> None:
        record = self._records[thread_ts]
        assert session_name == record.terminal_name
        record.status = status

    def status_of(self, thread_ts: str) -> WorkerStatus:
        return self._records[thread_ts].status


def _bring_up(terminal_host: FakeTerminalHost, thread_map: FakeThreadMap | None = None) -> UpReport:
    return bring_up(
        terminal_host,
        thread_map if thread_map is not None else FakeThreadMap([]),
        terminal="pan-orchestrator",
        cwd=_CWD,
        command=_COMMAND,
    )


def test_bring_up_starts_a_missing_orchestrator_with_the_boot_brief() -> None:
    terminal_host = FakeTerminalHost()

    report = _bring_up(terminal_host)

    (run,) = terminal_host.runs
    assert run.name == "pan-orchestrator"
    assert run.cwd == _CWD
    assert run.argv == [*_COMMAND, orchestrator_boot_brief(orchestrating_skill())]
    assert run.labels == {"pan.role": "orchestrator"}
    assert report.started is True
    assert report.orchestrator_address == "t101"
    # A freshly started orchestrator drains on its own via the boot brief; a send to a
    # booting agent could be dropped, so no wake is sent.
    assert terminal_host.sent == []


def test_bring_up_leaves_a_healthy_orchestrator_and_wakes_it() -> None:
    terminal_host = FakeTerminalHost()
    terminal_host.add(make_terminal("t42", name="pan-orchestrator", agent="claude-code"))

    report = _bring_up(terminal_host)

    assert terminal_host.runs == []
    assert report.started is False
    assert report.orchestrator_address == "t42"
    assert terminal_host.sent == [("t42", wake_instruction(orchestrating_skill()))]


def _make_skill_unreadable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SkillVersion:
    missing_skill = tmp_path / "SKILL.md"
    monkeypatch.setattr(
        "pan.backend.lifecycle.orchestrating_skill", lambda: orchestrating_skill(missing_skill)
    )
    return SkillVersion(path=missing_skill, version=None)


def test_bring_up_starts_with_a_versionless_boot_brief_when_the_skill_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skill = _make_skill_unreadable(monkeypatch, tmp_path)
    terminal_host = FakeTerminalHost()
    terminal_host.add(
        make_terminal(
            "t42",
            name="pan-orchestrator",
            state=TerminalState.FINISHED,
            agent_status=AgentStatus.NONE,
        )
    )

    report = _bring_up(terminal_host)

    (run,) = terminal_host.runs
    assert run.argv == [*_COMMAND, orchestrator_boot_brief(skill)]
    assert str(skill.path) in run.argv[-1]
    assert "version" not in run.argv[-1]
    assert report.started is True


def test_bring_up_wakes_with_a_versionless_wake_when_the_skill_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skill = _make_skill_unreadable(monkeypatch, tmp_path)
    terminal_host = FakeTerminalHost()
    terminal_host.add(make_terminal("t42", name="pan-orchestrator", agent="claude-code"))

    report = _bring_up(terminal_host)

    assert terminal_host.sent == [("t42", wake_instruction(skill))]
    assert report.woke is True


def test_bring_up_replaces_a_finished_orchestrator() -> None:
    terminal_host = FakeTerminalHost()
    terminal_host.add(
        make_terminal(
            "t42",
            name="pan-orchestrator",
            state=TerminalState.FINISHED,
            agent_status=AgentStatus.NONE,
        )
    )

    report = _bring_up(terminal_host)

    assert terminal_host.removed == ["t42"]
    assert terminal_host.timeline[:2] == ["remove", "run"]
    assert report.started is True


def test_bring_up_refuses_a_live_terminal_that_is_not_claude() -> None:
    # A live terminal holding the name but running something else is not pan's to kill.
    terminal_host = FakeTerminalHost()
    terminal_host.add(
        make_terminal("t3", name="pan-orchestrator", agent="zsh", agent_status=AgentStatus.NONE)
    )

    with pytest.raises(TerminalNameTakenError):
        _bring_up(terminal_host)

    assert terminal_host.removed == []
    assert terminal_host.runs == []


def test_bring_up_tolerates_a_blocked_orchestrator_on_wake() -> None:
    terminal_host = FakeTerminalHost(errors={"send": TargetBlockedError("blocked")})
    terminal_host.add(make_terminal("t42", name="pan-orchestrator", agent="claude-code"))

    report = _bring_up(terminal_host)

    assert report.woke is False


def test_reconcile_marks_only_gone_live_workers_failed() -> None:
    terminal_host = FakeTerminalHost()
    terminal_host.add(make_terminal("t20", agent_status=AgentStatus.WORKING))
    terminal_host.add(make_terminal("t21", state=TerminalState.FINISHED))
    records = [
        _record("alive", "t20", WorkerStatus.RUNNING),
        _record("finished-terminal", "t21", WorkerStatus.BLOCKED),
        _record("missing-terminal", "t22", WorkerStatus.SPAWNING),
        _record("never-launched", "", WorkerStatus.RUNNING),
        _record("already-done", "t23", WorkerStatus.DONE),
        _record("idle-gone", "t24", WorkerStatus.IDLE),
    ]
    thread_map = FakeThreadMap(records)

    lost = reconcile_lost_workers(terminal_host, thread_map)

    assert lost == ["finished-terminal", "missing-terminal", "never-launched", "idle-gone"]
    assert thread_map.status_of("alive") is WorkerStatus.RUNNING
    assert thread_map.status_of("missing-terminal") is WorkerStatus.FAILED
    assert thread_map.status_of("already-done") is WorkerStatus.DONE


def test_bring_up_reports_lost_workers() -> None:
    terminal_host = FakeTerminalHost()
    thread_map = FakeThreadMap([_record("gone", "t30", WorkerStatus.RUNNING)])

    report = _bring_up(terminal_host, thread_map)

    assert report.lost_threads == ["gone"]
