from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pan.errors import HerdrError, MorcliError, SpawnError
from pan.models import ThreadRecord, WorkerStatus
from pan.spawn import ClaudeLauncher, spawn_worker

_FIXED_NOW = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)


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


class FakeHerdr:
    def __init__(self, timeline: list[str], *, fail: bool = False) -> None:
        self._timeline = timeline
        self._fail = fail
        self.sent: list[tuple[str, str]] = []
        self.nudged: list[str] = []

    def create_workspace(self, label: str, cwd: Path) -> tuple[str, str]:
        self._timeline.append("workspace")
        if self._fail:
            raise HerdrError("herdr failed")
        return "wsid", "pane1"

    def nudge(self, pane_id: str) -> None:
        self.nudged.append(pane_id)

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))

    def kill_pane(self, pane_id: str) -> None:  # pragma: no cover
        raise NotImplementedError


class FakeLauncher:
    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.calls: list[tuple[Path, str, str]] = []

    def launch(self, worktree: Path, pane_id: str, brief: str) -> None:
        self._timeline.append("launch")
        self.calls.append((worktree, pane_id, brief))


class FakeThreadMap:
    def __init__(self) -> None:
        self.records: dict[str, ThreadRecord] = {}

    def get(self, thread_ts: str) -> ThreadRecord | None:
        return self.records.get(thread_ts)

    def put(self, record: ThreadRecord) -> None:
        self.records[record.thread_ts] = record

    def update_status(self, thread_ts: str, status: WorkerStatus) -> None:  # pragma: no cover
        self.records[thread_ts].status = status


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


class FakeMorcli:
    def __init__(self, *, handle: str | None = "sess-wsid", fail: bool = False) -> None:
        self._handle = handle
        self._fail = fail
        self.resolved: list[str] = []

    def session_status(self, handle: str) -> WorkerStatus:  # pragma: no cover
        raise NotImplementedError

    def resolve_session(self, workspace_id: str) -> str | None:
        self.resolved.append(workspace_id)
        if self._fail:
            raise MorcliError("morcli streams failed")
        return self._handle


def _spawn(
    *,
    stream: str | None,
    base: Path,
    git: FakeGit,
    herdr: FakeHerdr,
    launcher: FakeLauncher,
    thread_map: FakeThreadMap,
    slack: FakeSlack,
    morcli: FakeMorcli | None = None,
) -> ThreadRecord:
    return spawn_worker(
        thread_ts="1718000000.000200",
        channel="C1",
        task="do the thing",
        repo=Path("/repo"),
        base=base,
        stream=stream,
        git=git,
        herdr=herdr,
        launcher=launcher,
        thread_map=thread_map,
        slack=slack,
        clock=FakeClock(),
        id_gen=FakeIdGen(),
        morcli=morcli if morcli is not None else FakeMorcli(),
    )


def test_spawn_worker_command_order_and_record(tmp_path: Path) -> None:
    timeline: list[str] = []
    git, herdr, launcher = FakeGit(timeline), FakeHerdr(timeline), FakeLauncher(timeline)
    thread_map, slack = FakeThreadMap(), FakeSlack()

    record = _spawn(
        stream="backend",
        base=tmp_path,
        git=git,
        herdr=herdr,
        launcher=launcher,
        thread_map=thread_map,
        slack=slack,
    )

    assert timeline == ["worktree", "workspace", "launch"]
    assert record.status is WorkerStatus.SPAWNING
    assert record.workspace_name == "pan-backend"
    assert record.workspace_id == "wsid"
    assert record.channel == "C1"
    assert record.pane_ids == ["pane1"]
    assert record.worktree_path == tmp_path / "pan-backend"
    assert record.created_at == _FIXED_NOW
    assert record.updated_at == _FIXED_NOW
    # The launched worker got the task brief.
    assert launcher.calls == [(tmp_path / "pan-backend", "pane1", "do the thing")]
    # The record is the sole thread->worker binding (INV-7).
    assert thread_map.records["1718000000.000200"] is record


def test_spawn_worker_captures_morcli_session_on_success(tmp_path: Path) -> None:
    timeline: list[str] = []
    thread_map = FakeThreadMap()
    morcli = FakeMorcli(handle="sess-live-1")

    record = _spawn(
        stream="backend",
        base=tmp_path,
        git=FakeGit(timeline),
        herdr=FakeHerdr(timeline),
        launcher=FakeLauncher(timeline),
        thread_map=thread_map,
        slack=FakeSlack(),
        morcli=morcli,
    )

    # The handle is resolved from the freshly created workspace id (R-7) and recorded
    # so the reconciled sessions view shows real morcli linkage.
    assert morcli.resolved == ["wsid"]
    assert record.morcli_session == "sess-live-1"
    assert thread_map.records["1718000000.000200"].morcli_session == "sess-live-1"


def test_spawn_worker_tolerates_unindexed_morcli_session(tmp_path: Path) -> None:
    # morcli lag: the just-spawned session is not indexed yet, so resolve_session
    # returns None. The spawn still succeeds; morcli_session is resolved later.
    timeline: list[str] = []

    record = _spawn(
        stream="backend",
        base=tmp_path,
        git=FakeGit(timeline),
        herdr=FakeHerdr(timeline),
        launcher=FakeLauncher(timeline),
        thread_map=FakeThreadMap(),
        slack=FakeSlack(),
        morcli=FakeMorcli(handle=None),
    )

    assert record.status is WorkerStatus.SPAWNING
    assert record.morcli_session is None


def test_spawn_worker_tolerates_morcli_resolve_failure(tmp_path: Path) -> None:
    # A morcli subprocess failure during the best-effort handle capture must not fail the
    # already-launched worker: the spawn still succeeds with morcli_session None.
    timeline: list[str] = []

    record = _spawn(
        stream="backend",
        base=tmp_path,
        git=FakeGit(timeline),
        herdr=FakeHerdr(timeline),
        launcher=FakeLauncher(timeline),
        thread_map=FakeThreadMap(),
        slack=FakeSlack(),
        morcli=FakeMorcli(fail=True),
    )

    assert record.status is WorkerStatus.SPAWNING
    assert record.morcli_session is None
    # The worker was still launched despite the morcli failure.
    assert timeline == ["worktree", "workspace", "launch"]


def test_spawn_worker_posts_ack_via_egress(tmp_path: Path) -> None:
    timeline: list[str] = []
    slack = FakeSlack()

    _spawn(
        stream="backend",
        base=tmp_path,
        git=FakeGit(timeline),
        herdr=FakeHerdr(timeline),
        launcher=FakeLauncher(timeline),
        thread_map=FakeThreadMap(),
        slack=slack,
    )

    assert slack.posts == [("C1", "1718000000.000200", "on it — stream pan-backend")]


def test_spawn_worker_without_stream_derives_label_from_id(tmp_path: Path) -> None:
    timeline: list[str] = []

    record = _spawn(
        stream=None,
        base=tmp_path,
        git=FakeGit(timeline),
        herdr=FakeHerdr(timeline),
        launcher=FakeLauncher(timeline),
        thread_map=FakeThreadMap(),
        slack=FakeSlack(),
    )

    assert record.workspace_name == "pan-abcd1234"


def test_spawn_writes_worker_claude_settings_with_hook_commands(tmp_path: Path) -> None:
    timeline: list[str] = []
    thread_map = FakeThreadMap()

    record = _spawn(
        stream="backend",
        base=tmp_path,
        git=FakeGit(timeline),
        herdr=FakeHerdr(timeline),
        launcher=FakeLauncher(timeline),
        thread_map=thread_map,
        slack=FakeSlack(),
    )

    settings_path = record.worktree_path / ".claude" / "settings.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    stop_command = settings["hooks"]["Stop"][0]["hooks"][0]
    notification_command = settings["hooks"]["Notification"][0]["hooks"][0]
    assert stop_command == {"type": "command", "command": "pan hook stop"}
    assert notification_command == {"type": "command", "command": "pan hook notification"}


def test_herdr_failure_wraps_as_spawn_error_and_marks_failed(tmp_path: Path) -> None:
    timeline: list[str] = []
    thread_map, slack = FakeThreadMap(), FakeSlack()

    with pytest.raises(SpawnError):
        _spawn(
            stream="backend",
            base=tmp_path,
            git=FakeGit(timeline),
            herdr=FakeHerdr(timeline, fail=True),
            launcher=FakeLauncher(timeline),
            thread_map=thread_map,
            slack=slack,
        )

    failed = thread_map.records["1718000000.000200"]
    assert failed.status is WorkerStatus.FAILED
    # The failed record also carries the channel so it stays a usable binding.
    assert failed.channel == "C1"
    # The failure is surfaced to the thread through the single egress path.
    assert len(slack.posts) == 1
    assert slack.posts[0][0:2] == ("C1", "1718000000.000200")


def test_git_failure_wraps_as_spawn_error_and_marks_failed(tmp_path: Path) -> None:
    timeline: list[str] = []
    thread_map = FakeThreadMap()

    with pytest.raises(SpawnError):
        _spawn(
            stream="backend",
            base=tmp_path,
            git=FakeGit(timeline, fail=True),
            herdr=FakeHerdr(timeline),
            launcher=FakeLauncher(timeline),
            thread_map=thread_map,
            slack=FakeSlack(),
        )

    assert thread_map.records["1718000000.000200"].status is WorkerStatus.FAILED
    # herdr workspace creation was never reached.
    assert timeline == ["worktree"]


def test_claude_launcher_sends_brief_then_submits() -> None:
    timeline: list[str] = []
    herdr = FakeHerdr(timeline)

    ClaudeLauncher(herdr).launch(Path("/base/pan-backend"), "pane1", "do the thing")

    assert herdr.sent == [("pane1", "claude 'do the thing'")]
    assert herdr.nudged == ["pane1"]


@pytest.mark.parametrize(
    "brief, expected_command",
    [
        ("rm -rf $HOME; echo pwned", "claude 'rm -rf $HOME; echo pwned'"),
        ("a'; rm -rf ~ #", "claude 'a'\"'\"'; rm -rf ~ #'"),
        ("$(id)", "claude '$(id)'"),
        ("`whoami`", "claude '`whoami`'"),
    ],
    ids=["semicolon", "single-quote-breakout", "command-substitution", "backticks"],
)
def test_claude_launcher_quotes_brief_safely(brief: str, expected_command: str) -> None:
    timeline: list[str] = []
    herdr = FakeHerdr(timeline)

    ClaudeLauncher(herdr).launch(Path("/wt"), "pane1", brief)

    # shlex.quote fully neutralizes shell metacharacters, including a single-quote
    # breakout attempt (which becomes the '"'"' idiom), so the brief can never run
    # as a shell command.
    assert herdr.sent == [("pane1", expected_command)]
