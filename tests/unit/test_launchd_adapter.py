from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from pan.core.errors import ServiceError
from pan.core.models import LaunchdJobState
from pan.data.adapters.launchd import LaunchctlServiceManager, parse_job_state
from pan.utils.config import LAUNCHD_BOOTSTRAP_ATTEMPTS

_UID = 501

_PRINT_RUNNING = """gui/501/com.pan.gateway = {
\tactive count = 1
\tpath = /Users/example/Library/LaunchAgents/com.pan.gateway.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /Users/example/.local/bin/pan
\targuments = {
\t\t/Users/example/.local/bin/pan
\t\tgateway
\t}

\tpid = 412
\timmediate reason = speculative
\tlast exit code = 0

\tendpoints = {
\t\tstate = active
\t}
}
"""

_PRINT_NEVER_EXITED = """gui/501/com.pan.watcher = {
\tstate = running
\tpid = 98
\tlast exit code = (never exited)
}
"""

_PRINT_NOT_RUNNING = """gui/501/com.pan.up = {
\tstate = not running
\tlast exit code = 78: EX_CONFIG
}
"""


@pytest.mark.parametrize(
    ("print_output", "expected_state"),
    [
        (_PRINT_RUNNING, LaunchdJobState(pid=412, last_exit_code=0, state="running")),
        (_PRINT_NEVER_EXITED, LaunchdJobState(pid=98, last_exit_code=None, state="running")),
        (_PRINT_NOT_RUNNING, LaunchdJobState(pid=None, last_exit_code=78, state="not running")),
    ],
    ids=["running", "never-exited", "not-running"],
)
def test_parse_job_state(print_output: str, expected_state: LaunchdJobState) -> None:
    assert parse_job_state(print_output) == expected_state


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, results: list[FakeCompleted]
) -> list[list[str]]:
    # Each launchctl call takes the next result; the last one repeats.
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> FakeCompleted:
        calls.append(command)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _manager(launch_agents_dir: Path, sleeps: list[float] | None = None) -> LaunchctlServiceManager:
    recorded_sleeps = sleeps if sleeps is not None else []
    return LaunchctlServiceManager(launch_agents_dir, uid=_UID, sleep=recorded_sleeps.append)


def test_job_state_prints_the_gui_domain_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_runner(monkeypatch, [FakeCompleted(stdout=_PRINT_RUNNING)])

    job_state = _manager(tmp_path).job_state("com.pan.gateway")

    assert calls == [["launchctl", "print", "gui/501/com.pan.gateway"]]
    assert job_state == LaunchdJobState(pid=412, last_exit_code=0, state="running")


def test_job_state_is_none_when_not_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_runner(monkeypatch, [FakeCompleted(returncode=113, stderr="Could not find service")])

    assert _manager(tmp_path).job_state("com.pan.gateway") is None


def test_bootstrap_loads_into_the_gui_domain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_runner(monkeypatch, [FakeCompleted()])
    plist_path = tmp_path / "com.pan.gateway.plist"

    _manager(tmp_path).bootstrap(plist_path)

    assert calls == [["launchctl", "bootstrap", "gui/501", str(plist_path)]]


def test_bootstrap_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _install_runner(
        monkeypatch,
        [
            FakeCompleted(returncode=5, stderr="Bootstrap failed: 5: Input/output error"),
            FakeCompleted(),
        ],
    )
    sleeps: list[float] = []

    _manager(tmp_path, sleeps).bootstrap(tmp_path / "com.pan.up.plist")

    assert len(calls) == 2
    assert len(sleeps) == 1


def test_bootstrap_raises_after_every_attempt_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _install_runner(
        monkeypatch, [FakeCompleted(returncode=5, stderr="Bootstrap failed: 5: Input/output error")]
    )

    with pytest.raises(ServiceError, match="Input/output error"):
        _manager(tmp_path).bootstrap(tmp_path / "com.pan.up.plist")

    assert len(calls) == LAUNCHD_BOOTSTRAP_ATTEMPTS


def test_bootout_removes_the_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = _install_runner(monkeypatch, [FakeCompleted()])

    _manager(tmp_path).bootout("com.pan.watcher")

    assert calls == [["launchctl", "bootout", "gui/501/com.pan.watcher"]]


@pytest.mark.parametrize(
    ("print_result", "expect_error"),
    [
        (FakeCompleted(returncode=113, stderr="Could not find service"), False),
        (FakeCompleted(stdout=_PRINT_RUNNING), True),
    ],
    ids=["already-gone", "still-loaded"],
)
def test_bootout_failure_is_ignored_only_when_the_job_is_gone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    print_result: FakeCompleted,
    expect_error: bool,
) -> None:
    _install_runner(
        monkeypatch, [FakeCompleted(returncode=3, stderr="Boot-out failed: 3"), print_result]
    )
    manager = _manager(tmp_path)

    if expect_error:
        with pytest.raises(ServiceError, match="bootout gui/501/com.pan.gateway"):
            manager.bootout("com.pan.gateway")
    else:
        manager.bootout("com.pan.gateway")


def test_missing_launchctl_raises_service_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def missing_launchctl(_command: list[str], **_kwargs: Any) -> FakeCompleted:
        raise FileNotFoundError("launchctl")

    monkeypatch.setattr(subprocess, "run", missing_launchctl)

    with pytest.raises(ServiceError, match="failed to run launchctl"):
        _manager(tmp_path).bootout("com.pan.up")


def test_plist_files_live_in_the_launch_agents_dir(tmp_path: Path) -> None:
    launch_agents_dir = tmp_path / "LaunchAgents"
    manager = _manager(launch_agents_dir)

    plist_path = manager.write_plist("com.pan.up", "<plist/>")

    assert plist_path == launch_agents_dir / "com.pan.up.plist"
    assert plist_path.read_text() == "<plist/>"
    assert manager.plist_installed("com.pan.up")
    assert manager.remove_plist("com.pan.up") is True
    assert manager.remove_plist("com.pan.up") is False
    assert not manager.plist_installed("com.pan.up")
