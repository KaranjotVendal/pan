from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

import pan.cli as cli
from pan.errors import (
    ConfigMissingError,
    CredentialsError,
    GatedOpDeniedError,
    HerdrError,
    InboxError,
    MorcliError,
    PanError,
    SlackPostError,
    SpawnError,
    ThreadNotFoundError,
    UnauthorizedSenderError,
)
from pan.hooks.notification import notification_hook
from pan.hooks.stop import stop_hook
from pan.inbox import FileInboxStore
from pan.models import InboxItem, PanConfig, SlackCredentials, ThreadRecord, WorkerStatus
from pan.threadmap import FileThreadMap

runner = CliRunner()

FAKE_BOT_TOKEN = "xoxb-fake-cli-bottoken"
FAKE_APP_TOKEN = "xapp-fake-cli-apptoken"


def _make_config(tmp_path: Path) -> PanConfig:
    return PanConfig.model_validate(
        {
            "orchestrator": {"pane_id": "%3", "worktree_base": str(tmp_path / "wt")},
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


def _record(thread_ts: str, worktree_path: Path = Path("/tmp/wt")) -> ThreadRecord:
    now = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    return ThreadRecord(
        thread_ts=thread_ts,
        workspace_name="pan-task",
        workspace_id="ws1",
        channel="C1",
        pane_ids=["p1"],
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
    assert record["workspace_id"] == "ws1"


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


def test_watcher_builds_from_config_and_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    herdr_sentinel = object()
    monkeypatch.setattr(cli, "ShellHerdrAdapter", lambda: herdr_sentinel)
    captured: dict[str, Any] = {}

    class FakeWatcher:
        def __init__(self, herdr: Any, orchestrator_pane_id: str, inbox_dir: Path) -> None:
            captured["args"] = (herdr, orchestrator_pane_id, inbox_dir)

        def start(self) -> None:
            captured["started"] = True

    monkeypatch.setattr(cli, "WatchdogInboxWatcher", FakeWatcher)

    result = runner.invoke(cli.app, ["watcher"])

    assert result.exit_code == 0
    # Built from config: the ShellHerdrAdapter, the orchestrator pane id, the inbox dir.
    assert captured["args"] == (herdr_sentinel, config.orchestrator.pane_id, config.paths.inbox)
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


def test_status_resolves_morcli_handle_from_thread(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _make_config(tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    FileThreadMap(config.paths.threads, cli.SystemClock()).put(_record("t-1"))
    handles: list[str] = []

    class FakeMorcli:
        def session_status(self, handle: str) -> Any:
            handles.append(handle)
            from pan.models import WorkerStatus

            return WorkerStatus.RUNNING

    monkeypatch.setattr(cli, "ShellMorcliAdapter", FakeMorcli)

    result = runner.invoke(cli.app, ["status", "--thread", "t-1"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "running"
    # Resolved from the record's workspace_id (morcli_session is None until captured).
    assert handles == ["ws1"]


@pytest.mark.parametrize(
    "error, expected_code",
    [
        (UnauthorizedSenderError("x"), 10),
        (ConfigMissingError("x"), 11),
        (CredentialsError("x"), 12),
        (InboxError("x"), 13),
        (ThreadNotFoundError("x"), 14),
        (SpawnError("x"), 15),
        (HerdrError("x"), 16),
        (SlackPostError("x"), 17),
        (GatedOpDeniedError("x"), 18),
        (MorcliError("x"), 19),
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
