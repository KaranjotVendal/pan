from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from pan.models import (
    Agent,
    Autonomy,
    InboxItem,
    PanConfig,
    SlackConfig,
    ThreadRecord,
    UserPolicy,
    WorkerStatus,
)


def _full_config_dict() -> dict:
    return {
        "slack": {"socket_mode": True},
        "orchestrator": {
            "terminal": "pan-orchestrator",
            "cwd": "/Users/me/dev/pan",
            "worktree_base": "/Users/me/dev/pan-worktrees",
        },
        "defaults": {
            "agent": "claude",
            "permission_mode": "bypass",
            "repo_allowlist": ["/Users/me/dev/pan"],
        },
        "users": {
            "U12345": {
                "autonomy": "full",
                "channels": ["C0001", "C0002"],
                "repos": ["*"],
            }
        },
        "gated_ops": [],
        "paths": {
            "inbox": "/Users/me/.pan/inbox",
            "threads": "/Users/me/.pan/threads.json",
            "logs": "/Users/me/.pan/logs",
            "credentials": "/Users/me/.pan/credentials.json",
        },
    }


def test_pan_config_composition_resolves_nested_fields() -> None:
    config = PanConfig.model_validate(_full_config_dict())

    assert config.orchestrator.terminal == "pan-orchestrator"
    assert config.orchestrator.cwd == Path("/Users/me/dev/pan")
    assert config.defaults.agent is Agent.CLAUDE
    assert config.users["U12345"].channels == ["C0001", "C0002"]
    assert config.users["U12345"].repos == ["*"]
    assert config.paths.inbox == Path("/Users/me/.pan/inbox")
    assert config.paths.threads == Path("/Users/me/.pan/threads.json")


def test_inbox_item_parses_typed_fields() -> None:
    item = InboxItem.model_validate(
        {
            "id": "Ev0AAAAA",
            "slack_user": "U12345",
            "channel": "C0001",
            "thread_ts": "1718000000.000100",
            "is_thread_reply": True,
            "raw_text": "@pan build the thing",
            "received_at": "2026-07-16T10:00:00+00:00",
        }
    )

    assert item.is_thread_reply is True
    assert isinstance(item.received_at, datetime)


def test_thread_record_defaults_to_spawning() -> None:
    now = datetime(2026, 7, 16, 10, 0, 0)
    record = ThreadRecord(
        thread_ts="1718000000.000100",
        terminal_name="pan-task-3",
        channel="C0001",
        worktree_path=Path("/Users/me/dev/pan-worktrees/task-3"),
        created_at=now,
        updated_at=now,
    )

    assert record.status is WorkerStatus.SPAWNING
    assert record.agent is Agent.CLAUDE
    assert record.terminal_address == ""
    assert record.claude_session_id is None
    assert record.transcript_path is None


def test_thread_record_carries_channel() -> None:
    now = datetime(2026, 7, 16, 10, 0, 0)
    record = ThreadRecord(
        thread_ts="1718000000.000100",
        terminal_name="pan-task-3",
        channel="C0001",
        worktree_path=Path("/Users/me/dev/pan-worktrees/task-3"),
        created_at=now,
        updated_at=now,
    )

    assert record.channel == "C0001"


@pytest.mark.parametrize(
    "legacy_status, expected_status",
    [
        ("running", WorkerStatus.FAILED),
        ("blocked", WorkerStatus.FAILED),
        ("spawning", WorkerStatus.FAILED),
        ("failed", WorkerStatus.FAILED),
        ("done", WorkerStatus.DONE),
    ],
)
def test_herdr_era_record_loads_as_lost(legacy_status: str, expected_status: WorkerStatus) -> None:
    # A record written before the hc migration points at a herdr pane that no longer
    # exists: it loads with no terminal address and, unless it already finished, FAILED.
    record = ThreadRecord.model_validate(
        {
            "thread_ts": "1700000000.000100",
            "workspace_name": "pan-0a1b2c3d",
            "workspace_id": "w1",
            "channel": "C0001",
            "pane_ids": ["w1:p1"],
            "worktree_path": "/wt/pan-0a1b2c3d",
            "morcli_session": "abc",
            "status": legacy_status,
            "created_at": "2026-07-21T10:00:00+00:00",
            "updated_at": "2026-07-21T10:00:00+00:00",
        }
    )

    assert record.terminal_name == "pan-0a1b2c3d"
    assert record.terminal_address == ""
    assert record.status is expected_status


@pytest.mark.parametrize(
    "enum_member, value",
    [
        (WorkerStatus.SPAWNING, "spawning"),
        (WorkerStatus.RUNNING, "running"),
        (WorkerStatus.BLOCKED, "blocked"),
        (WorkerStatus.DONE, "done"),
        (WorkerStatus.FAILED, "failed"),
        (Agent.CLAUDE, "claude"),
        (Agent.CODEX, "codex"),
        (Agent.PI, "pi"),
    ],
)
def test_str_enum_values(enum_member: str, value: str) -> None:
    assert enum_member == value


def test_user_policy_defaults_are_permissive_wildcards() -> None:
    policy = UserPolicy()

    assert policy.autonomy is Autonomy.FULL
    assert policy.channels == ["*"]
    assert policy.repos == ["*"]


def test_slack_config_defaults_socket_mode_on() -> None:
    assert SlackConfig().socket_mode is True
