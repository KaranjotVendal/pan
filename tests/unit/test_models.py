from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from pan.core.errors import SlackPostError
from pan.core.models import (
    Conversation,
    InboxItem,
    PanConfig,
    SlackConfig,
    ThreadRecord,
    ThreadSource,
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
        "users": {"U12345": {"channels": ["C0001", "C0002"]}},
        "paths": {
            "inbox": "/Users/me/.pan/inbox",
            "threads": "/Users/me/.pan/threads.json",
            "credentials": "/Users/me/.pan/credentials.json",
        },
    }


def test_pan_config_composition_resolves_nested_fields() -> None:
    config = PanConfig.model_validate(_full_config_dict())

    assert config.orchestrator.terminal == "pan-orchestrator"
    assert config.orchestrator.cwd == Path("/Users/me/dev/pan")
    assert config.users["U12345"].channels == ["C0001", "C0002"]
    assert config.paths.inbox == Path("/Users/me/.pan/inbox")
    assert config.paths.threads == Path("/Users/me/.pan/threads.json")


@pytest.mark.parametrize(
    ("stored_source", "expected_source"),
    [({}, ThreadSource.SLACK), ({"source": "x"}, ThreadSource.X)],
    ids=["no-source", "x"],
)
def test_a_flat_inbox_item_from_before_conversation_loads_into_the_new_shape(
    stored_source: dict[str, str], expected_source: ThreadSource
) -> None:
    item = InboxItem.model_validate(
        {
            "id": "Ev0AAAAA",
            "slack_user": "U12345",
            "channel": "C0001",
            "thread_ts": "1718000000.000100",
            "is_thread_reply": True,
            "raw_text": "@pan build the thing",
            "received_at": "2026-07-16T10:00:00+00:00",
            **stored_source,
        }
    )

    assert item.author == "U12345"
    assert item.conversation == Conversation(
        source=expected_source, id="1718000000.000100", place="C0001"
    )
    assert isinstance(item.received_at, datetime)


def test_conversation_key_tells_sources_apart_and_ignores_the_place() -> None:
    slack_thread = Conversation.slack("1700000000", "C0001")

    assert slack_thread.key == Conversation.slack("1700000000", "C0002").key
    assert slack_thread.key != Conversation(source=ThreadSource.X, id="1700000000").key


def test_conversation_slack_channel_needs_a_place() -> None:
    assert Conversation.slack("1700000000", "C0001").slack_channel() == "C0001"
    with pytest.raises(SlackPostError):
        Conversation(source=ThreadSource.X, id="1700000000").slack_channel()


def test_thread_record_defaults_to_spawning() -> None:
    now = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    record = ThreadRecord(
        conversation=Conversation.slack("1718000000.000100", "C0001"),
        terminal_name="pan-task-3",
        worktree_path=Path("/Users/me/dev/pan-worktrees/task-3"),
        created_at=now,
        updated_at=now,
    )

    assert record.status is WorkerStatus.SPAWNING
    assert record.terminal_address == ""
    assert record.claude_session_id is None
    assert record.transcript_path is None


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

    assert record.conversation == Conversation.slack("1700000000.000100", "C0001")
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
    ],
)
def test_str_enum_values(enum_member: str, value: str) -> None:
    assert enum_member == value


def test_user_policy_defaults_are_permissive_wildcards() -> None:
    policy = UserPolicy()

    assert policy.channels == ["*"]


def test_slack_config_defaults_socket_mode_on() -> None:
    assert SlackConfig().socket_mode is True
