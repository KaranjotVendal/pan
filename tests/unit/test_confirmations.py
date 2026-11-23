from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from inbox_fakes import FakeInboxDirectory
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.confirmations import (
    EXPIRED_REPLY,
    NOT_SENT_REPLY,
    FilePendingConfirmations,
    cancel_confirmation,
    candidate_label,
    confirm_session,
    propose_sessions,
    question_text,
    route_with_pending,
)
from pan.errors import SessionBusyError, TargetNotFoundError
from pan.models import (
    AgentStatus,
    ConfirmationCandidate,
    PendingConfirmation,
    Route,
    RouteKind,
    Terminal,
    TerminalState,
    ThreadRecord,
    ThreadSource,
)
from pan.threadmap import FileThreadMap

_ASKED_AT = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)
_THREAD = "1700000000.000300"
_PAN_FOLDER = Path("/tmp/work/pan")
_DOCS_FOLDER = Path("/tmp/work/docs")


class SettableClock:
    def __init__(self, now: datetime = _ASKED_AT) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class RecordingSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        self.posts.append((channel, thread_ts, text))

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def texts(self) -> list[str]:
        return [text for _channel, _thread, text in self.posts]


def _pan_session(**updates: object) -> Terminal:
    terminal = make_terminal("t3", name="stream-pan", where="work / 1", cwd=_PAN_FOLDER)
    return terminal.model_copy(update=updates)


def _docs_session() -> Terminal:
    return make_terminal("t4", where="work / 2", cwd=_DOCS_FOLDER)


def _host(*terminals: Terminal) -> FakeTerminalHost:
    terminal_host = FakeTerminalHost()
    for terminal in terminals:
        terminal_host.add(terminal)
    return terminal_host


def _store(tmp_path: Path) -> FilePendingConfirmations:
    return FilePendingConfirmations(tmp_path / "confirmations.json")


def _question(
    *candidates: ConfirmationCandidate, asked_at: datetime = _ASKED_AT, thread_ts: str = _THREAD
) -> PendingConfirmation:
    return PendingConfirmation(
        thread_ts=thread_ts,
        channel="C0TEST",
        selector="pan docs",
        task="add the tests",
        requester="U0TEST",
        candidates=list(candidates),
        created_at=asked_at,
    )


_PAN_CANDIDATE = ConfirmationCandidate(
    address="t3", name="stream-pan", label="stream-pan (work / 1, folder pan)"
)
_DOCS_CANDIDATE = ConfirmationCandidate(address="t4", label="t4 (work / 2, folder docs)")


def _propose(
    tmp_path: Path, terminal_host: FakeTerminalHost, slack: RecordingSlack, *addresses: str
) -> PendingConfirmation | None:
    return propose_sessions(
        thread_ts=_THREAD,
        channel="C0TEST",
        selector="pan docs",
        task="add the tests",
        requester="U0TEST",
        reply_in_slack=False,
        candidate_addresses=list(addresses),
        terminal_host=terminal_host,
        confirmations=_store(tmp_path),
        slack=slack,
        clock=SettableClock(),
    )


def _confirm(
    tmp_path: Path,
    terminal_host: FakeTerminalHost,
    slack: RecordingSlack,
    choice: str,
    *,
    clock: SettableClock | None = None,
) -> ThreadRecord | None:
    return confirm_session(
        thread_ts=_THREAD,
        channel="C0TEST",
        choice=choice,
        terminal_host=terminal_host,
        thread_map=FileThreadMap(tmp_path / "threads.json", SettableClock()),
        inbox_directory=FakeInboxDirectory(),
        confirmations=_store(tmp_path),
        slack=slack,
        clock=clock if clock is not None else SettableClock(),
    )


def test_store_keeps_one_question_per_thread_and_drops_expired_ones(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put(_question(_DOCS_CANDIDATE, thread_ts="old", asked_at=_ASKED_AT - timedelta(hours=1)))
    store.put(_question(_DOCS_CANDIDATE))
    store.put(_question(_PAN_CANDIDATE))

    assert store.get(ThreadSource.SLACK, "old") is None
    assert store.get(ThreadSource.SLACK, _THREAD) == _question(_PAN_CANDIDATE)
    store.clear(ThreadSource.SLACK, _THREAD)
    assert store.get(ThreadSource.SLACK, _THREAD) is None


@pytest.mark.parametrize(
    "raw_text, question, minutes_later, expected_route, expected_cancels",
    [
        ("yes", _question(_PAN_CANDIDATE), 5, Route(kind=RouteKind.CONFIRM, choice="yes"), False),
        (
            "<@U0TEST> 2",
            _question(_PAN_CANDIDATE),
            5,
            Route(kind=RouteKind.CONFIRM, choice="2"),
            False,
        ),
        ("no thanks", _question(_PAN_CANDIDATE), 5, Route(text="no thanks"), True),
        ("yes", _question(_PAN_CANDIDATE), 30, Route(text="yes"), False),
        ("yes", None, 0, Route(text="yes"), False),
    ],
    ids=["yes", "number", "anything-else-cancels", "expired", "no-question"],
)
def test_route_with_pending(
    raw_text: str,
    question: PendingConfirmation | None,
    minutes_later: int,
    expected_route: Route,
    expected_cancels: bool,
) -> None:
    now = _ASKED_AT + timedelta(minutes=minutes_later)
    route = Route(text=raw_text.removeprefix("<@U0TEST> "))

    assert route_with_pending(route, raw_text, question, now) == (expected_route, expected_cancels)


@pytest.mark.parametrize(
    "terminal, expected_label",
    [
        (_pan_session(), "stream-pan (work / 1, folder pan)"),
        (make_terminal("t5", cwd=Path("/")), "t5"),
    ],
    ids=["name-label-folder", "address-only"],
)
def test_candidate_label(terminal: Terminal, expected_label: str) -> None:
    assert candidate_label(terminal) == expected_label


@pytest.mark.parametrize(
    "candidates, expected_text",
    [
        ([_PAN_CANDIDATE], "Send this to stream-pan (work / 1, folder pan)? Reply yes."),
        (
            [_PAN_CANDIDATE, _DOCS_CANDIDATE],
            "Which session? Reply 1 or 2:\n"
            "1. stream-pan (work / 1, folder pan)\n"
            "2. t4 (work / 2, folder docs)",
        ),
    ],
    ids=["one", "two"],
)
def test_question_text(candidates: list[ConfirmationCandidate], expected_text: str) -> None:
    assert question_text(candidates) == expected_text


@pytest.mark.parametrize(
    "addresses, expected_candidates",
    [
        (["t3"], [_PAN_CANDIDATE]),
        (["t9", "t3", "t3", "t4"], [_PAN_CANDIDATE, _DOCS_CANDIDATE]),
    ],
    ids=["one", "unknown-and-repeated-dropped"],
)
def test_propose_stores_the_question_and_asks_types_nothing(
    tmp_path: Path, addresses: list[str], expected_candidates: list[ConfirmationCandidate]
) -> None:
    terminal_host = _host(_pan_session(), _docs_session())
    slack = RecordingSlack()

    _propose(tmp_path, terminal_host, slack, *addresses)

    stored = _store(tmp_path).get(ThreadSource.SLACK, _THREAD)
    assert stored is not None
    assert stored.candidates == expected_candidates
    assert stored.task == "add the tests"
    assert slack.texts() == [question_text(expected_candidates)]
    assert terminal_host.sent == []


def test_propose_names_at_most_three_sessions(tmp_path: Path) -> None:
    terminals = [make_terminal(f"t{number}") for number in range(1, 6)]

    pending = _propose(tmp_path, _host(*terminals), RecordingSlack(), "t1", "t2", "t3", "t4")

    assert pending is not None
    assert [candidate.address for candidate in pending.candidates] == ["t1", "t2", "t3"]


@pytest.mark.parametrize(
    "addresses", [[], ["t9"], ["t5"]], ids=["none", "unknown", "finished-terminal"]
)
def test_propose_with_no_live_candidate_says_no_session_matches(
    tmp_path: Path, addresses: list[str]
) -> None:
    _store(tmp_path).put(_question(_PAN_CANDIDATE))
    terminal_host = _host(_pan_session(), make_terminal("t5", state=TerminalState.FINISHED))
    slack = RecordingSlack()

    assert _propose(tmp_path, terminal_host, slack, *addresses) is None

    assert _store(tmp_path).get(ThreadSource.SLACK, _THREAD) is None
    assert slack.texts() == [
        'no session matches "pan docs"; to start new work send `@pan new: <your text>`'
    ]


@pytest.mark.parametrize(
    "candidates, choice, expected_address",
    [
        ([_PAN_CANDIDATE], "yes", "t3"),
        ([_PAN_CANDIDATE, _DOCS_CANDIDATE], "2", "t4"),
    ],
    ids=["yes-to-one", "number-from-several"],
)
def test_confirm_sends_the_stored_task_and_ties_the_thread(
    tmp_path: Path,
    candidates: list[ConfirmationCandidate],
    choice: str,
    expected_address: str,
) -> None:
    _store(tmp_path).put(_question(*candidates))
    terminal_host = _host(_pan_session(), _docs_session())
    slack = RecordingSlack()

    tied = _confirm(tmp_path, terminal_host, slack, choice)

    assert tied is not None
    assert tied.terminal_address == expected_address
    assert tied.requester == "U0TEST"
    ((sent_address, sent_text),) = terminal_host.sent
    assert sent_address == expected_address
    assert sent_text.startswith("From Slack via pan.")
    assert sent_text.endswith("add the tests")
    assert _store(tmp_path).get(ThreadSource.SLACK, _THREAD) is None
    assert slack.texts()[-1].startswith("Sent to ")


@pytest.mark.parametrize(
    "candidates, choice, expected_reply",
    [
        (
            [_PAN_CANDIDATE, _DOCS_CANDIDATE],
            "yes",
            "Reply 1 or 2 to choose, or anything else to cancel.",
        ),
        ([_PAN_CANDIDATE], "2", "Reply yes to send it, or anything else to cancel."),
    ],
    ids=["yes-to-several", "number-out-of-range"],
)
def test_confirm_with_a_reply_that_picks_nothing_asks_again(
    tmp_path: Path, candidates: list[ConfirmationCandidate], choice: str, expected_reply: str
) -> None:
    _store(tmp_path).put(_question(*candidates))
    terminal_host = _host(_pan_session(), _docs_session())
    slack = RecordingSlack()

    assert _confirm(tmp_path, terminal_host, slack, choice) is None

    assert terminal_host.sent == []
    assert slack.texts() == [expected_reply]
    assert _store(tmp_path).get(ThreadSource.SLACK, _THREAD) is not None


def test_confirm_after_the_question_expired_sends_nothing(tmp_path: Path) -> None:
    _store(tmp_path).put(_question(_PAN_CANDIDATE))
    terminal_host = _host(_pan_session())
    slack = RecordingSlack()

    clock = SettableClock(_ASKED_AT + timedelta(minutes=31))
    assert _confirm(tmp_path, terminal_host, slack, "yes", clock=clock) is None

    assert terminal_host.sent == []
    assert slack.texts() == [EXPIRED_REPLY]
    assert _store(tmp_path).get(ThreadSource.SLACK, _THREAD) is None


@pytest.mark.parametrize(
    "terminals",
    [[], [_pan_session(name="other-session")], [_pan_session(state=TerminalState.FINISHED)]],
    ids=["gone", "address-reused-by-another-session", "finished"],
)
def test_confirm_to_a_session_no_longer_running_sends_nothing(
    tmp_path: Path, terminals: list[Terminal]
) -> None:
    _store(tmp_path).put(_question(_PAN_CANDIDATE))
    terminal_host = _host(*terminals)
    slack = RecordingSlack()

    with pytest.raises(TargetNotFoundError):
        _confirm(tmp_path, terminal_host, slack, "yes")

    assert terminal_host.sent == []
    assert slack.texts() == ["stream-pan (work / 1, folder pan) is no longer running; not sent"]
    assert _store(tmp_path).get(ThreadSource.SLACK, _THREAD) is None


def test_confirm_to_a_busy_session_is_refused_and_the_question_cleared(tmp_path: Path) -> None:
    _store(tmp_path).put(_question(_PAN_CANDIDATE))
    terminal_host = _host(_pan_session(agent_status=AgentStatus.WORKING))
    slack = RecordingSlack()

    with pytest.raises(SessionBusyError):
        _confirm(tmp_path, terminal_host, slack, "yes")

    assert terminal_host.sent == []
    assert slack.texts() == ["stream-pan is busy; try again, or @pan check"]
    assert _store(tmp_path).get(ThreadSource.SLACK, _THREAD) is None


@pytest.mark.parametrize(
    "question, expected_cancelled, expected_texts",
    [(_question(_PAN_CANDIDATE), True, [NOT_SENT_REPLY]), (None, False, [])],
    ids=["open-question", "no-question"],
)
def test_cancel_confirmation(
    tmp_path: Path,
    question: PendingConfirmation | None,
    expected_cancelled: bool,
    expected_texts: list[str],
) -> None:
    if question is not None:
        _store(tmp_path).put(question)
    slack = RecordingSlack()

    cancelled = cancel_confirmation(
        thread_ts=_THREAD, channel="C0TEST", confirmations=_store(tmp_path), slack=slack
    )

    assert cancelled is expected_cancelled
    assert slack.texts() == expected_texts
    assert _store(tmp_path).get(ThreadSource.SLACK, _THREAD) is None
