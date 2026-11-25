from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from git_worktree_fakes import FakeGitWorktree
from reporter_fakes import RecordingReporter
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.backend.spawn import (
    RELAYED_FOLLOW_UP_SENTENCE,
    ClaudeLauncher,
    inbox_thread_name,
    reopen_worker,
    slack_thread_paragraph,
    spawn_worker,
    worker_brief,
    worker_terminal_name,
)
from pan.core.errors import (
    NotPanSessionError,
    PanError,
    SpawnError,
    TerminalHostError,
    TerminalNameTakenError,
)
from pan.core.events import (
    PrivateDetails,
    SessionNotice,
    SessionNoticeReason,
    StartAction,
    StartFailed,
    StatusChanged,
)
from pan.core.models import (
    Conversation,
    ReplyMode,
    StatusState,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.data.adapters.hc import HcError

_FIXED_NOW = datetime(2026, 7, 16, 10, 0, 0, tzinfo=UTC)
_THREAD = "1718000000.000200"


def _fake_git(timeline: list[str], *, fail: bool = False) -> FakeGitWorktree:
    # The shared fake, its create step recorded on the terminal host's timeline.
    errors: dict[str, PanError] = {"create": SpawnError("git failed")} if fail else {}
    return FakeGitWorktree(timeline=timeline, errors=errors)


class FakeThreadMap:
    def __init__(self) -> None:
        self.records: dict[str, ThreadRecord] = {}
        self.ties: list[str] = []

    def get(self, conversation: Conversation) -> ThreadRecord | None:
        return self.records.get(conversation.id)

    def put(self, record: ThreadRecord) -> None:
        self.records[record.conversation.id] = record

    def tie(self, record: ThreadRecord) -> None:
        self.ties.append(record.conversation.id)
        self.records[record.conversation.id] = record

    def record_status_message(
        self, conversation: Conversation, message_ts: str, *, session_name: str | None = None
    ) -> None:
        record = self.records[conversation.id]
        self.records[conversation.id] = record.model_copy(update={"status_message_ts": message_ts})


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
    git: FakeGitWorktree,
    thread_map: FakeThreadMap | None = None,
    reporter: RecordingReporter | None = None,
    stream: str | None = "backend",
    app_space: str | None = None,
    task: str = "do the thing",
    request_text: str | None = None,
    reply_in_slack: bool = False,
    from_thread_reply: bool = False,
) -> ThreadRecord:
    return spawn_worker(
        conversation=Conversation.slack(_THREAD, "C1"),
        task=task,
        repo=Path("/repo"),
        base=base,
        stream=stream,
        app_space=app_space,
        git=git,
        terminal_host=terminal_host,
        launcher=ClaudeLauncher(terminal_host),
        thread_map=thread_map if thread_map is not None else FakeThreadMap(),
        reporter=reporter if reporter is not None else RecordingReporter(),
        clock=FakeClock(),
        id_gen=FakeIdGen(),
        requester="U0TEST",
        request_text=request_text,
        reply_in_slack=reply_in_slack,
        from_thread_reply=from_thread_reply,
    )


def test_spawn_worker_runs_claude_in_a_labelled_terminal_and_records_it(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    git = _fake_git(terminal_host.timeline)
    thread_map, reporter = FakeThreadMap(), RecordingReporter()

    record = _spawn(
        base=tmp_path,
        terminal_host=terminal_host,
        git=git,
        thread_map=thread_map,
        reporter=reporter,
    )

    assert terminal_host.timeline == ["worktree", "run"]
    (run,) = terminal_host.runs
    assert run.name == "pan-backend"
    assert run.cwd == tmp_path / "pan-backend"
    # The brief is one argv element after `claude`: never parsed as shell text. It opens
    # with the fixed Inbox preamble naming the session's Inbox thread.
    assert run.argv == [
        "claude",
        worker_brief("pan-backend: do the thing", "do the thing", ReplyMode.ON_DEMAND),
    ]
    assert run.argv[1].endswith("\n\nTask:\ndo the thing")
    assert 'in a thread named exactly "pan-backend: do the thing"' in run.argv[1]
    assert "Do not post to Slack yourself" in run.argv[1]
    assert run.labels == {"pan.role": "worker", "pan.thread": _THREAD, "pan.channel": "C1"}
    assert record.status is WorkerStatus.SPAWNING
    assert record.terminal_name == "pan-backend"
    assert record.terminal_address == "t101"
    assert record.conversation == Conversation.slack(_THREAD, "C1")
    assert record.worktree_path == tmp_path / "pan-backend"
    assert record.repo_path == Path("/repo")
    assert record.created_at == _FIXED_NOW
    assert record.tied is True
    assert record.started_by_pan is True
    assert record.requester == "U0TEST"
    assert record.reply_mode is ReplyMode.ON_DEMAND
    assert record.inbox_thread_name == "pan-backend: do the thing"
    assert record.inbox_thread_id is None
    assert record.status_label == "do the thing"
    assert record.task_started_at == _FIXED_NOW
    # The record ties the thread to the session (INV-7). The thread gets one status (Working)
    # and nothing else; the Inbox thread and terminal go only to the requester.
    assert record.status_message_ts == "status-1"
    assert thread_map.records[_THREAD] == record
    assert thread_map.ties == [_THREAD]
    assert reporter.reports == [
        (
            Conversation.slack(_THREAD, "C1"),
            StatusChanged(
                state=StatusState.WORKING,
                session_name="pan-backend",
                label="do the thing",
                task_started_at=_FIXED_NOW,
                updated_at=_FIXED_NOW,
            ),
        ),
        (
            Conversation.slack(_THREAD, "C1"),
            PrivateDetails(
                requester="U0TEST",
                session_name="pan-backend",
                terminal_address="t101",
                inbox_thread_name="pan-backend: do the thing",
                inbox_thread_known=True,
            ),
        ),
    ]


@pytest.mark.parametrize(
    "request_text, task, reply_in_slack, expected_mode",
    [
        (
            "<@U0TEST> fix the login page, Reply in Slack",
            "fix the login page",
            False,
            ReplyMode.SLACK,
        ),
        ("<@U0TEST> fix the login page", "fix the login page", False, ReplyMode.ON_DEMAND),
        (None, "fix the login page and reply in slack", False, ReplyMode.SLACK),
        (None, "fix the login page", True, ReplyMode.SLACK),
    ],
    ids=["phrase-in-request", "no-phrase", "phrase-in-task-fallback", "forced"],
)
def test_spawn_sets_the_reply_mode_from_the_request(
    tmp_path: Path,
    request_text: str | None,
    task: str,
    reply_in_slack: bool,
    expected_mode: ReplyMode,
) -> None:
    terminal_host = FakeTerminalHost()
    reporter = RecordingReporter()

    record = _spawn(
        base=tmp_path,
        terminal_host=terminal_host,
        git=_fake_git(terminal_host.timeline),
        reporter=reporter,
        task=task,
        request_text=request_text,
        reply_in_slack=reply_in_slack,
    )

    assert record.reply_mode is expected_mode
    (status,) = reporter.of_type(StatusChanged)
    assert status.replies_in_thread is (expected_mode is ReplyMode.SLACK)
    (run,) = terminal_host.runs
    assert run.argv[1] == worker_brief(record.inbox_thread_name or "", task, expected_mode)


@pytest.mark.parametrize(
    "task, expected_name",
    [
        ("do the thing", "pan-7: do the thing"),
        ("  fix\n the   login\tpage ", "pan-7: fix the login page"),
        ("x" * 80, "pan-7: " + "x" * 57 + "..."),
        ("", "pan-7"),
    ],
    ids=["short", "whitespace-collapsed", "long-cut", "empty"],
)
def test_inbox_thread_name_is_one_short_line(task: str, expected_name: str) -> None:
    assert inbox_thread_name("pan-7", task) == expected_name


def test_spawn_worker_labels_into_the_app_space_only_when_configured(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()

    _spawn(
        base=tmp_path,
        terminal_host=terminal_host,
        git=_fake_git(terminal_host.timeline),
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

    record = _spawn(
        base=tmp_path, terminal_host=terminal_host, git=_fake_git(terminal_host.timeline)
    )

    settings = json.loads((record.worktree_path / ".claude" / "settings.json").read_text())
    commands = {
        event: settings["hooks"][event][0]["hooks"][0]["command"]
        for event in ("SessionStart", "Stop", "Notification", "PostToolUse")
    }
    assert commands == {
        "SessionStart": "pan hook session-start",
        "Stop": "pan hook stop",
        "Notification": "pan hook notification",
        "PostToolUse": "pan hook inbox-link",
    }
    assert settings["hooks"]["PostToolUse"][0]["matcher"] == (
        "^(mcp__plannotator__plannotator_inbox|REPL)$"
    )


@pytest.mark.parametrize(
    "tool_name, expected_match",
    [
        ("mcp__plannotator__plannotator_inbox", True),
        ("REPL", True),
        ("Bash", False),
        ("mcp__plannotator__plannotator", False),
        ("REPL_extra", False),
        ("mcp__other__REPL", False),
    ],
    ids=[
        "inbox-tool",
        "code-mode-repl",
        "other-tool",
        "other-plannotator-tool",
        "repl-prefix",
        "repl-suffix",
    ],
)
def test_worker_inbox_link_matcher_matches_the_inbox_tool_and_the_repl_tool(
    tmp_path: Path, tool_name: str, expected_match: bool
) -> None:
    # Claude Code matches a hook's matcher as a regex; the matcher is anchored, so a search
    # and a whole-name match agree.
    terminal_host = FakeTerminalHost()
    record = _spawn(
        base=tmp_path, terminal_host=terminal_host, git=_fake_git(terminal_host.timeline)
    )
    settings = json.loads((record.worktree_path / ".claude" / "settings.json").read_text())
    matcher = settings["hooks"]["PostToolUse"][0]["matcher"]

    assert (re.search(matcher, tool_name) is not None) is expected_match


def test_a_live_terminal_holding_the_name_fails_before_any_worktree(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    terminal_host.add(make_terminal("t5", name="pan-backend"))
    thread_map, reporter = FakeThreadMap(), RecordingReporter()

    with pytest.raises(SpawnError, match="stream name in use"):
        _spawn(
            base=tmp_path,
            terminal_host=terminal_host,
            git=_fake_git(terminal_host.timeline),
            thread_map=thread_map,
            reporter=reporter,
        )

    assert terminal_host.timeline == []
    assert thread_map.records[_THREAD].status is WorkerStatus.FAILED
    # No session started, so the thread is left untied.
    assert thread_map.records[_THREAD].tied is False
    assert thread_map.ties == []
    assert reporter.events() == [
        StartFailed(action=StartAction.SPAWN, session_name="pan-backend", name_in_use=True)
    ]


def test_a_finished_terminal_with_the_name_does_not_block_spawn(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    terminal_host.add(make_terminal("t5", name="pan-backend", state=TerminalState.FINISHED))

    record = _spawn(
        base=tmp_path, terminal_host=terminal_host, git=_fake_git(terminal_host.timeline)
    )

    assert record.status is WorkerStatus.SPAWNING


@pytest.mark.parametrize(
    "run_error",
    [TerminalHostError("spawn_failed"), HcError("spawn_failed"), TerminalNameTakenError("raced")],
)
def test_terminal_host_failure_wraps_as_spawn_error_and_marks_failed(
    tmp_path: Path, run_error: TerminalHostError
) -> None:
    terminal_host = FakeTerminalHost(errors={"run": run_error})
    thread_map, reporter = FakeThreadMap(), RecordingReporter()

    with pytest.raises(SpawnError):
        _spawn(
            base=tmp_path,
            terminal_host=terminal_host,
            git=_fake_git(terminal_host.timeline),
            thread_map=thread_map,
            reporter=reporter,
        )

    failed = thread_map.records[_THREAD]
    assert failed.status is WorkerStatus.FAILED
    assert failed.terminal_address == ""
    assert failed.conversation == Conversation.slack(_THREAD, "C1")
    name_in_use = isinstance(run_error, TerminalNameTakenError)
    assert reporter.reports == [
        (
            Conversation.slack(_THREAD, "C1"),
            StartFailed(
                action=StartAction.SPAWN, session_name="pan-backend", name_in_use=name_in_use
            ),
        )
    ]


def test_git_failure_wraps_as_spawn_error_and_never_runs(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    thread_map = FakeThreadMap()

    with pytest.raises(SpawnError):
        _spawn(
            base=tmp_path,
            terminal_host=terminal_host,
            git=_fake_git(terminal_host.timeline, fail=True),
            thread_map=thread_map,
        )

    assert thread_map.records[_THREAD].status is WorkerStatus.FAILED
    assert thread_map.records[_THREAD].repo_path == Path("/repo")
    assert terminal_host.timeline == ["worktree"]


def _finished_record(
    worktree_path: Path, status: WorkerStatus = WorkerStatus.FAILED
) -> ThreadRecord:
    return ThreadRecord(
        conversation=Conversation.slack(_THREAD, "C1"),
        terminal_name="pan-backend",
        terminal_address="t50",
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
    reporter: RecordingReporter,
    *,
    reply_in_slack: bool = False,
) -> ThreadRecord:
    return reopen_worker(
        record=record,
        task="also update the docs",
        app_space=None,
        terminal_host=terminal_host,
        launcher=ClaudeLauncher(terminal_host),
        thread_map=thread_map,
        reporter=reporter,
        clock=LaterClock(),
        reply_in_slack=reply_in_slack,
    )


@pytest.mark.parametrize("status", [WorkerStatus.FAILED, WorkerStatus.DONE])
def test_reopen_continues_the_conversation_in_the_same_worktree(
    tmp_path: Path, status: WorkerStatus
) -> None:
    terminal_host, thread_map, reporter = FakeTerminalHost(), FakeThreadMap(), RecordingReporter()
    record = _finished_record(tmp_path, status)

    reopened = _reopen(record, terminal_host, thread_map, reporter)

    (run,) = terminal_host.runs
    assert run.name == "pan-backend"
    assert run.cwd == tmp_path
    assert run.argv == ["claude", "--continue", "also update the docs"]
    assert run.labels["pan.thread"] == _THREAD
    assert reopened.terminal_address == "t101"
    assert reopened.status is WorkerStatus.SPAWNING
    assert reopened.claude_session_id == "s-1"
    assert reopened.created_at == _FIXED_NOW
    assert reopened.status_label == "also update the docs"
    assert thread_map.records[_THREAD] == reopened
    assert thread_map.ties == [_THREAD]
    # The thread's status message says Working again; nothing else is reported.
    assert reporter.states() == [StatusState.WORKING]
    assert [status.label for status in reporter.of_type(StatusChanged)] == ["also update the docs"]
    assert len(reporter.events()) == 1


def test_reopen_edits_the_status_message_the_thread_already_has(tmp_path: Path) -> None:
    terminal_host, thread_map, reporter = FakeTerminalHost(), FakeThreadMap(), RecordingReporter()
    record = _finished_record(tmp_path).model_copy(
        update={"status_message_ts": "1718000000.000300"}
    )

    reopened = _reopen(record, terminal_host, thread_map, reporter)

    assert [status.status_message_id for status in reporter.of_type(StatusChanged)] == [
        "1718000000.000300"
    ]
    assert reopened.status_message_ts == "1718000000.000300"


@pytest.mark.parametrize(
    "missing_worktree, live_name_holder",
    [(True, False), (False, True)],
    ids=["worktree-missing", "name-in-use"],
)
def test_reopen_failure_is_reported_and_leaves_the_record(
    tmp_path: Path, missing_worktree: bool, live_name_holder: bool
) -> None:
    terminal_host, thread_map, reporter = FakeTerminalHost(), FakeThreadMap(), RecordingReporter()
    if live_name_holder:
        terminal_host.add(make_terminal("t5", name="pan-backend"))
    worktree = tmp_path / "gone" if missing_worktree else tmp_path
    record = _finished_record(worktree)

    with pytest.raises(SpawnError):
        _reopen(record, terminal_host, thread_map, reporter)

    assert terminal_host.runs == []
    assert thread_map.records == {}
    assert reporter.events() == [
        StartFailed(
            action=StartAction.REOPEN, session_name="pan-backend", name_in_use=live_name_holder
        )
    ]


def test_reopen_never_restarts_a_session_pan_did_not_start(tmp_path: Path) -> None:
    terminal_host, thread_map, reporter = FakeTerminalHost(), FakeThreadMap(), RecordingReporter()
    record = _finished_record(tmp_path).model_copy(
        update={"terminal_name": "desk-api", "started_by_pan": False}
    )

    with pytest.raises(NotPanSessionError):
        _reopen(record, terminal_host, thread_map, reporter)

    assert terminal_host.runs == []
    assert thread_map.records == {}
    assert reporter.events() == [
        SessionNotice(reason=SessionNoticeReason.DESK_SESSION_ENDED, session_name="desk-api")
    ]


@pytest.mark.parametrize(
    "reply_mode, expected_sentence, unwanted_text",
    [
        (
            ReplyMode.SLACK,
            "Do not post to Slack yourself: pan posts your final answer to this Slack thread "
            "for you.",
            "only when the person asks",
        ),
        (
            ReplyMode.ON_DEMAND,
            "Do not post to Slack yourself: the Slack thread gets a reply only when the person "
            "asks for one, and pan posts it for you.",
            "posts your final answer",
        ),
    ],
    ids=["slack", "on-demand"],
)
def test_worker_brief_says_who_replies_in_slack(
    reply_mode: ReplyMode, expected_sentence: str, unwanted_text: str
) -> None:
    brief = worker_brief("pan-a: do the thing", "do the thing", reply_mode)

    preamble, task = brief.split("\n\nTask:\n")
    assert task == "do the thing"
    assert preamble.endswith(f"{expected_sentence} {RELAYED_FOLLOW_UP_SENTENCE}")
    assert unwanted_text not in preamble
    assert "Do not post to Slack." not in preamble


@pytest.mark.parametrize(
    ("from_thread_reply", "expects_thread_paragraph"),
    [(False, False), (True, True)],
    ids=["top-level-mention", "thread-reply"],
)
def test_spawn_brief_names_the_slack_thread_only_for_a_thread_reply(
    tmp_path: Path, from_thread_reply: bool, expects_thread_paragraph: bool
) -> None:
    terminal_host = FakeTerminalHost()

    _spawn(
        base=tmp_path,
        terminal_host=terminal_host,
        git=_fake_git(terminal_host.timeline),
        from_thread_reply=from_thread_reply,
    )

    (run,) = terminal_host.runs
    preamble, task = run.argv[1].split("\n\nTask:\n")
    assert task == "do the thing"
    assert ("channel C1" in preamble) is expects_thread_paragraph
    assert (f"thread ts {_THREAD}" in preamble) is expects_thread_paragraph
    assert ("slack_read_thread" in preamble) is expects_thread_paragraph


def test_slack_thread_paragraph_says_read_every_message_and_never_post() -> None:
    paragraph = slack_thread_paragraph("C1", _THREAD)

    assert "channel C1 with thread ts 1718000000.000200" in paragraph
    assert "every message, following pagination until there are no more" in paragraph
    assert "slack_read_thread" in paragraph
    assert "do not post to Slack" in paragraph
    assert "If the tool is unavailable, say so in the Inbox thread" in paragraph


@pytest.mark.parametrize(
    ("initial_mode", "reply_in_slack", "worktree_exists", "expected_mode"),
    [
        (ReplyMode.ON_DEMAND, True, True, ReplyMode.SLACK),
        (ReplyMode.ON_DEMAND, False, True, ReplyMode.ON_DEMAND),
        (ReplyMode.SLACK, False, True, ReplyMode.SLACK),
        (ReplyMode.ON_DEMAND, True, False, ReplyMode.ON_DEMAND),
    ],
    ids=[
        "flag-sets-slack",
        "no-flag-keeps-on-demand",
        "no-flag-never-downgrades",
        "failed-reopen-leaves-on-demand",
    ],
)
def test_reopen_reply_in_slack_is_saved_only_when_the_launch_succeeds(
    tmp_path: Path,
    initial_mode: ReplyMode,
    reply_in_slack: bool,
    worktree_exists: bool,
    expected_mode: ReplyMode,
) -> None:
    terminal_host, thread_map, reporter = FakeTerminalHost(), FakeThreadMap(), RecordingReporter()
    worktree = tmp_path if worktree_exists else tmp_path / "gone"
    record = _finished_record(worktree).model_copy(update={"reply_mode": initial_mode})
    thread_map.put(record)

    if worktree_exists:
        reopened = _reopen(
            record, terminal_host, thread_map, reporter, reply_in_slack=reply_in_slack
        )
        assert reopened.reply_mode is expected_mode
    else:
        with pytest.raises(SpawnError):
            _reopen(record, terminal_host, thread_map, reporter, reply_in_slack=reply_in_slack)

    assert thread_map.records[_THREAD].reply_mode is expected_mode
