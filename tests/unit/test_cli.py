from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hc_fakes import FakeHc, make_terminal
from pydantic import SecretStr
from typer.testing import CliRunner

import pan.cli as cli
from pan.errors import (
    ConfigMissingError,
    CredentialsError,
    GatedOpDeniedError,
    HcError,
    InboxError,
    PanError,
    SlackPostError,
    SpawnError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
    TerminalGoneError,
    TerminalNameTakenError,
    ThreadNotFoundError,
    UnauthorizedSenderError,
)
from pan.hooks.notification import notification_hook
from pan.hooks.stop import stop_hook
from pan.inbox import FileInboxStore
from pan.models import (
    AgentStatus,
    InboxItem,
    PanConfig,
    SessionSummary,
    SlackCredentials,
    ThreadRecord,
    WorkerStatus,
)
from pan.threadmap import FileThreadMap

runner = CliRunner()

FAKE_BOT_TOKEN = "xoxb-fake-cli-bottoken"
FAKE_APP_TOKEN = "xapp-fake-cli-apptoken"


def _make_config(tmp_path: Path) -> PanConfig:
    return PanConfig.model_validate(
        {
            "orchestrator": {
                "cwd": str(tmp_path / "pan"),
                "worktree_base": str(tmp_path / "wt"),
                "app_space": "sp42",
            },
            "defaults": {},
            "paths": {
                "inbox": str(tmp_path / "inbox"),
                "threads": str(tmp_path / "threads.json"),
                "logs": str(tmp_path / "logs"),
                "credentials": str(tmp_path / "creds.json"),
            },
        }
    )


def _item(event_id: str) -> InboxItem:
    return InboxItem(
        id=event_id,
        slack_user="U1",
        channel="C1",
        thread_ts="1718000000.000200",
        is_thread_reply=False,
        raw_text="do it",
        received_at=datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC),
    )


def _record(
    thread_ts: str, worktree_path: Path = Path("/tmp/wt"), terminal_address: str = "t12"
) -> ThreadRecord:
    now = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    return ThreadRecord(
        thread_ts=thread_ts,
        terminal_name="pan-task",
        terminal_address=terminal_address,
        channel="C1",
        worktree_path=worktree_path,
        created_at=now,
        updated_at=now,
    )


def test_inbox_drain_json_emits_items(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    store = FileInboxStore(config.paths.inbox)
    store.append(_item("Ev1"))

    result = runner.invoke(cli.app, ["inbox", "drain", "--json"])

    assert result.exit_code == 0
    emitted = json.loads(result.stdout)
    assert [entry["item"]["id"] for entry in emitted] == ["Ev1"]
    # The deterministically-parsed directive travels with each item (INV-3).
    assert emitted[0]["directive"]["mode"] == "delegate"
    assert emitted[0]["directive"]["cleaned_text"] == "do it"
    # Drain emptied the store.
    assert FileInboxStore(config.paths.inbox).drain() == []


def test_threads_get_prints_record_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    FileThreadMap(config.paths.threads, cli.SystemClock()).put(_record("t-1"))

    result = runner.invoke(cli.app, ["threads", "get", "--thread", "t-1"])

    assert result.exit_code == 0
    record = json.loads(result.stdout)
    assert record["thread_ts"] == "t-1"
    assert record["terminal_address"] == "t12"


def test_threads_get_unknown_prints_null(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)

    result = runner.invoke(cli.app, ["threads", "get", "--thread", "missing"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "null"


def test_spawn_wires_to_spawn_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        cli,
        "load_credentials",
        lambda path: SlackCredentials(
            bot_token=SecretStr(FAKE_BOT_TOKEN), app_token=SecretStr(FAKE_APP_TOKEN)
        ),
    )
    captured: dict[str, Any] = {}

    def fake_spawn_worker(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(cli, "spawn_worker", fake_spawn_worker)

    result = runner.invoke(
        cli.app,
        [
            "spawn",
            "--thread",
            "t-1",
            "--task",
            "build it",
            "--channel",
            "C1",
            "--stream",
            "backend",
        ],
    )

    assert result.exit_code == 0
    assert captured["thread_ts"] == "t-1"
    assert captured["task"] == "build it"
    assert captured["channel"] == "C1"
    assert captured["stream"] == "backend"
    assert captured["base"] == config.orchestrator.worktree_base
    assert captured["app_space"] == "sp42"
    assert isinstance(captured["hc"], cli.ShellHcAdapter)


def test_watcher_builds_from_config_and_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    hc_sentinel = object()
    monkeypatch.setattr(cli, "ShellHcAdapter", lambda: hc_sentinel)
    captured: dict[str, Any] = {}

    class FakeWatcher:
        def __init__(self, hc: Any, orchestrator_terminal: str, inbox_dir: Path) -> None:
            captured["args"] = (hc, orchestrator_terminal, inbox_dir)

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(cli, "WatchdogInboxWatcher", FakeWatcher)

    result = runner.invoke(cli.app, ["watcher"])

    assert result.exit_code == 0
    # Built from config: the hc adapter, the orchestrator terminal name, the inbox dir.
    assert captured["args"] == (hc_sentinel, "pan-orchestrator", config.paths.inbox)
    assert captured["started"] is True


def test_config_show_masks_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(
        cli,
        "load_credentials",
        lambda path: SlackCredentials(
            bot_token=SecretStr(FAKE_BOT_TOKEN), app_token=SecretStr(FAKE_APP_TOKEN)
        ),
    )

    result = runner.invoke(cli.app, ["config", "show"])

    assert result.exit_code == 0
    assert FAKE_BOT_TOKEN not in result.stdout
    assert FAKE_APP_TOKEN not in result.stdout
    assert "**********" in result.stdout


def _use_hc(monkeypatch: pytest.MonkeyPatch, hc: FakeHc) -> FakeHc:
    monkeypatch.setattr(cli, "ShellHcAdapter", lambda: hc)
    return hc


def _use_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> PanConfig:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    return config


@pytest.mark.parametrize(
    "terminals, terminal_address, expected",
    [
        ([make_terminal("t12", agent_status=AgentStatus.BLOCKED)], "t12", "blocked"),
        ([make_terminal("t12", agent_status=AgentStatus.WORKING)], "t12", "running"),
        ([], "t12", "failed"),
        ([], "", "failed"),
    ],
    ids=["blocked", "working", "terminal-gone", "never-launched"],
)
def test_status_reads_the_recorded_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    terminals: list[Any],
    terminal_address: str,
    expected: str,
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    record = _record("t-1", terminal_address=terminal_address)
    record.status = WorkerStatus.RUNNING
    FileThreadMap(config.paths.threads, cli.SystemClock()).put(record)
    hc = FakeHc()
    for terminal in terminals:
        hc.add(terminal)
    _use_hc(monkeypatch, hc)

    result = runner.invoke(cli.app, ["status", "--thread", "t-1"])

    assert result.exit_code == 0
    assert result.stdout.strip() == expected


def _summary(*, is_pan_owned: bool = True, drift: bool = False) -> SessionSummary:
    return SessionSummary(
        address="t12",
        name="pan-a" if is_pan_owned else None,
        where=None if is_pan_owned else "my-app / 1",
        cwd=Path("/tmp/worktree"),
        agent="claude-code",
        agent_status=AgentStatus.WORKING,
        thread_ts="t-1" if is_pan_owned else None,
        pan_status=WorkerStatus.RUNNING if is_pan_owned else None,
        is_pan_owned=is_pan_owned,
        drift=drift,
    )


def test_sessions_json_emits_summary_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "collect_sessions", lambda hc, thread_map: [_summary(drift=True)])

    result = runner.invoke(cli.app, ["sessions", "--json"])

    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data[0]["address"] == "t12"
    assert data[0]["name"] == "pan-a"
    assert data[0]["agent_status"] == "working"
    assert data[0]["is_pan_owned"] is True
    assert data[0]["drift"] is True
    assert data[0]["thread_ts"] == "t-1"


def test_sessions_human_table_renders(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cli,
        "collect_sessions",
        lambda hc, thread_map: [_summary(drift=True), _summary(is_pan_owned=False)],
    )

    result = runner.invoke(cli.app, ["sessions"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["TERMINAL", "NAME", "AGENT", "OWNER", "PAN_STATUS", "DRIFT"]
    assert lines[1].split() == ["t12", "pan-a", "working", "pan", "running", "DRIFT"]
    assert "my-app / 1" in lines[2]
    assert "external" in lines[2]


def _live_hc() -> FakeHc:
    hc = FakeHc()
    hc.add(make_terminal("t3", where="web / codex"))
    hc.add(make_terminal("t4", where="web / review"))
    hc.add(make_terminal("t5", name="pan-backend"))
    return hc


def test_relay_echoes_resolved_name_and_address(monkeypatch: pytest.MonkeyPatch) -> None:
    hc = _use_hc(monkeypatch, _live_hc())

    result = runner.invoke(cli.app, ["relay", "pan-backend", "re-run the failing test"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "relayed into pan-backend terminal t5"
    assert hc.sent == [("t5", "re-run the failing test")]


@pytest.mark.parametrize(
    "selector, hc_errors, expected_error",
    [
        ("nope", {}, TargetNotFoundError),
        ("web", {}, TargetAmbiguousError),
        ("t5", {"send": TargetBlockedError("blocked")}, TargetBlockedError),
    ],
)
def test_relay_failures_propagate_typed_errors(
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
    hc_errors: dict[str, Any],
    expected_error: type[PanError],
) -> None:
    hc = _live_hc()
    hc.errors.update(hc_errors)
    _use_hc(monkeypatch, hc)

    result = runner.invoke(cli.app, ["relay", selector, "hi"])

    assert isinstance(result.exception, expected_error)
    assert hc.sent == []


def test_read_recent_echoes_raw_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_config(monkeypatch, tmp_path)
    hc = _use_hc(monkeypatch, _live_hc())

    result = runner.invoke(cli.app, ["read", "pan-backend"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "recent output"
    assert hc.reads == [("t5", cli.READ_RECENT_LINES)]


def test_read_full_echoes_the_recorded_transcript(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type": "assistant", "message": {"content": "all done"}}\n')
    record = _record("t-1", terminal_address="t5")
    record.transcript_path = transcript
    FileThreadMap(config.paths.threads, cli.SystemClock()).put(record)
    hc = _use_hc(monkeypatch, _live_hc())

    result = runner.invoke(cli.app, ["read", "pan-backend", "--full"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "assistant: all done"
    assert hc.reads == []


def test_read_not_found_propagates_target_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    hc = _use_hc(monkeypatch, _live_hc())

    result = runner.invoke(cli.app, ["read", "nope"])

    assert isinstance(result.exception, TargetNotFoundError)
    assert hc.reads == []


@pytest.mark.parametrize("terminal_address", ["t12", ""], ids=["recorded", "never-launched"])
def test_stop_removes_the_terminal_and_marks_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, terminal_address: str
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.put(_record("t-1", terminal_address=terminal_address))
    hc = _use_hc(monkeypatch, FakeHc())

    result = runner.invoke(cli.app, ["stop", "--thread", "t-1"])

    assert result.exit_code == 0
    assert hc.removed == ([terminal_address] if terminal_address else [])
    stopped = thread_map.get("t-1")
    assert stopped is not None
    assert stopped.status is WorkerStatus.FAILED


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (UnauthorizedSenderError("x"), 10),
        (ConfigMissingError("x"), 11),
        (CredentialsError("x"), 12),
        (InboxError("x"), 13),
        (ThreadNotFoundError("x"), 14),
        (SpawnError("x"), 15),
        (HcError("x"), 16),
        (SlackPostError("x"), 17),
        (GatedOpDeniedError("x"), 18),
        (TargetNotFoundError("x"), 20),
        (TargetAmbiguousError("x", []), 21),
        (TerminalGoneError("x"), 22),
        (TargetBlockedError("x"), 23),
        (TerminalNameTakenError("x"), 24),
        (PanError("x"), 1),
    ],
)
def test_exit_code_for(error: PanError, expected_code: int) -> None:
    assert cli._exit_code_for(error) == expected_code


def test_run_maps_pan_error_to_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_kwargs: Any) -> None:
        raise SpawnError("kaboom")

    monkeypatch.setattr(cli, "app", boom)

    assert cli._run() == 15


class _HookThreadMap:
    def __init__(self, record: ThreadRecord) -> None:
        self._record = record
        self.status_updates: list[tuple[str, WorkerStatus]] = []

    def get(self, thread_ts: str) -> ThreadRecord | None:
        return self._record if thread_ts == self._record.thread_ts else None

    def get_by_worktree(self, worktree_path: Path) -> ThreadRecord | None:
        return self._record if worktree_path == self._record.worktree_path else None

    def put(self, record: ThreadRecord) -> None:  # pragma: no cover
        raise NotImplementedError

    def update_status(self, thread_ts: str, status: WorkerStatus) -> None:
        self.status_updates.append((thread_ts, status))


class _HookSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        self.posts.append((channel, thread_ts, text))

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError


def _hook_payload(cwd: str, event: str) -> str:
    return json.dumps({"hook_event_name": event, "cwd": cwd, "transcript_path": None})


def test_hook_stop_resolves_by_cwd_posts_and_marks_done() -> None:
    record = _record("t-1", worktree_path=Path("/wt/pan-a"))
    thread_map, slack = _HookThreadMap(record), _HookSlack()

    cli._dispatch_completion_hook(stop_hook, _hook_payload("/wt/pan-a", "Stop"), thread_map, slack)

    assert slack.posts[0][0:2] == ("C1", "t-1")
    assert thread_map.status_updates == [("t-1", WorkerStatus.DONE)]


def test_hook_notification_resolves_by_cwd_posts_and_marks_blocked() -> None:
    record = _record("t-1", worktree_path=Path("/wt/pan-a"))
    thread_map, slack = _HookThreadMap(record), _HookSlack()
    payload = json.dumps(
        {"hook_event_name": "Notification", "cwd": "/wt/pan-a", "message": "need input"}
    )

    cli._dispatch_completion_hook(notification_hook, payload, thread_map, slack)

    assert slack.posts == [("C1", "t-1", "need input")]
    assert thread_map.status_updates == [("t-1", WorkerStatus.BLOCKED)]


@pytest.mark.parametrize(
    "raw_stdin",
    ['{"hook_event_name": "Stop", "cwd": "/wt/other", "transcript_path": null}', "not json"],
    ids=["cwd-matches-no-record", "unparseable-stdin"],
)
def test_hook_exits_cleanly_without_posting(raw_stdin: str) -> None:
    record = _record("t-1", worktree_path=Path("/wt/pan-a"))
    thread_map, slack = _HookThreadMap(record), _HookSlack()

    cli._dispatch_completion_hook(stop_hook, raw_stdin, thread_map, slack)

    assert slack.posts == []
    assert thread_map.status_updates == []


def test_hook_subcommands_are_registered() -> None:
    result = runner.invoke(cli.app, ["hook", "--help"])

    assert result.exit_code == 0
    assert "stop" in result.stdout
    assert "notification" in result.stdout
    assert "session-start" in result.stdout


def test_hook_session_start_records_the_session_on_the_cwd_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.put(_record("t-1", worktree_path=tmp_path / "wt" / "pan-task"))
    payload = json.dumps(
        {
            "hook_event_name": "SessionStart",
            "cwd": str(tmp_path / "wt" / "pan-task"),
            "session_id": "s-9",
            "transcript_path": "/home/.claude/projects/x/s-9.jsonl",
        }
    )

    result = runner.invoke(cli.app, ["hook", "session-start"], input=payload)

    assert result.exit_code == 0
    recorded = thread_map.get("t-1")
    assert recorded is not None
    assert recorded.claude_session_id == "s-9"
    assert recorded.transcript_path == Path("/home/.claude/projects/x/s-9.jsonl")


def test_up_starts_the_orchestrator_from_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    FileThreadMap(config.paths.threads, cli.SystemClock()).put(
        _record("t-1", terminal_address="t9")
    )
    hc = _use_hc(monkeypatch, FakeHc())

    result = runner.invoke(cli.app, ["up"])

    assert result.exit_code == 0
    (run,) = hc.runs
    assert run.name == "pan-orchestrator"
    assert run.cwd == config.orchestrator.cwd
    assert run.argv[0] == "claude"
    assert result.stdout.splitlines() == [
        "orchestrator pan-orchestrator started at t101",
        "marked 1 lost worker(s) failed: t-1",
    ]


def test_reopen_wires_the_record_to_reopen_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    FileThreadMap(config.paths.threads, cli.SystemClock()).put(_record("t-1"))
    monkeypatch.setattr(
        cli,
        "load_credentials",
        lambda path: SlackCredentials(
            bot_token=SecretStr(FAKE_BOT_TOKEN), app_token=SecretStr(FAKE_APP_TOKEN)
        ),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "reopen_worker", lambda **kwargs: captured.update(kwargs))

    result = runner.invoke(cli.app, ["reopen", "--thread", "t-1", "--task", "one more thing"])

    assert result.exit_code == 0
    assert captured["record"].thread_ts == "t-1"
    assert captured["task"] == "one more thing"
    assert captured["app_space"] == "sp42"


def test_reopen_unknown_thread_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_config(monkeypatch, tmp_path)

    result = runner.invoke(cli.app, ["reopen", "--thread", "nope", "--task", "x"])

    assert isinstance(result.exception, ThreadNotFoundError)
