from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from git_worktree_fakes import FakeGitWorktree
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.adapters.hc import HcError
from pan.errors import (
    NotPanSessionError,
    PanError,
    SpawnError,
    TerminalHostError,
    TerminalNameTakenError,
)
from pan.models import ReplyMode, TerminalState, ThreadRecord, ThreadSource, WorkerStatus
from pan.slack_format import to_slack_mrkdwn
from pan.spawn import (
    ClaudeLauncher,
    desk_session_ended_reply,
    inbox_thread_name,
    reopen_worker,
    spawn_worker,
    worker_brief,
    worker_terminal_name,
)

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

    def get(self, thread_ts: str) -> ThreadRecord | None:
        return self.records.get(thread_ts)

    def put(self, record: ThreadRecord) -> None:
        self.records[record.thread_ts] = record

    def tie(self, record: ThreadRecord) -> None:
        self.ties.append(record.thread_ts)
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
    git: FakeGitWorktree,
    thread_map: FakeThreadMap | None = None,
    slack: FakeSlack | None = None,
    stream: str | None = "backend",
    app_space: str | None = None,
    task: str = "do the thing",
    request_text: str | None = None,
    reply_in_slack: bool = False,
) -> ThreadRecord:
    return spawn_worker(
        thread_ts=_THREAD,
        channel="C1",
        task=task,
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
        requester="U0TEST",
        request_text=request_text,
        reply_in_slack=reply_in_slack,
    )


def test_spawn_worker_runs_claude_in_a_labelled_terminal_and_records_it(tmp_path: Path) -> None:
    terminal_host = FakeTerminalHost()
    git = _fake_git(terminal_host.timeline)
    thread_map, slack = FakeThreadMap(), FakeSlack()

    record = _spawn(
        base=tmp_path, terminal_host=terminal_host, git=git, thread_map=thread_map, slack=slack
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
    assert record.channel == "C1"
    assert record.worktree_path == tmp_path / "pan-backend"
    assert record.repo_path == Path("/repo")
    assert record.created_at == _FIXED_NOW
    assert record.source is ThreadSource.SLACK
    assert record.tied is True
    assert record.started_by_pan is True
    assert record.requester == "U0TEST"
    assert record.reply_mode is ReplyMode.ON_DEMAND
    assert record.inbox_thread_name == "pan-backend: do the thing"
    assert record.inbox_thread_id is None
    # The record ties the thread to the session (INV-7) and the one start notice goes via
    # egress, naming the session's Inbox thread.
    assert thread_map.records[_THREAD] is record
    assert thread_map.ties == [_THREAD]
    assert slack.posts == [
        (
            "C1",
            _THREAD,
            'Started pan-backend in a new worktree · Inbox thread: "pan-backend: do the thing"',
        )
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
    slack = FakeSlack()

    record = _spawn(
        base=tmp_path,
        terminal_host=terminal_host,
        git=_fake_git(terminal_host.timeline),
        slack=slack,
        task=task,
        request_text=request_text,
        reply_in_slack=reply_in_slack,
    )

    assert record.reply_mode is expected_mode
    assert slack.posts[0][2].endswith("answers will also be posted here") is (
        expected_mode is ReplyMode.SLACK
    )
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
    thread_map, slack = FakeThreadMap(), FakeSlack()

    with pytest.raises(SpawnError, match="stream name in use"):
        _spawn(
            base=tmp_path,
            terminal_host=terminal_host,
            git=_fake_git(terminal_host.timeline),
            thread_map=thread_map,
            slack=slack,
        )

    assert terminal_host.timeline == []
    assert thread_map.records[_THREAD].status is WorkerStatus.FAILED
    # No session started, so the thread is left untied.
    assert thread_map.records[_THREAD].tied is False
    assert thread_map.ties == []
    assert slack.posts == [
        ("C1", _THREAD, "spawn failed for stream pan-backend: stream name in use")
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
    thread_map, slack = FakeThreadMap(), FakeSlack()

    with pytest.raises(SpawnError):
        _spawn(
            base=tmp_path,
            terminal_host=terminal_host,
            git=_fake_git(terminal_host.timeline),
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
    assert thread_map.ties == [_THREAD]
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


def test_reopen_never_restarts_a_session_pan_did_not_start(tmp_path: Path) -> None:
    terminal_host, thread_map, slack = FakeTerminalHost(), FakeThreadMap(), FakeSlack()
    record = _finished_record(tmp_path).model_copy(
        update={"terminal_name": "desk-api", "started_by_pan": False}
    )

    with pytest.raises(NotPanSessionError):
        _reopen(record, terminal_host, thread_map, slack)

    assert terminal_host.runs == []
    assert thread_map.records == {}
    assert slack.posts == [("C1", _THREAD, to_slack_mrkdwn(desk_session_ended_reply("desk-api")))]


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
    assert preamble.endswith(expected_sentence)
    assert unwanted_text not in preamble
    assert "Do not post to Slack." not in preamble
