from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.adapters.hc import HcError
from pan.errors import SpawnError, TerminalHostError, TerminalNameTakenError
from pan.models import TerminalState, ThreadRecord, WorkerStatus
from pan.spawn import ClaudeLauncher, reopen_worker, spawn_worker, worker_terminal_name

_FIXED_NOW = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
_THREAD = "1718000000.000200"


class FakeGit:
    def __init__(self, timeline: list[str], *, fail: bool = False) -> None:
        self._timeline = timeline
        self._fail = fail

    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path:
        self._timeline.append("worktree")
        if self._fail:
            raise SpawnError("git failed")
        return base / branch

    def remove_worktree(self, path: Path) -> None:  # pragma: no cover
        raise NotImplementedError


class FakeThreadMap:
    def __init__(self) -> None:
        self.records: dict[str, ThreadRecord] = {}

    def get(self, thread_ts: str) -> ThreadRecord | None:
        return self.records.get(thread_ts)

    def put(self, record: ThreadRecord) -> None:
        self.records[record.thread_ts] = record


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        self.posts.append((channel, thread_ts, text))

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError


class FakeClock:
    def now(self) -> datetime:
        return _FIXED_NOW


class FakeIdGen:
    def new_id(self) -> str:
        return "abcd1234-ffff-0000-1111-222233334444"


def _spawn(
    *,
    base: Path,
    terminal_host: FakeTerminalHost,
    git: FakeGit,
    thread_map: FakeThreadMap | None = None,
    slack: FakeSlack | None = None,
    stream: str | None = "backend",
    app_space: str | None = None,
) -> ThreadRecord:
    return spawn_worker(
        thread_ts=_THREAD,
        channel="C1",
        task="do the thing",
        repo=Path("/repo"),
        base=base,
        stream=stream,
        app_space=app_space,
        git=git,
        terminal_host=terminal_host,
        launcher=ClaudeLauncher(terminal_host),
        thread_map=thread_map if thread_map is not None else FakeThreadMap(),
        slack=slack if slack is not None else FakeSlack(),
        clock=FakeClock(),
        id_gen=FakeIdGen(),
    )


def test_spawn_worker_runs_claude_in_a_labelled_terminal_and_records_it(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    git = FakeGit(terminal_host.timeline)
    thread_map, slack = FakeThreadMap(), FakeSlack()

    record = _spawn(
        base=tmp_path, terminal_host=terminal_host, git=git, thread_map=thread_map, slack=slack
    )

    assert terminal_host.timeline == ["worktree", "run"]
    (run,) = terminal_host.runs
    assert run.name == "pan-backend"
    assert run.cwd == tmp_path / "pan-backend"
    # The brief is one argv element after `claude`: never parsed as shell text.
    assert run.argv == ["claude", "do the thing"]
    assert run.labels == {"pan.role": "worker", "pan.thread": _THREAD, "pan.channel": "C1"}
    assert record.status is WorkerStatus.SPAWNING
    assert record.terminal_name == "pan-backend"
    assert record.terminal_address == "t101"
    assert record.channel == "C1"
    assert record.worktree_path == tmp_path / "pan-backend"
    assert record.created_at == _FIXED_NOW
    # The record is the sole thread->worker binding (INV-7) and the ack goes via egress.
    assert thread_map.records[_THREAD] is record
    assert slack.posts == [("C1", _THREAD, "on it — stream pan-backend")]


def test_spawn_worker_labels_into_the_app_space_only_when_configured(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()

    _spawn(
        base=tmp_path,
        terminal_host=terminal_host,
        git=FakeGit(terminal_host.timeline),
        app_space="sp42",
    )

    assert terminal_host.runs[0].labels["herdr.space"] == "sp42"


@pytest.mark.parametrize(
    "stream, expected_name",
    [
        ("backend", "pan-backend"),
        ("Fix Login.Bug", "pan-fix-login-bug"),
        ("--weird--", "pan-weird"),
        ("a" * 40, "pan-" + "a" * 28),
        ("...", "pan-abcd1234"),
        (None, "pan-abcd1234"),
    ],
)
def test_worker_terminal_name_follows_hc_name_rules(stream: str | None, expected_name: str) -> None:
    name = worker_terminal_name(stream, FakeIdGen())

    assert name == expected_name
    assert len(name) <= 32


def test_spawn_writes_worker_claude_settings_with_hook_commands(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()

    record = _spawn(base=tmp_path, terminal_host=terminal_host, git=FakeGit(terminal_host.timeline))

    settings = json.loads((record.worktree_path / ".claude" / "settings.json").read_text())
    commands = {
        event: settings["hooks"][event][0]["hooks"][0]["command"]
        for event in ("SessionStart", "Stop", "Notification")
    }
    assert commands == {
        "SessionStart": "pan hook session-start",
        "Stop": "pan hook stop",
        "Notification": "pan hook notification",
    }


def test_a_live_terminal_holding_the_name_fails_before_any_worktree(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    terminal_host.add(make_terminal("t5", name="pan-backend"))
    thread_map, slack = FakeThreadMap(), FakeSlack()

    with pytest.raises(SpawnError, match="stream name in use"):
        _spawn(
            base=tmp_path,
            terminal_host=terminal_host,
            git=FakeGit(terminal_host.timeline),
            thread_map=thread_map,
            slack=slack,
        )

    assert terminal_host.timeline == []
    assert thread_map.records[_THREAD].status is WorkerStatus.FAILED
    assert slack.posts == [
        ("C1", _THREAD, "spawn failed for stream pan-backend: stream name in use")
    ]


def test_a_finished_terminal_with_the_name_does_not_block_spawn(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    terminal_host.add(make_terminal("t5", name="pan-backend", state=TerminalState.FINISHED))

    record = _spawn(base=tmp_path, terminal_host=terminal_host, git=FakeGit(terminal_host.timeline))

    assert record.status is WorkerStatus.SPAWNING


@pytest.mark.parametrize(
    "run_error",
    [TerminalHostError("spawn_failed"), HcError("spawn_failed"), TerminalNameTakenError("raced")],
)
def test_terminal_host_failure_wraps_as_spawn_error_and_marks_failed(
    tmp_path: Path, run_error: TerminalHostError
) -> None:
    terminal_host = FakeTerminalHost(errors={"run": run_error})
    thread_map, slack = FakeThreadMap(), FakeSlack()

    with pytest.raises(SpawnError):
        _spawn(
            base=tmp_path,
            terminal_host=terminal_host,
            git=FakeGit(terminal_host.timeline),
            thread_map=thread_map,
            slack=slack,
        )

    failed = thread_map.records[_THREAD]
    assert failed.status is WorkerStatus.FAILED
    assert failed.terminal_address == ""
    assert failed.channel == "C1"
    assert len(slack.posts) == 1
    assert slack.posts[0][0:2] == ("C1", _THREAD)


def test_git_failure_wraps_as_spawn_error_and_never_runs(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    thread_map = FakeThreadMap()

    with pytest.raises(SpawnError):
        _spawn(
            base=tmp_path,
            terminal_host=terminal_host,
            git=FakeGit(terminal_host.timeline, fail=True),
            thread_map=thread_map,
        )

    assert thread_map.records[_THREAD].status is WorkerStatus.FAILED
    assert terminal_host.timeline == ["worktree"]


def _finished_record(
    worktree_path: Path, status: WorkerStatus = WorkerStatus.FAILED
) -> ThreadRecord:
    return ThreadRecord(
        thread_ts=_THREAD,
        terminal_name="pan-backend",
        terminal_address="t50",
        channel="C1",
        worktree_path=worktree_path,
        claude_session_id="s-1",
        status=status,
        created_at=_FIXED_NOW,
        updated_at=_FIXED_NOW,
    )


class LaterClock:
    def now(self) -> datetime:
        return datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _reopen(
    record: ThreadRecord,
    terminal_host: FakeTerminalHost,
    thread_map: FakeThreadMap,
    slack: FakeSlack,
) -> ThreadRecord:
    return reopen_worker(
        record=record,
        task="also update the docs",
        app_space=None,
        terminal_host=terminal_host,
        launcher=ClaudeLauncher(terminal_host),
        thread_map=thread_map,
        slack=slack,
        clock=LaterClock(),
    )


@pytest.mark.parametrize("status", [WorkerStatus.FAILED, WorkerStatus.DONE])
def test_reopen_continues_the_conversation_in_the_same_worktree(
    tmp_path: Path, status: WorkerStatus
) -> None:
    terminal_host, thread_map, slack = FakeTerminalHost(), FakeThreadMap(), FakeSlack()
    record = _finished_record(tmp_path, status)

    reopened = _reopen(record, terminal_host, thread_map, slack)

    (run,) = terminal_host.runs
    assert run.name == "pan-backend"
    assert run.cwd == tmp_path
    assert run.argv == ["claude", "--continue", "also update the docs"]
    assert run.labels["pan.thread"] == _THREAD
    assert reopened.terminal_address == "t101"
    assert reopened.status is WorkerStatus.SPAWNING
    assert reopened.claude_session_id == "s-1"
    assert reopened.created_at == _FIXED_NOW
    assert thread_map.records[_THREAD] is reopened
    assert slack.posts == [("C1", _THREAD, "reopened — stream pan-backend")]


@pytest.mark.parametrize(
    "missing_worktree, live_name_holder, expected_notice",
    [
        (True, False, "reopen failed for stream pan-backend"),
        (False, True, "reopen failed for stream pan-backend: stream name in use"),
    ],
    ids=["worktree-missing", "name-in-use"],
)
def test_reopen_failure_posts_and_leaves_the_record(
    tmp_path: Path, missing_worktree: bool, live_name_holder: bool, expected_notice: str
) -> None:
    terminal_host, thread_map, slack = FakeTerminalHost(), FakeThreadMap(), FakeSlack()
    if live_name_holder:
        terminal_host.add(make_terminal("t5", name="pan-backend"))
    worktree = tmp_path / "gone" if missing_worktree else tmp_path
    record = _finished_record(worktree)

    with pytest.raises(SpawnError):
        _reopen(record, terminal_host, thread_map, slack)

    assert terminal_host.runs == []
    assert thread_map.records == {}
    assert slack.posts == [("C1", _THREAD, expected_notice)]
