from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from git_worktree_fakes import FakeGitWorktree
from inbox_fakes import FakeInboxDirectory
from pydantic import SecretStr
from service_manager_fakes import FakeServiceManager
from slack_fakes import RecordingSlack
from terminal_host_fakes import FakeTerminalHost, make_terminal
from typer.testing import CliRunner

import pan.cli as cli
from pan.adapters.hc import HcError, ShellHcAdapter
from pan.confirmations import FilePendingConfirmations, confirmations_path
from pan.errors import (
    ConfigMissingError,
    CredentialsError,
    DiscardNotRequestedError,
    GatedOpDeniedError,
    GitWorktreeError,
    InboxError,
    NoExactMatchError,
    NotPanSessionError,
    PanError,
    PrivateMessageError,
    ServiceError,
    SessionBusyError,
    SlackPostError,
    SpawnError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
    TerminalGoneError,
    TerminalHostError,
    TerminalNameTakenError,
    ThreadNotFoundError,
    UnansweredQuestionError,
    UnauthorizedSenderError,
    UnsafeWorktreeError,
    UnsavedWorkError,
)
from pan.hooks.notification import notification_hook
from pan.hooks.stop import stop_hook
from pan.inbox import FileInboxStore
from pan.models import (
    AgentStatus,
    BranchStatus,
    ConfirmationCandidate,
    InboxItem,
    LaunchdJobState,
    PanConfig,
    PendingConfirmation,
    ReplyMode,
    SessionSummary,
    SlackCredentials,
    ThreadRecord,
    ThreadSource,
    WorkerStatus,
    WorktreeStatus,
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


@pytest.mark.parametrize(
    "raw_text, expected_text",
    [
        ("do it", "do it"),
        ("<@U000TEST> to ocr: rerun it, reply in slack", "to ocr: rerun it, reply in slack"),
        ("<@U000TEST> done \u2014discard\n*Sent using* <@U000TEST|Claude>", "done --discard"),
    ],
    ids=["plain", "mention-removed", "signature-and-phone-dash"],
)
def test_inbox_drain_json_emits_the_item_its_text_tie_and_question(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw_text: str, expected_text: str
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    FileInboxStore(config.paths.inbox).append(
        _item("Ev1").model_copy(update={"raw_text": raw_text})
    )

    result = runner.invoke(cli.app, ["inbox", "drain", "--json"])

    assert result.exit_code == 0
    (entry,) = json.loads(result.stdout)
    assert set(entry) == {"item", "text", "tie", "open_question"}
    assert entry["item"]["id"] == "Ev1"
    assert entry["item"]["raw_text"] == raw_text
    assert entry["item"]["source"] == "slack"
    assert entry["text"] == expected_text
    assert entry["tie"] is None
    assert entry["open_question"] is None
    # Drain emptied the store.
    assert FileInboxStore(config.paths.inbox).drain() == []


def test_inbox_drain_reads_an_item_an_older_gateway_wrote_with_a_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    raw_item = _item("Ev1").model_dump(mode="json") | {"route": {"kind": "check"}}
    config.paths.inbox.mkdir(parents=True)
    (config.paths.inbox / "Ev1.json").write_text(json.dumps(raw_item))

    result = runner.invoke(cli.app, ["inbox", "drain", "--json"])

    assert result.exit_code == 0
    (entry,) = json.loads(result.stdout)
    assert "route" not in entry["item"]


def test_inbox_drain_names_the_tied_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    FileInboxStore(config.paths.inbox).append(_item("Ev1"))
    FileThreadMap(config.paths.threads, cli.SystemClock()).tie(
        _record("1718000000.000200").model_copy(update={"status": WorkerStatus.IDLE})
    )

    result = runner.invoke(cli.app, ["inbox", "drain", "--json"])

    assert result.exit_code == 0
    (entry,) = json.loads(result.stdout)
    assert entry["tie"] == {
        "session": "pan-task",
        "terminal_address": "t12",
        "status": "idle",
        "note": (
            "this thread's work belongs to pan-task (idle); send follow-ups there; "
            "never start a second session for the same work"
        ),
    }


@pytest.mark.parametrize("tied", [True, False], ids=["tied", "untied"])
def test_inbox_drain_text_adds_the_tie_line_only_for_a_tied_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tied: bool
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    FileInboxStore(config.paths.inbox).append(_item("Ev1"))
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.tie(_record("1718000000.000200"))
    if not tied:
        thread_map.untie("1718000000.000200")

    result = runner.invoke(cli.app, ["inbox", "drain"])

    lines = result.stdout.splitlines()
    assert lines[:2] == ["Ev1 C1 1718000000.000200 U1", "  do it"]
    assert ("belongs to pan-task (spawning)" in result.stdout) is tied
    assert len(lines) == (3 if tied else 2)


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
            "--requester",
            "U0TEST",
            "--request-text",
            "build it, reply in slack",
        ],
    )

    assert result.exit_code == 0
    assert captured["requester"] == "U0TEST"
    assert captured["request_text"] == "build it, reply in slack"
    assert captured["reply_in_slack"] is False
    assert captured["thread_ts"] == "t-1"
    assert captured["task"] == "build it"
    assert captured["channel"] == "C1"
    assert captured["stream"] == "backend"
    assert captured["base"] == config.orchestrator.worktree_base
    assert captured["app_space"] == "sp42"
    assert isinstance(captured["terminal_host"], ShellHcAdapter)
    assert captured["from_thread_reply"] is False


@pytest.mark.parametrize(
    ("extra_arguments", "expected_from_thread_reply"),
    [([], False), (["--thread-reply"], True)],
    ids=["top-level", "thread-reply"],
)
def test_spawn_passes_thread_reply_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra_arguments: list[str],
    expected_from_thread_reply: bool,
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda: _make_config(tmp_path))
    monkeypatch.setattr(
        cli,
        "load_credentials",
        lambda path: SlackCredentials(
            bot_token=SecretStr(FAKE_BOT_TOKEN), app_token=SecretStr(FAKE_APP_TOKEN)
        ),
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "spawn_worker", lambda **kwargs: captured.update(kwargs))

    result = runner.invoke(
        cli.app,
        ["spawn", "--thread", "t-1", "--task", "build it", "--channel", "C1", *extra_arguments],
    )

    assert result.exit_code == 0
    assert captured["from_thread_reply"] is expected_from_thread_reply


def test_watcher_builds_from_config_and_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    terminal_host_sentinel = object()
    monkeypatch.setattr(cli, "build_terminal_host", lambda kind: terminal_host_sentinel)
    captured: dict[str, Any] = {}

    class FakeWatcher:
        def __init__(self, terminal_host: Any, orchestrator_terminal: str, inbox_dir: Path) -> None:
            captured["args"] = (terminal_host, orchestrator_terminal, inbox_dir)

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(cli, "WatchdogInboxWatcher", FakeWatcher)

    result = runner.invoke(cli.app, ["watcher"])

    assert result.exit_code == 0
    # Built from config: the hc adapter, the orchestrator terminal name, the inbox dir.
    assert captured["args"] == (terminal_host_sentinel, "pan-orchestrator", config.paths.inbox)
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


def _use_terminal_host(
    monkeypatch: pytest.MonkeyPatch, terminal_host: FakeTerminalHost
) -> FakeTerminalHost:
    monkeypatch.setattr(cli, "build_terminal_host", lambda kind: terminal_host)
    return terminal_host


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
    terminal_host = FakeTerminalHost()
    for terminal in terminals:
        terminal_host.add(terminal)
    _use_terminal_host(monkeypatch, terminal_host)

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
    monkeypatch.setattr(
        cli, "collect_sessions", lambda terminal_host, thread_map: [_summary(drift=True)]
    )

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
        lambda terminal_host, thread_map: [_summary(drift=True), _summary(is_pan_owned=False)],
    )

    result = runner.invoke(cli.app, ["sessions"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split() == [
        "TERMINAL",
        "NAME",
        "AGENT",
        "OWNER",
        "PAN_STATUS",
        "DRIFT",
        "LAST_ASK",
    ]
    assert lines[1].split() == ["t12", "pan-a", "working", "pan", "running", "DRIFT"]
    assert "my-app / 1" in lines[2]
    assert "external" in lines[2]


def test_sessions_table_shows_the_inbox_thread_under_its_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    with_thread = _summary().model_copy(update={"inbox_thread_name": "pan-a: fix the build"})
    monkeypatch.setattr(
        cli,
        "collect_sessions",
        lambda terminal_host, thread_map: [with_thread, _summary(is_pan_owned=False)],
    )

    result = runner.invoke(cli.app, ["sessions"])

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[1].split()[:2] == ["t12", "pan-a"]
    assert lines[2].strip() == "inbox: pan-a: fix the build"
    assert "external" in lines[3]


def _live_terminal_host() -> FakeTerminalHost:
    terminal_host = FakeTerminalHost()
    terminal_host.add(make_terminal("t3", where="web / codex"))
    terminal_host.add(make_terminal("t4", where="web / review"))
    terminal_host.add(make_terminal("t5", name="pan-backend"))
    return terminal_host


def test_relay_echoes_resolved_name_and_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    terminal_host = _use_terminal_host(monkeypatch, _live_terminal_host())

    result = runner.invoke(cli.app, ["relay", "pan-backend", "re-run the failing test"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "relayed into pan-backend terminal t5"
    assert terminal_host.sent == [("t5", "re-run the failing test")]


@pytest.mark.parametrize(
    "selector, terminal_host_errors, expected_error",
    [
        ("nope", {}, TargetNotFoundError),
        ("web", {}, TargetAmbiguousError),
        ("t5", {"send": TargetBlockedError("blocked")}, TargetBlockedError),
    ],
)
def test_relay_failures_propagate_typed_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selector: str,
    terminal_host_errors: dict[str, Any],
    expected_error: type[PanError],
) -> None:
    _use_config(monkeypatch, tmp_path)
    terminal_host = _live_terminal_host()
    terminal_host.errors.update(terminal_host_errors)
    _use_terminal_host(monkeypatch, terminal_host)

    result = runner.invoke(cli.app, ["relay", selector, "hi"])

    assert isinstance(result.exception, expected_error)
    assert terminal_host.sent == []


def test_read_recent_echoes_raw_history(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_config(monkeypatch, tmp_path)
    terminal_host = _use_terminal_host(monkeypatch, _live_terminal_host())

    result = runner.invoke(cli.app, ["read", "pan-backend"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "recent output"
    assert terminal_host.reads == [("t5", cli.READ_RECENT_LINES)]


@pytest.mark.parametrize(
    "recorded_name, expects_transcript",
    [("pan-backend", True), ("pan-task", False)],
    ids=["same-terminal", "address-reused-by-another-name"],
)
def test_read_full_echoes_the_transcript_only_of_the_same_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, recorded_name: str, expects_transcript: bool
) -> None:
    # hc can reuse an address for a terminal of another name; that record's transcript is
    # not this session's, so the screen history is read instead.
    config = _use_config(monkeypatch, tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('{"type": "assistant", "message": {"content": "all done"}}\n')
    record = _record("t-1", terminal_address="t5")
    record.terminal_name = recorded_name
    record.transcript_path = transcript
    FileThreadMap(config.paths.threads, cli.SystemClock()).put(record)
    terminal_host = _use_terminal_host(monkeypatch, _live_terminal_host())

    result = runner.invoke(cli.app, ["read", "pan-backend", "--full"])

    assert result.exit_code == 0
    assert (result.stdout.strip() == "assistant: all done") is expects_transcript
    assert (terminal_host.reads == []) is expects_transcript


def test_read_not_found_propagates_target_not_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    terminal_host = _use_terminal_host(monkeypatch, _live_terminal_host())

    result = runner.invoke(cli.app, ["read", "nope"])

    assert isinstance(result.exception, TargetNotFoundError)
    assert terminal_host.reads == []


@pytest.mark.parametrize("terminal_address", ["t12", ""], ids=["recorded", "never-launched"])
def test_stop_removes_the_terminal_and_marks_failed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, terminal_address: str
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.put(_record("t-1", terminal_address=terminal_address))
    terminal_host = _use_terminal_host(monkeypatch, FakeTerminalHost())

    result = runner.invoke(cli.app, ["stop", "--thread", "t-1"])

    assert result.exit_code == 0
    assert terminal_host.removed == ([terminal_address] if terminal_address else [])
    stopped = thread_map.get("t-1")
    assert stopped is not None
    assert stopped.status is WorkerStatus.FAILED


def test_stop_edits_the_threads_status_message_to_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.put(
        _record("t-1", terminal_address="t12").model_copy(
            update={"status_message_ts": "1718000000.000900", "status_label": "fix the bug"}
        )
    )
    _use_terminal_host(monkeypatch, FakeTerminalHost())
    slack = _use_slack(monkeypatch)

    result = runner.invoke(cli.app, ["stop", "--thread", "t-1"])

    assert result.exit_code == 0
    assert [(ts, text) for _channel, ts, _blocks, text in slack.status_updates] == [
        ("1718000000.000900", "Stopped: fix the bug")
    ]
    assert slack.posts == []


def test_stop_never_stops_a_session_pan_did_not_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    desk_record = _record("t-1", terminal_address="t9").model_copy(
        update={"terminal_name": "desk-api", "started_by_pan": False, "status": WorkerStatus.IDLE}
    )
    thread_map.tie(desk_record)
    terminal_host = _use_terminal_host(
        monkeypatch, _terminal_host_with(make_terminal("t9", name="desk-api"))
    )

    result = runner.invoke(cli.app, ["stop", "--thread", "t-1"])

    assert isinstance(result.exception, NotPanSessionError)
    assert terminal_host.removed == []
    assert "t9" in terminal_host.terminals
    kept = thread_map.get("t-1")
    assert kept is not None
    assert kept.tied is False
    assert kept.status is WorkerStatus.IDLE


def _terminal_host_with(*terminals: Any) -> FakeTerminalHost:
    terminal_host = FakeTerminalHost()
    for terminal in terminals:
        terminal_host.add(terminal)
    return terminal_host


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (UnauthorizedSenderError("x"), 10),
        (ConfigMissingError("x"), 11),
        (CredentialsError("x"), 12),
        (InboxError("x"), 13),
        (ThreadNotFoundError("x"), 14),
        (SpawnError("x"), 15),
        (TerminalHostError("x"), 16),
        (HcError("x"), 16),
        (SlackPostError("x"), 17),
        (GatedOpDeniedError("x"), 18),
        (TargetNotFoundError("x"), 20),
        (TargetAmbiguousError("x", []), 21),
        (TerminalGoneError("x"), 22),
        (TargetBlockedError("x"), 23),
        (TerminalNameTakenError("x"), 24),
        (NotPanSessionError("x"), 25),
        (SessionBusyError("x"), 26),
        (ServiceError("x"), 27),
        (NoExactMatchError("x"), 28),
        (UnsavedWorkError("x"), 29),
        (GitWorktreeError("x"), 30),
        (UnsafeWorktreeError("x"), 31),
        (PrivateMessageError("x"), 32),
        (DiscardNotRequestedError("x"), 33),
        (UnansweredQuestionError("x"), 34),
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

    def get(self, thread_ts: str, *, session_name: str | None = None) -> ThreadRecord | None:
        if thread_ts != self._record.thread_ts:
            return None
        if session_name is not None and session_name != self._record.terminal_name:
            return None
        return self._record

    def get_by_worktree(self, worktree_path: Path) -> ThreadRecord | None:
        return self._record if worktree_path == self._record.worktree_path else None

    def put(self, record: ThreadRecord) -> None:  # pragma: no cover
        raise NotImplementedError

    def update_status(
        self, thread_ts: str, status: WorkerStatus, *, session_name: str | None = None
    ) -> None:
        self.status_updates.append((thread_ts, status))

    def record_status_message(
        self, thread_ts: str, message_ts: str, *, session_name: str | None = None
    ) -> None:
        self._record = self._record.model_copy(update={"status_message_ts": message_ts})


class _HookSlack(RecordingSlack):
    pass


def _hook_payload(cwd: str, event: str) -> str:
    return json.dumps({"hook_event_name": event, "cwd": cwd, "transcript_path": None})


def test_hook_stop_resolves_by_cwd_updates_the_status_and_marks_idle() -> None:
    record = _record("t-1", worktree_path=Path("/wt/pan-a")).model_copy(
        update={"reply_mode": ReplyMode.SLACK}
    )
    thread_map, slack = _HookThreadMap(record), _HookSlack()

    cli._dispatch_completion_hook(stop_hook, _hook_payload("/wt/pan-a", "Stop"), thread_map, slack)

    assert [(post.channel, post.thread_ts, post.text) for post in slack.status_posts] == [
        ("C1", "t-1", "Finished")
    ]
    assert thread_map.status_updates == [("t-1", WorkerStatus.IDLE)]


def test_hook_notification_resolves_by_cwd_updates_the_status_and_marks_blocked() -> None:
    record = _record("t-1", worktree_path=Path("/wt/pan-a")).model_copy(
        update={"reply_mode": ReplyMode.SLACK}
    )
    thread_map, slack = _HookThreadMap(record), _HookSlack()
    payload = json.dumps(
        {
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "cwd": "/wt/pan-a",
            "message": "need input",
        }
    )

    cli._dispatch_completion_hook(notification_hook, payload, thread_map, slack)

    assert slack.posts == []
    assert [(post.channel, post.thread_ts, post.text) for post in slack.status_posts] == [
        ("C1", "t-1", "Waiting for you")
    ]
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
    assert slack.status_posts == []
    assert thread_map.status_updates == []


def test_hook_subcommands_are_registered() -> None:
    result = runner.invoke(cli.app, ["hook", "--help"])

    assert result.exit_code == 0
    assert "stop" in result.stdout
    assert "notification" in result.stdout
    assert "session-start" in result.stdout
    assert "inbox-link" in result.stdout


def _inbox_link_payload(cwd: Path) -> str:
    result = {"thread_id": "thr_1", "url": "https://inbox.example/t/thr_1"}
    return json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "cwd": str(cwd),
            "tool_name": "mcp__plannotator__plannotator_inbox",
            "tool_input": {"action": "send_message", "body": "starting"},
            "tool_response": [{"type": "text", "text": json.dumps(result)}],
        }
    )


def test_hook_inbox_link_records_the_link_on_the_cwd_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    worktree = tmp_path / "wt" / "pan-task"
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.tie(_record("t-1", worktree_path=worktree))

    result = runner.invoke(cli.app, ["hook", "inbox-link"], input=_inbox_link_payload(worktree))

    assert result.exit_code == 0
    recorded = thread_map.get("t-1")
    assert recorded is not None
    assert recorded.inbox_thread_id == "thr_1"
    assert recorded.inbox_thread_url == "https://inbox.example/t/thr_1"


@pytest.mark.parametrize(
    "raw_stdin, config_fails",
    [("not json", False), ('{"cwd": "/wt/unknown"}', False), ("{}", True)],
    ids=["unparseable", "no-record", "config-error"],
)
def test_hook_inbox_link_never_fails_the_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw_stdin: str, config_fails: bool
) -> None:
    _use_config(monkeypatch, tmp_path)
    if config_fails:

        def missing_config() -> PanConfig:
            raise ConfigMissingError("no config")

        monkeypatch.setattr(cli, "load_config", missing_config)

    result = runner.invoke(cli.app, ["hook", "inbox-link"], input=raw_stdin)

    assert result.exit_code == 0
    assert result.exception is None


def test_hook_inbox_link_records_the_link_from_the_real_inbox_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    worktree = tmp_path / "wt" / "pan-task"
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.tie(_record("t-1", worktree_path=worktree))
    result_json = json.dumps({"thread_id": "thr_1", "url": "https://inbox.example/t/thr_1"})
    text = f"Sent to the Plannotator Inbox (thread thr_1, https://inbox.example/t/thr_1).\n\n{result_json}"
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "cwd": f"{worktree}/",
            "tool_name": "mcp__plannotator__plannotator_inbox",
            "tool_input": {"action": "send_message", "body": "starting"},
            "tool_response": [{"type": "text", "text": text}],
        }
    )

    result = runner.invoke(cli.app, ["hook", "inbox-link"], input=payload)

    assert result.exit_code == 0
    recorded = thread_map.get("t-1")
    assert recorded is not None
    assert recorded.inbox_thread_id == "thr_1"


@pytest.mark.parametrize(
    "raw_stdin, expected_line",
    [
        ("not json", "inbox-link hook: the payload carries no cwd"),
        ('{"cwd": "/wt/unknown"}', "inbox-link hook: no thread record for cwd=/wt/unknown"),
    ],
    ids=["no-cwd", "no-record"],
)
def test_hook_inbox_link_logs_why_it_recorded_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    raw_stdin: str,
    expected_line: str,
) -> None:
    _use_config(monkeypatch, tmp_path)
    cli.logger.addHandler(caplog.handler)
    try:
        result = runner.invoke(cli.app, ["hook", "inbox-link"], input=raw_stdin)
    finally:
        cli.logger.removeHandler(caplog.handler)

    assert result.exit_code == 0
    assert expected_line in caplog.messages


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
    terminal_host = _use_terminal_host(monkeypatch, FakeTerminalHost())

    result = runner.invoke(cli.app, ["up"])

    assert result.exit_code == 0
    (run,) = terminal_host.runs
    assert run.name == "pan-orchestrator"
    assert run.cwd == config.orchestrator.cwd
    assert run.argv[0] == "claude"
    assert result.stdout.splitlines() == [
        "orchestrator pan-orchestrator started at t101",
        "marked 1 lost worker(s) failed: t-1",
    ]


@pytest.mark.parametrize(
    ("extra_arguments", "expected_reply_in_slack"),
    [([], False), (["--reply-in-slack"], True)],
    ids=["no-flag", "reply-in-slack"],
)
def test_reopen_wires_the_record_to_reopen_worker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra_arguments: list[str],
    expected_reply_in_slack: bool,
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

    result = runner.invoke(
        cli.app, ["reopen", "--thread", "t-1", "--task", "one more thing", *extra_arguments]
    )

    assert result.exit_code == 0
    assert captured["record"].thread_ts == "t-1"
    assert captured["task"] == "one more thing"
    assert captured["app_space"] == "sp42"
    assert captured["reply_in_slack"] is expected_reply_in_slack


def test_reopen_unknown_thread_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_config(monkeypatch, tmp_path)

    result = runner.invoke(cli.app, ["reopen", "--thread", "nope", "--task", "x"])

    assert isinstance(result.exception, ThreadNotFoundError)


class _RecordingSlack(RecordingSlack):
    pass


def _use_slack(monkeypatch: pytest.MonkeyPatch) -> _RecordingSlack:
    slack = _RecordingSlack()
    monkeypatch.setattr(cli, "_slack_adapter", lambda config: slack)
    return slack


def _threaded(
    name: str, agent_status: AgentStatus, thread_ts: str, minutes_ago: int = 0
) -> SessionSummary:
    return _summary().model_copy(
        update={
            "address": f"t-{name}",
            "name": name,
            "agent_status": agent_status,
            "thread_ts": thread_ts,
            "thread_channel": "C000TEST",
            "updated_at": datetime(2026, 7, 16, 10, 0, tzinfo=UTC) - timedelta(minutes=minutes_ago),
        }
    )


_SESSIONS_IN_THREAD = ["sessions", "--thread", "1700000000.000100", "--channel", "C000TEST"]


_SESSIONS_FOR_REQUESTER = [*_SESSIONS_IN_THREAD, "--requester", "U000TEST"]


@pytest.mark.parametrize(
    "show, expected_text",
    [
        (
            "t-pan-ocr,t-pan-docs",
            "*Your sessions*\n"
            "• *pan-ocr* · Waiting for you · <https://example.slack.com/p2|thread>\n"
            "• *pan-docs* · Idle",
        ),
        (
            "t-pan-docs, t-pan-ocr",
            "*Your sessions*\n"
            "• *pan-docs* · Idle\n"
            "• *pan-ocr* · Waiting for you · <https://example.slack.com/p2|thread>",
        ),
        ("t-pan-docs,t-pan-orchestrator,t9", "*Your sessions*\n• *pan-docs* · Idle"),
        ("t9", "None of those sessions is running."),
        ("", "None of those sessions is running."),
    ],
    ids=["given-order", "other-order", "orchestrator-and-unknown-dropped", "none-live", "empty"],
)
def test_sessions_show_sends_the_chosen_sessions_to_the_requester_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, show: str, expected_text: str
) -> None:
    _use_config(monkeypatch, tmp_path)
    summaries = [
        _threaded("pan-docs", AgentStatus.IDLE, "1700000000.000300"),
        _threaded("pan-ocr", AgentStatus.BLOCKED, "1700000000.000200"),
        _threaded("pan-orchestrator", AgentStatus.BLOCKED, "1700000000.000400"),
    ]
    monkeypatch.setattr(cli, "collect_sessions", lambda terminal_host, thread_map: summaries)
    slack = _use_slack(monkeypatch)
    slack.permalinks["1700000000.000200"] = "https://example.slack.com/p2"

    result = runner.invoke(cli.app, [*_SESSIONS_FOR_REQUESTER, "--show", show])

    assert result.exit_code == 0
    assert result.stdout == ""
    assert slack.posts == []
    assert slack.ephemerals == [("C000TEST", "1700000000.000100", "U000TEST", expected_text)]


def test_sessions_show_with_nothing_running_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "collect_sessions", lambda terminal_host, thread_map: [])
    slack = _use_slack(monkeypatch)

    result = runner.invoke(cli.app, [*_SESSIONS_FOR_REQUESTER, "--show", ""])

    assert result.exit_code == 0
    assert slack.ephemerals == [
        ("C000TEST", "1700000000.000100", "U000TEST", "No sessions running.")
    ]


def test_sessions_posts_nothing_publicly_when_the_private_list_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "collect_sessions", lambda terminal_host, thread_map: [_summary()])
    slack = _use_slack(monkeypatch)
    slack.ephemeral_error = SlackPostError("not delivered")

    result = runner.invoke(cli.app, [*_SESSIONS_FOR_REQUESTER, "--show", "t12"])

    assert isinstance(result.exception, PrivateMessageError)
    assert cli._exit_code_for(result.exception) == 32
    assert slack.posts == []
    assert slack.ephemerals == []


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--show", "t12"],
        ["--requester", "U000TEST"],
        ["--thread", "1700000000.000100"],
        [*_SESSIONS_IN_THREAD[1:], "--show", "t12"],
        [*_SESSIONS_IN_THREAD[1:], "--requester", "U000TEST"],
        ["--match", "ocr"],
    ],
    ids=[
        "show-alone",
        "requester-alone",
        "thread-alone",
        "no-requester",
        "no-show",
        "retired-match",
    ],
)
def test_sessions_rejects_incomplete_slack_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, extra_args: list[str]
) -> None:
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "collect_sessions", lambda terminal_host, thread_map: [_summary()])
    slack = _use_slack(monkeypatch)

    result = runner.invoke(cli.app, ["sessions", *extra_args])

    assert result.exit_code != 0
    assert slack.posts == []
    assert slack.ephemerals == []


class _CountingSlack(_RecordingSlack):
    def __init__(self) -> None:
        super().__init__()
        self.permalink_requests: list[tuple[str, str]] = []

    def permalink(self, channel: str, ts: str) -> str | None:
        self.permalink_requests.append((channel, ts))
        return super().permalink(channel, ts)


def test_sessions_fetches_links_only_for_the_sessions_shown_and_each_thread_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    summaries = [
        _threaded("pan-a", AgentStatus.BLOCKED, "1700000000.000200"),
        _threaded("pan-b", AgentStatus.WORKING, "1700000000.000200"),
        *[
            _threaded(f"pan-idle-{index}", AgentStatus.IDLE, f"1700000000.000{index}00", index)
            for index in range(3, 9)
        ],
    ]
    monkeypatch.setattr(cli, "collect_sessions", lambda terminal_host, thread_map: summaries)
    slack = _CountingSlack()
    monkeypatch.setattr(cli, "_slack_adapter", lambda config: slack)

    show = ",".join(summary.address for summary in summaries)
    result = runner.invoke(cli.app, [*_SESSIONS_FOR_REQUESTER, "--show", show])

    assert result.exit_code == 0
    assert slack.permalink_requests == [
        ("C000TEST", "1700000000.000200"),
        ("C000TEST", "1700000000.000300"),
        ("C000TEST", "1700000000.000400"),
        ("C000TEST", "1700000000.000500"),
    ]


def test_check_command_posts_through_the_thread_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    _use_terminal_host(monkeypatch, FakeTerminalHost())
    slack = _use_slack(monkeypatch)

    result = runner.invoke(cli.app, ["check", "--thread", "t-1", "--channel", "C0TEST"])

    assert result.exit_code == 0
    assert slack.posts == [
        ("C0TEST", "t-1", "this thread isn't tied to a session; @pan sessions lists them")
    ]


def test_forget_command_unties_the_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _use_config(monkeypatch, tmp_path)
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.tie(_record("t-1"))
    slack = _use_slack(monkeypatch)

    result = runner.invoke(cli.app, ["forget", "--thread", "t-1", "--channel", "C0TEST"])

    assert result.exit_code == 0
    record = thread_map.get("t-1")
    assert record is not None
    assert record.tied is False
    assert slack.posts == [
        ("C0TEST", "t-1", "untied this thread from pan-task; the session was not stopped")
    ]


@pytest.mark.parametrize(
    "discard_args, expected_discard",
    [
        ([], False),
        (["--discard", "--request-text", "<@U000BOT> done --discard"], True),
        (["--discard", "--request-text", "<@U000BOT> done \u2014discard"], True),
    ],
    ids=["keep", "discard-typed", "discard-phone-dash"],
)
def test_done_command_wires_to_done_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    discard_args: list[str],
    expected_discard: bool,
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    _use_terminal_host(monkeypatch, FakeTerminalHost())
    _use_slack(monkeypatch)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "done_thread", lambda **kwargs: captured.update(kwargs))

    result = runner.invoke(
        cli.app, ["done", "--thread", "t-1", "--channel", "C0TEST", *discard_args]
    )

    assert result.exit_code == 0
    assert captured["thread_ts"] == "t-1"
    assert captured["channel"] == "C0TEST"
    assert captured["discard"] is expected_discard
    assert captured["worktree_base"] == config.orchestrator.worktree_base
    assert captured["fallback_repo"] == config.orchestrator.cwd


@pytest.mark.parametrize(
    "request_args",
    [
        [],
        ["--request-text", "<@U000BOT> done"],
        ["--request-text", "<@U000BOT> just close it, throw the changes away"],
        ["--request-text", "<@U000BOT> done discard"],
    ],
    ids=["no-request-text", "plain-done", "by-meaning", "no-dashes"],
)
def test_done_command_refuses_discard_the_person_did_not_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request_args: list[str]
) -> None:
    _use_config(monkeypatch, tmp_path)
    terminal_host = _use_terminal_host(monkeypatch, FakeTerminalHost())
    slack = _use_slack(monkeypatch)
    monkeypatch.setattr(cli, "done_thread", lambda **kwargs: pytest.fail("must not close"))

    result = runner.invoke(
        cli.app,
        ["done", "--thread", "t-1", "--channel", "C000TEST", "--discard", *request_args],
    )

    assert isinstance(result.exception, DiscardNotRequestedError)
    assert cli._exit_code_for(result.exception) == 33
    assert terminal_host.removed == []
    assert slack.posts == []


def test_done_command_keeps_a_worktree_with_unsaved_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    worktree_path = config.orchestrator.worktree_base / "pan-task"
    worktree_path.mkdir(parents=True)
    thread_map = FileThreadMap(config.paths.threads, cli.SystemClock())
    thread_map.tie(_record("t-1", worktree_path=worktree_path))
    terminal_host = _use_terminal_host(
        monkeypatch, _terminal_host_with(make_terminal("t12", name="pan-task"))
    )
    git = FakeGitWorktree(
        status=WorktreeStatus(
            main_repo=tmp_path / "repo",
            is_main_worktree=False,
            branch="pan-task",
            modified_paths=["app.py"],
        ),
        branches={tmp_path / "repo": BranchStatus()},
    )
    monkeypatch.setattr(cli, "ShellGitWorktreeAdapter", lambda: git)
    slack = _use_slack(monkeypatch)

    result = runner.invoke(cli.app, ["done", "--thread", "t-1", "--channel", "C0TEST"])

    assert isinstance(result.exception, UnsavedWorkError)
    assert cli._exit_code_for(result.exception) == 29
    assert terminal_host.removed == []
    assert slack.posts == [
        (
            "C0TEST",
            "t-1",
            "pan-task has 1 uncommitted file; kept it. "
            "Push or commit them, or send `@pan done --discard`.",
        )
    ]


def test_send_command_wires_to_send_to_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    terminal_host = _use_terminal_host(monkeypatch, FakeTerminalHost())
    slack = _use_slack(monkeypatch)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "send_to_session", lambda **kwargs: captured.update(kwargs))

    result = runner.invoke(
        cli.app,
        [
            "send",
            "--thread",
            "t-1",
            "--channel",
            "C0TEST",
            "--to",
            "pan-7",
            "--task",
            "add the tests",
            "--requester",
            "U0TEST",
            "--request-text",
            "<@U000BOT> to pan-7: add the tests",
            "--reply-in-slack",
        ],
    )

    assert result.exit_code == 0
    assert captured["thread_ts"] == "t-1"
    assert captured["channel"] == "C0TEST"
    assert captured["selector"] == "pan-7"
    assert captured["text"] == "add the tests"
    assert captured["requester"] == "U0TEST"
    assert captured["reply_in_slack"] is True
    assert captured["terminal_host"] is terminal_host
    assert captured["slack"] is slack


@pytest.mark.parametrize(
    "selector, request_text, expected_sent",
    [
        ("pan-7", "<@U000BOT> to pan-7: add the tests", True),
        ("PAN-7", "<@U000BOT> ask pan-7 to add the tests", True),
        ("work / 1", "<@U000BOT> send to work / 1: add the tests", True),
        ("pan-7", "<@U000BOT> to pan-77: add the tests", False),
        ("pan-7", "<@U000BOT> to pan: add the tests", False),
        ("ocr", "<@U000BOT> to ocrr: rerun it", False),
        ("pan-7", "<@U000BOT> the session from this morning: add the tests", False),
    ],
    ids=[
        "named",
        "any-case",
        "phrase",
        "longer-name-typed",
        "shorter-name-typed",
        "typo",
        "described-not-named",
    ],
)
def test_send_command_sends_only_to_a_session_the_person_named(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    selector: str,
    request_text: str,
    expected_sent: bool,
) -> None:
    _use_config(monkeypatch, tmp_path)
    _use_terminal_host(monkeypatch, FakeTerminalHost())
    slack = _use_slack(monkeypatch)
    sent: list[str] = []
    monkeypatch.setattr(cli, "send_to_session", lambda **kwargs: sent.append(kwargs["selector"]))

    result = runner.invoke(
        cli.app,
        [
            "send",
            "--thread",
            "1700000000.000100",
            "--channel",
            "C000TEST",
            "--to",
            selector,
            "--task",
            "add the tests",
            "--requester",
            "U000TEST",
            "--request-text",
            request_text,
        ],
    )

    assert sent == ([selector] if expected_sent else [])
    if not expected_sent:
        assert isinstance(result.exception, NoExactMatchError)
        assert cli._exit_code_for(result.exception) == 28
    assert slack.posts == []


def test_send_command_to_a_session_needs_the_request_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "send_to_session", lambda **kwargs: pytest.fail("must not send"))

    result = runner.invoke(
        cli.app,
        [
            "send",
            "--thread",
            "1700000000.000100",
            "--channel",
            "C000TEST",
            "--to",
            "pan-7",
            "--task",
            "add the tests",
        ],
    )

    assert result.exit_code != 0


@pytest.mark.parametrize(
    ("extra_arguments", "expected_reply_in_slack"),
    [([], False), (["--reply-in-slack"], True)],
    ids=["no-flag", "reply-in-slack"],
)
def test_send_command_without_a_session_name_follows_up_to_the_tied_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    extra_arguments: list[str],
    expected_reply_in_slack: bool,
) -> None:
    _use_config(monkeypatch, tmp_path)
    terminal_host = _use_terminal_host(monkeypatch, FakeTerminalHost())
    slack = _use_slack(monkeypatch)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "follow_up_to_session", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr(cli, "send_to_session", lambda **kwargs: pytest.fail("not a to-route"))

    result = runner.invoke(
        cli.app,
        [
            "send",
            "--thread",
            "t-1",
            "--channel",
            "C0TEST",
            "--task",
            "and the docs",
            *extra_arguments,
        ],
    )

    assert result.exit_code == 0
    assert captured["thread_ts"] == "t-1"
    assert captured["text"] == "and the docs"
    assert captured["terminal_host"] is terminal_host
    assert captured["slack"] is slack
    assert captured["reply_in_slack"] is expected_reply_in_slack


def _store_question(
    config: PanConfig, asked_minutes_ago: int, *, requester: str | None = "U1"
) -> None:
    FilePendingConfirmations(confirmations_path(config.paths.threads)).put(
        PendingConfirmation(
            thread_ts="1718000000.000200",
            channel="C1",
            selector="pan docs",
            task="add the tests",
            requester=requester,
            candidates=[
                ConfirmationCandidate(address="t3", cwd=Path("/tmp/pan"), label="t3 (folder pan)")
            ],
            created_at=datetime.now(tz=UTC) - timedelta(minutes=asked_minutes_ago),
        )
    )


def _stored_question(config: PanConfig) -> PendingConfirmation | None:
    return FilePendingConfirmations(confirmations_path(config.paths.threads)).get(
        ThreadSource.SLACK, "1718000000.000200"
    )


def _reply(event_id: str, slack_user: str, raw_text: str = "yes") -> InboxItem:
    return _item(event_id).model_copy(
        update={"slack_user": slack_user, "raw_text": raw_text, "received_at": datetime.now(tz=UTC)}
    )


@pytest.mark.parametrize(
    "slack_user, requester, raw_text, expected_answered",
    [
        ("U1", "U1", "yes", True),
        ("U1", "U1", "<@U000BOT> the second one please", True),
        ("U1", "U1", "no wait, start something new", True),
        ("U000OTHER", "U1", "yes", False),
        ("U1", None, "yes", False),
    ],
    ids=[
        "requester-yes",
        "requester-by-meaning",
        "requester-other-text",
        "another-user",
        "no-requester",
    ],
)
def test_inbox_drain_marks_the_question_answered_only_by_the_requester(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    slack_user: str,
    requester: str | None,
    raw_text: str,
    expected_answered: bool,
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    _store_question(config, asked_minutes_ago=1, requester=requester)
    FileInboxStore(config.paths.inbox).append(_reply("Ev1", slack_user, raw_text))

    result = runner.invoke(cli.app, ["inbox", "drain", "--json"])

    assert result.exit_code == 0
    (entry,) = json.loads(result.stdout)
    question = entry["open_question"]
    assert question["selector"] == "pan docs"
    assert question["task"] == "add the tests"
    assert question["candidates"] == [{"number": 1, "label": "t3 (folder pan)", "address": "t3"}]
    assert question["answered_by_requester"] is expected_answered
    stored = _stored_question(config)
    assert stored is not None
    assert (stored.answered_at is not None) is expected_answered


def test_inbox_drain_shows_no_question_once_it_expired(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    _store_question(config, asked_minutes_ago=31)
    FileInboxStore(config.paths.inbox).append(_reply("Ev1", "U1"))

    result = runner.invoke(cli.app, ["inbox", "drain", "--json"])

    (entry,) = json.loads(result.stdout)
    assert entry["open_question"] is None


def test_inbox_drain_keeps_the_answer_for_later_items_in_the_batch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    _store_question(config, asked_minutes_ago=1)
    store = FileInboxStore(config.paths.inbox)
    store.append(_reply("Ev1", "U1"))
    store.append(_reply("Ev2", "U000OTHER"))

    result = runner.invoke(cli.app, ["inbox", "drain", "--json"])

    answered = [
        entry["open_question"]["answered_by_requester"] for entry in json.loads(result.stdout)
    ]
    assert answered == [True, True]


@pytest.mark.parametrize(
    "contents",
    [b"{not json", b"[1, 2]", b"\xff\xfe"],
    ids=["not-json", "not-an-object", "not-text"],
)
def test_inbox_drain_with_a_damaged_confirmations_file_still_emits_every_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, contents: bytes
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    confirmations_path(config.paths.threads).write_bytes(contents)
    store = FileInboxStore(config.paths.inbox)
    store.append(_item("Ev1").model_copy(update={"raw_text": "yes"}))
    store.append(_item("Ev2"))

    result = runner.invoke(cli.app, ["inbox", "drain", "--json"])

    assert result.exit_code == 0
    emitted = json.loads(result.stdout)
    assert sorted(entry["item"]["id"] for entry in emitted) == ["Ev1", "Ev2"]
    assert all(entry["open_question"] is None for entry in emitted)


def test_propose_command_wires_to_propose_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_config(monkeypatch, tmp_path)
    terminal_host = _use_terminal_host(monkeypatch, FakeTerminalHost())
    slack = _use_slack(monkeypatch)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "propose_sessions", lambda **kwargs: captured.update(kwargs))

    result = runner.invoke(
        cli.app,
        [
            "propose",
            "--thread",
            "t-1",
            "--channel",
            "C0TEST",
            "--to",
            "pan docs",
            "--task",
            "add the tests",
            "--candidate",
            "t3",
            "--candidate",
            "t4",
            "--requester",
            "U0TEST",
        ],
    )

    assert result.exit_code == 0
    assert captured["selector"] == "pan docs"
    assert captured["task"] == "add the tests"
    assert captured["candidate_addresses"] == ["t3", "t4"]
    assert captured["requester"] == "U0TEST"
    assert captured["reply_in_slack"] is False
    assert captured["terminal_host"] is terminal_host
    assert captured["slack"] is slack


@pytest.mark.parametrize(
    "arguments, expected_call, expected_choice",
    [
        (["--choice", "2"], "confirm_session", "2"),
        (["--choice", " Y "], "confirm_session", "yes"),
        (["--choice", "YES"], "confirm_session", "yes"),
        (["--cancel"], "cancel_confirmation", None),
    ],
    ids=["number", "y-with-spaces", "upper-case-yes", "cancel"],
)
def test_confirm_command_wires_the_reply(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arguments: list[str],
    expected_call: str,
    expected_choice: str | None,
) -> None:
    _use_config(monkeypatch, tmp_path)
    _use_terminal_host(monkeypatch, FakeTerminalHost())
    _use_slack(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []
    for name in ("confirm_session", "cancel_confirmation"):
        monkeypatch.setattr(cli, name, lambda name=name, **kwargs: calls.append((name, kwargs)))

    result = runner.invoke(
        cli.app, ["confirm", "--thread", "t-1", "--channel", "C0TEST", *arguments]
    )

    assert result.exit_code == 0
    ((called, kwargs),) = calls
    assert called == expected_call
    assert kwargs["thread_ts"] == "t-1"
    assert kwargs.get("choice") == expected_choice


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--choice", "1", "--cancel"],
        ["--choice", "maybe"],
        ["--choice", "4"],
        ["--choice", "no"],
    ],
    ids=["neither", "both", "not-a-reply", "out-of-range", "no-is-cancel"],
)
def test_confirm_command_needs_exactly_one_of_choice_or_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, arguments: list[str]
) -> None:
    _use_config(monkeypatch, tmp_path)

    result = runner.invoke(
        cli.app, ["confirm", "--thread", "t-1", "--channel", "C0TEST", *arguments]
    )

    assert result.exit_code != 0


@pytest.mark.parametrize("answered", [False, True], ids=["unanswered", "answered"])
def test_confirm_command_sends_only_after_the_requester_replied(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answered: bool
) -> None:
    config = _use_config(monkeypatch, tmp_path)
    _store_question(config, asked_minutes_ago=1)
    if answered:
        FilePendingConfirmations(confirmations_path(config.paths.threads)).mark_answered(
            ThreadSource.SLACK, "1718000000.000200", datetime.now(tz=UTC)
        )
    terminal_host = _use_terminal_host(
        monkeypatch, _terminal_host_with(make_terminal("t3", cwd=Path("/tmp/pan")))
    )
    slack = _use_slack(monkeypatch)
    monkeypatch.setattr(cli, "ShellPlannotatorInboxAdapter", FakeInboxDirectory)

    result = runner.invoke(
        cli.app,
        ["confirm", "--thread", "1718000000.000200", "--channel", "C1", "--choice", "yes"],
    )

    if answered:
        assert result.exit_code == 0
        assert [address for address, _text in terminal_host.sent] == ["t3"]
    else:
        assert isinstance(result.exception, UnansweredQuestionError)
        assert cli._exit_code_for(result.exception) == 34
        assert terminal_host.sent == []
        assert slack.posts == []


@pytest.fixture
def service_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeServiceManager:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(shutil, "which", lambda _name: "/opt/tools/bin/pan")
    manager = FakeServiceManager(home / "Library" / "LaunchAgents")
    monkeypatch.setattr(cli, "build_service_manager", lambda _home: manager)
    return manager


def test_service_install_loads_every_agent_with_the_resolved_binary(
    service_manager: FakeServiceManager,
) -> None:
    result = runner.invoke(cli.app, ["service", "install"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[:3] == [
        "loaded com.pan.gateway",
        "loaded com.pan.watcher",
        "loaded com.pan.up",
    ]
    assert "/opt/tools/bin/pan" in result.output
    assert "<string>/opt/tools/bin/pan</string>" in service_manager.plists["com.pan.gateway"]


def test_service_uninstall_reports_each_agent(service_manager: FakeServiceManager) -> None:
    service_manager.plists["com.pan.gateway"] = "<plist/>"
    service_manager.loaded["com.pan.gateway"] = LaunchdJobState(pid=412)

    result = runner.invoke(cli.app, ["service", "uninstall"])

    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        "com.pan.up: was not loaded, no plist",
        "com.pan.watcher: was not loaded, no plist",
        "com.pan.gateway: unloaded, plist deleted",
    ]


def test_service_status_lists_each_agent(service_manager: FakeServiceManager) -> None:
    service_manager.plists["com.pan.gateway"] = "<plist/>"
    service_manager.loaded["com.pan.gateway"] = LaunchdJobState(pid=412, last_exit_code=0)

    result = runner.invoke(cli.app, ["service", "status"])

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0].split() == ["com.pan.gateway", "installed", "loaded", "pid=412", "last_exit=0"]
    assert lines[1].split() == [
        "com.pan.watcher",
        "not",
        "installed",
        "not",
        "loaded",
        "pid=-",
        "last_exit=-",
    ]


def test_service_install_failure_maps_to_service_exit_code(
    monkeypatch: pytest.MonkeyPatch, service_manager: FakeServiceManager
) -> None:
    service_manager.fail_bootstrap_for = "com.pan.gateway"
    monkeypatch.setattr(sys, "argv", ["pan", "service", "install"])

    assert cli._run() == 27
