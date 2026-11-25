from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pan.core.errors import ThreadNotFoundError
from pan.core.models import Conversation, ReplyMode, ThreadRecord, ThreadSource, WorkerStatus
from pan.data.threadmap import FileThreadMap, tie_note


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def set(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def _slack(thread_ts: str) -> Conversation:
    return Conversation.slack(thread_ts, "C0001")


def _x(post_id: str) -> Conversation:
    return Conversation(source=ThreadSource.X, id=post_id)


def _record(
    thread_ts: str = "1718000000.000100",
    worktree_path: Path = Path("/Users/me/dev/pan-worktrees/task-3"),
    terminal_name: str = "pan-task-3",
    source: ThreadSource = ThreadSource.SLACK,
) -> ThreadRecord:
    created = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
    conversation = _slack(thread_ts) if source is ThreadSource.SLACK else _x(thread_ts)
    return ThreadRecord(
        conversation=conversation,
        terminal_name=terminal_name,
        terminal_address="t12",
        worktree_path=worktree_path,
        claude_session_id="session-123",
        transcript_path=Path("/home/.claude/projects/x/session-123.jsonl"),
        status=WorkerStatus.SPAWNING,
        created_at=created,
        updated_at=created,
    )


def _flat_payload(record: ThreadRecord) -> dict[str, object]:
    # The record as files written before Conversation existed hold it: the identity flat.
    payload = record.model_dump(mode="json")
    conversation = payload.pop("conversation")
    return {
        **payload,
        "thread_ts": conversation["id"],
        "channel": conversation["place"],
        "source": conversation["source"],
    }


def test_put_then_get_round_trips_through_disk(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    record = _record()

    thread_map.put(record)
    loaded = thread_map.get(record.conversation)

    assert loaded is not None
    assert loaded.terminal_name == "pan-task-3"
    assert loaded.terminal_address == "t12"
    assert loaded.worktree_path == Path("/Users/me/dev/pan-worktrees/task-3")
    assert loaded.claude_session_id == "session-123"
    assert loaded.transcript_path == Path("/home/.claude/projects/x/session-123.jsonl")
    assert loaded.status is WorkerStatus.SPAWNING
    assert loaded.created_at == record.created_at


def test_update_status_transitions_and_bumps_updated_at(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    record = _record()
    thread_map.put(record)

    later = datetime(2026, 7, 16, 11, 30, 0, tzinfo=UTC)
    clock.set(later)
    thread_map.update_status(record.conversation, WorkerStatus.RUNNING)

    updated = thread_map.get(record.conversation)
    assert updated is not None
    assert updated.status is WorkerStatus.RUNNING
    assert updated.updated_at == later
    # Only status and updated_at change — every other field survives the
    # read-modify-write cycle unchanged.
    assert updated.created_at == record.created_at
    assert updated.terminal_name == record.terminal_name
    assert updated.claude_session_id == record.claude_session_id
    assert updated.worktree_path == record.worktree_path
    assert updated.terminal_address == record.terminal_address


def test_get_unknown_thread_returns_none(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )

    assert thread_map.get(_slack("nope")) is None


def test_update_status_unknown_thread_raises(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )

    with pytest.raises(ThreadNotFoundError):
        thread_map.update_status(_slack("nope"), WorkerStatus.DONE)


def test_put_is_idempotent_upsert(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.put(_record("t1"))
    thread_map.put(_record("t2"))

    replacement = _record("t1")
    replacement.terminal_address = "t99"
    thread_map.put(replacement)

    fetched = thread_map.get(_slack("t1"))
    assert fetched is not None
    assert fetched.terminal_address == "t99"
    assert thread_map.get(_slack("t2")) is not None


def test_get_by_worktree_returns_matching_record(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.put(_record("t1", worktree_path=Path("/wt/pan-a")))
    thread_map.put(_record("t2", worktree_path=Path("/wt/pan-b")))

    found = thread_map.get_by_worktree(Path("/wt/pan-b"))

    assert found is not None
    assert found.conversation.id == "t2"


def test_get_by_worktree_matches_across_symlinked_prefix(tmp_path: Path) -> None:
    # A worker's cwd delivered by a Claude Code hook can differ from the stored
    # worktree_path only by a symlinked prefix (on macOS /tmp -> /private/tmp). The
    # lookup must still resolve to the record. Build a real symlink so resolve()
    # actually collapses it rather than relying on a platform-specific path.
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    real_dir = tmp_path / "real"
    worktree = real_dir / "pan-a"
    worktree.mkdir(parents=True)
    linked_prefix = tmp_path / "linked"
    linked_prefix.symlink_to(real_dir)

    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    # Stored via the symlinked prefix, looked up via the resolved real path.
    thread_map.put(_record("t1", worktree_path=linked_prefix / "pan-a"))

    found = thread_map.get_by_worktree(worktree)

    assert found is not None
    assert found.conversation.id == "t1"


def test_get_by_worktree_returns_none_when_no_match(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.put(_record("t1", worktree_path=Path("/wt/pan-a")))

    assert thread_map.get_by_worktree(Path("/wt/nope")) is None


def test_read_tolerates_unparseable_legacy_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A record written before `channel` became required must not poison the whole
    # read: the valid neighbour is still routable, the bad one is skipped.
    valid_ts = "t-valid"
    legacy_ts = "t-legacy"
    valid_record = _record(valid_ts, worktree_path=Path("/wt/pan-valid"))
    legacy_payload = _flat_payload(_record(legacy_ts))
    del legacy_payload["channel"]

    threads_path = tmp_path / "threads.json"
    threads_path.write_text(
        json.dumps(
            {
                valid_ts: valid_record.model_dump(mode="json"),
                legacy_ts: legacy_payload,
            }
        )
    )

    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(threads_path, clock)

    # propagate=False on this logger, so attach caplog's handler directly.
    threadmap_logger = logging.getLogger("pan.data.threadmap")
    threadmap_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="pan.data.threadmap"):
            loaded_valid = thread_map.get(_slack(valid_ts))
            loaded_legacy = thread_map.get(_slack(legacy_ts))
            found = thread_map.get_by_worktree(Path("/wt/pan-valid"))
    finally:
        threadmap_logger.removeHandler(caplog.handler)

    assert loaded_valid is not None
    assert loaded_valid.conversation.id == valid_ts
    assert loaded_legacy is None
    assert found is not None
    assert found.conversation.id == valid_ts
    assert any(legacy_ts in record.message for record in caplog.records)


def test_record_session_sets_session_fields_and_bumps_updated_at(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.put(_record())
    later = datetime(2026, 7, 16, 10, 5, 0, tzinfo=UTC)
    clock.set(later)

    thread_map.record_session(
        _slack("1718000000.000100"), "session-new", Path("/t/session-new.jsonl")
    )

    updated = thread_map.get(_slack("1718000000.000100"))
    assert updated is not None
    assert updated.claude_session_id == "session-new"
    assert updated.transcript_path == Path("/t/session-new.jsonl")
    assert updated.updated_at == later
    assert updated.status is WorkerStatus.SPAWNING


def test_record_session_unknown_thread_raises(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )

    with pytest.raises(ThreadNotFoundError):
        thread_map.record_session(_slack("nope"), "session", None)


def test_a_record_written_before_session_control_loads_with_defaults(tmp_path: Path) -> None:
    session_control_keys = {
        "source",
        "tied",
        "started_by_pan",
        "requester",
        "reply_mode",
        "inbox_thread_name",
        "inbox_thread_id",
        "inbox_thread_url",
        "status_message_ts",
        "status_label",
        "task_started_at",
        "last_answer_digest",
    }
    older_payload = {
        key: value
        for key, value in _flat_payload(_record("t1")).items()
        if key not in session_control_keys
    }
    threads_path = tmp_path / "threads.json"
    threads_path.write_text(json.dumps({"t1": older_payload}))

    loaded = FileThreadMap(threads_path, FakeClock(datetime(2026, 7, 16, tzinfo=UTC))).get(
        _slack("t1")
    )

    assert loaded is not None
    assert loaded.conversation.source is ThreadSource.SLACK
    assert loaded.tied is True
    assert loaded.started_by_pan is True
    assert loaded.requester is None
    assert loaded.reply_mode is ReplyMode.ON_DEMAND
    assert loaded.inbox_thread_name is None
    assert loaded.inbox_thread_id is None
    assert loaded.inbox_thread_url is None
    assert loaded.status_message_ts is None
    assert loaded.status_label is None
    assert loaded.task_started_at is None
    assert loaded.last_answer_digest is None


def test_tie_moves_a_session_from_its_old_thread(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.tie(_record("t-old", terminal_name="pan-7"))
    thread_map.tie(_record("t-other", terminal_name="pan-8"))
    later = datetime(2026, 7, 16, 11, 0, 0, tzinfo=UTC)
    clock.set(later)

    thread_map.tie(_record("t-new", terminal_name="pan-7"))

    old = thread_map.get(_slack("t-old"))
    assert old is not None
    assert old.tied is False
    assert old.updated_at == later
    assert [record.conversation.id for record in thread_map.records() if record.tied] == [
        "t-other",
        "t-new",
    ]


def test_tie_marks_the_stored_record_tied(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )
    untied = _record("t1").model_copy(update={"tied": False})

    thread_map.tie(untied)

    loaded = thread_map.get(_slack("t1"))
    assert loaded is not None
    assert loaded.tied is True


def test_untie_keeps_the_record_and_never_touches_the_session(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.tie(_record("t1"))

    thread_map.untie(_slack("t1"))

    loaded = thread_map.get(_slack("t1"))
    assert loaded is not None
    assert loaded.tied is False
    assert loaded.status is WorkerStatus.SPAWNING
    assert loaded.terminal_address == "t12"


def test_get_by_worktree_prefers_the_tied_record(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )
    thread_map.tie(_record("t-old", worktree_path=Path("/wt/pan-a")))
    thread_map.untie(_slack("t-old"))
    thread_map.tie(_record("t-new", worktree_path=Path("/wt/pan-a"), terminal_name="pan-b"))

    found = thread_map.get_by_worktree(Path("/wt/pan-a"))

    assert found is not None
    assert found.conversation.id == "t-new"


def test_record_inbox_thread_stores_the_id_and_link(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.tie(_record("t1"))
    later = datetime(2026, 7, 16, 10, 1, 0, tzinfo=UTC)
    clock.set(later)

    thread_map.record_inbox_thread(_slack("t1"), "thr_1", "https://inbox.example/t/thr_1")

    loaded = thread_map.get(_slack("t1"))
    assert loaded is not None
    assert loaded.inbox_thread_id == "thr_1"
    assert loaded.inbox_thread_url == "https://inbox.example/t/thr_1"
    assert loaded.updated_at == later


def test_record_status_message_and_task_store_the_status_fields(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.tie(_record("t1"))
    later = datetime(2026, 7, 16, 10, 1, 0, tzinfo=UTC)
    clock.set(later)

    thread_map.record_status_message(_slack("t1"), "1718000000.000900", session_name="pan-task-3")
    thread_map.record_task(_slack("t1"), "fix the login page", later, session_name="pan-task-3")

    loaded = thread_map.get(_slack("t1"))
    assert loaded is not None
    assert loaded.status_message_ts == "1718000000.000900"
    assert loaded.status_label == "fix the login page"
    assert loaded.task_started_at == later
    assert loaded.updated_at == later


@pytest.mark.parametrize(
    "operation",
    [
        lambda thread_map: thread_map.untie(_slack("nope")),
        lambda thread_map: thread_map.record_inbox_thread(
            _slack("nope"), "thr_1", "https://inbox.example"
        ),
        lambda thread_map: thread_map.record_status_message(_slack("nope"), "1718000000.000900"),
        lambda thread_map: thread_map.record_task(
            _slack("nope"), "fix it", datetime(2026, 7, 16, tzinfo=UTC)
        ),
    ],
    ids=["untie", "record-inbox-thread", "record-status-message", "record-task"],
)
def test_thread_operations_on_an_unknown_thread_raise(tmp_path: Path, operation: object) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )

    with pytest.raises(ThreadNotFoundError):
        operation(thread_map)  # type: ignore[operator]


def test_tie_note_names_the_session_and_its_state() -> None:
    record = _record("t1", terminal_name="pan-7").model_copy(update={"status": WorkerStatus.IDLE})

    assert tie_note(record) == (
        "this thread's work belongs to pan-7 (idle); send follow-ups there; "
        "never start a second session for the same work"
    )


@pytest.mark.parametrize("tied", [False, None], ids=["untied", "no-record"])
def test_tie_note_is_none_for_an_untied_thread(tied: bool | None) -> None:
    record = None if tied is None else _record("t1").model_copy(update={"tied": tied})

    assert tie_note(record) is None


def test_a_new_session_in_a_tied_thread_keeps_the_old_sessions_record(tmp_path: Path) -> None:
    # `new:` in a tied thread (or `to <name>:` naming another session) ties the thread to
    # another session. The old session's record stays, untied, and its hooks still reach it.
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.tie(_record("t1", worktree_path=Path("/wt/pan-7"), terminal_name="pan-7"))
    clock.set(datetime(2026, 7, 16, 11, 0, 0, tzinfo=UTC))

    thread_map.tie(_record("t1", worktree_path=Path("/wt/pan-8"), terminal_name="pan-8"))

    current = thread_map.get(_slack("t1"))
    assert current is not None
    assert current.terminal_name == "pan-8"
    old = thread_map.get(_slack("t1"), session_name="pan-7")
    assert old is not None
    assert old.tied is False
    found_by_hooks = thread_map.get_by_worktree(Path("/wt/pan-7"))
    assert found_by_hooks is not None
    assert found_by_hooks.terminal_name == "pan-7"

    thread_map.update_status(_slack("t1"), WorkerStatus.IDLE, session_name="pan-7")

    still_current = thread_map.get(_slack("t1"))
    assert still_current is not None
    assert still_current.terminal_name == "pan-8"
    assert still_current.status is WorkerStatus.SPAWNING
    updated_old = thread_map.get(_slack("t1"), session_name="pan-7")
    assert updated_old is not None
    assert updated_old.status is WorkerStatus.IDLE


def test_a_file_keyed_by_thread_alone_loads_and_migrates_on_write(tmp_path: Path) -> None:
    threads_path = tmp_path / "threads.json"
    older_file = {"t1": _flat_payload(_record("t1"))}
    threads_path.write_text(json.dumps(older_file))
    thread_map = FileThreadMap(threads_path, FakeClock(datetime(2026, 7, 16, tzinfo=UTC)))

    loaded = thread_map.get(_slack("t1"))
    thread_map.update_status(_slack("t1"), WorkerStatus.IDLE)

    assert loaded is not None
    assert loaded.terminal_name == "pan-task-3"
    assert list(json.loads(threads_path.read_text())) == ["slack:t1:pan-task-3"]
    reloaded = thread_map.get(_slack("t1"))
    assert reloaded is not None
    assert reloaded.status is WorkerStatus.IDLE


def test_get_falls_back_to_the_newest_record_of_an_untied_thread(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.tie(_record("t1", terminal_name="pan-7"))
    clock.set(datetime(2026, 7, 16, 11, 0, 0, tzinfo=UTC))
    thread_map.tie(_record("t1", terminal_name="pan-8"))
    clock.set(datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC))

    thread_map.untie(_slack("t1"))

    newest = thread_map.get(_slack("t1"))
    assert newest is not None
    assert newest.terminal_name == "pan-8"
    assert [record.tied for record in thread_map.records()] == [False, False]


def test_get_by_worktree_ignores_records_of_sessions_pan_did_not_start(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )
    desk_record = _record("t1", worktree_path=Path("/desk/api"), terminal_name="desk-api")
    thread_map.tie(desk_record.model_copy(update={"started_by_pan": False}))

    assert thread_map.get_by_worktree(Path("/desk/api")) is None


def test_record_inbox_thread_without_a_link_keeps_the_known_one(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )
    thread_map.tie(_record("t1").model_copy(update={"inbox_thread_url": "https://inbox.example/a"}))

    thread_map.record_inbox_thread(_slack("t1"), "thr_1", None)

    loaded = thread_map.get(_slack("t1"))
    assert loaded is not None
    assert loaded.inbox_thread_id == "thr_1"
    assert loaded.inbox_thread_url == "https://inbox.example/a"


def test_a_slack_thread_and_an_x_post_with_the_same_id_are_tied_apart(tmp_path: Path) -> None:
    thread_map = FileThreadMap(
        tmp_path / "threads.json", FakeClock(datetime(2026, 7, 16, tzinfo=UTC))
    )
    thread_map.tie(_record("1700000000", terminal_name="pan-slack"))

    thread_map.tie(_record("1700000000", terminal_name="pan-x", source=ThreadSource.X))

    slack_record = thread_map.get(_slack("1700000000"))
    x_record = thread_map.get(_x("1700000000"))
    assert slack_record is not None
    assert (slack_record.terminal_name, slack_record.tied) == ("pan-slack", True)
    assert x_record is not None
    assert (x_record.terminal_name, x_record.tied) == ("pan-x", True)


@pytest.mark.parametrize(
    "operation",
    [
        lambda thread_map: thread_map.untie(_x("1700000000")),
        lambda thread_map: thread_map.update_status(_x("1700000000"), WorkerStatus.DONE),
        lambda thread_map: thread_map.set_reply_mode(_x("1700000000"), ReplyMode.SLACK),
        lambda thread_map: thread_map.record_inbox_thread(_x("1700000000"), "thr_x", None),
        lambda thread_map: thread_map.record_status_message(_x("1700000000"), "1718000000.9"),
        lambda thread_map: thread_map.record_posted_answer(_x("1700000000"), "digest"),
        lambda thread_map: thread_map.record_task(
            _x("1700000000"), "x task", datetime(2026, 7, 17, tzinfo=UTC)
        ),
        lambda thread_map: thread_map.record_session(_x("1700000000"), "session-x", None),
    ],
    ids=[
        "untie",
        "update-status",
        "set-reply-mode",
        "record-inbox-thread",
        "record-status-message",
        "record-posted-answer",
        "record-task",
        "record-session",
    ],
)
def test_a_change_to_an_x_post_leaves_the_slack_thread_with_the_same_id_alone(
    tmp_path: Path, operation: object
) -> None:
    clock = FakeClock(datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC))
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.tie(_record("1700000000", terminal_name="pan-slack"))
    thread_map.tie(_record("1700000000", terminal_name="pan-x", source=ThreadSource.X))
    slack_before = thread_map.get(_slack("1700000000"))
    x_before = thread_map.get(_x("1700000000"))
    clock.set(datetime(2026, 7, 16, 11, 0, 0, tzinfo=UTC))

    operation(thread_map)  # type: ignore[operator]

    assert thread_map.get(_slack("1700000000")) == slack_before
    assert thread_map.get(_x("1700000000")) != x_before


@pytest.mark.parametrize(
    ("stored_source", "expected_conversation"),
    [
        (None, Conversation.slack("t1", "C0001")),
        ("slack", Conversation.slack("t1", "C0001")),
        ("x", Conversation(source=ThreadSource.X, id="t1", place="C0001")),
    ],
    ids=["no-source", "slack", "x"],
)
def test_a_flat_record_from_before_conversation_loads_and_is_rewritten_in_the_new_shape(
    tmp_path: Path, stored_source: str | None, expected_conversation: Conversation
) -> None:
    flat_payload = _flat_payload(_record("t1"))
    del flat_payload["source"]
    if stored_source is not None:
        flat_payload["source"] = stored_source
    threads_path = tmp_path / "threads.json"
    threads_path.write_text(json.dumps({"t1": flat_payload}))
    thread_map = FileThreadMap(threads_path, FakeClock(datetime(2026, 7, 16, tzinfo=UTC)))

    loaded = thread_map.get(expected_conversation)
    thread_map.update_status(expected_conversation, WorkerStatus.IDLE)

    assert loaded is not None
    assert loaded.conversation == expected_conversation
    (stored,) = json.loads(threads_path.read_text()).values()
    assert stored["conversation"] == expected_conversation.model_dump(mode="json")
    assert not {"thread_ts", "channel", "source"} & set(stored)
    reloaded = thread_map.get(expected_conversation)
    assert reloaded is not None
    assert reloaded.status is WorkerStatus.IDLE
