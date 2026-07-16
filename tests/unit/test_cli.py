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
from pan.inbox import FileInboxStore
from pan.models import InboxItem, PanConfig, SlackCredentials, ThreadRecord
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


def _record(thread_ts: str) -> ThreadRecord:
    now = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    return ThreadRecord(
        thread_ts=thread_ts,
        workspace_name="pan-task",
        workspace_id="ws1",
        pane_ids=["p1"],
        worktree_path=Path("/tmp/wt"),
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
