from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from inbox_fakes import FakeInboxDirectory
from slack_fakes import RecordingSlack
from terminal_host_fakes import FakeTerminalHost, make_terminal

from pan.backend.confirmations import (
    EXPIRED_REPLY,
    NOT_SENT_REPLY,
    FilePendingConfirmations,
    cancel_confirmation,
    candidate_label,
    confirm_session,
    is_answer,
    live_pending,
    normalize_choice,
    propose_sessions,
    question_text,
)
from pan.core.errors import (
    SessionBusyError,
    TargetBlockedError,
    TargetNotFoundError,
    UnansweredQuestionError,
)
from pan.core.models import (
    AgentStatus,
    ConfirmationCandidate,
    Conversation,
    PendingConfirmation,
    Terminal,
    TerminalState,
    ThreadRecord,
    ThreadSource,
)
from pan.data.threadmap import FileThreadMap

_ASKED_AT = datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)
_THREAD = "1700000000.000300"
_CONVERSATION = Conversation.slack(_THREAD, "C0TEST")
_PAN_FOLDER = Path("/tmp/work/pan")
_DOCS_FOLDER = Path("/tmp/work/docs")


class SettableClock:
    def __init__(self, now: datetime = _ASKED_AT) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


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
    *candidates: ConfirmationCandidate,
    asked_at: datetime = _ASKED_AT,
    thread_ts: str = _THREAD,
    answer: str | None = "yes",
    asked_again_at: datetime | None = None,
) -> PendingConfirmation:
    # Answered with `answer` a minute after it was asked; unanswered when `answer` is None.
    # `asked_again_at` is when pan last asked, when it asked again.
    return PendingConfirmation(
        conversation=Conversation.slack(thread_ts, "C0TEST"),
        selector="pan docs",
        task="add the tests",
        requester="U0TEST",
        candidates=list(candidates),
        created_at=asked_at,
        asked_at=asked_again_at if asked_again_at is not None else asked_at,
        answered_at=asked_at + timedelta(minutes=1) if answer is not None else None,
        answer_text=answer,
    )


_PAN_CANDIDATE = ConfirmationCandidate(
    address="t3",
    name="stream-pan",
    cwd=_PAN_FOLDER,
    where="work / 1",
    label="stream-pan (work / 1, folder pan)",
)
_DOCS_CANDIDATE = ConfirmationCandidate(
    address="t4", cwd=_DOCS_FOLDER, where="work / 2", label="t4 (work / 2, folder docs)"
)


def _propose(
    tmp_path: Path, terminal_host: FakeTerminalHost, slack: RecordingSlack, *addresses: str
) -> PendingConfirmation | None:
    return propose_sessions(
        conversation=_CONVERSATION,
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
        conversation=_CONVERSATION,
        choice=choice,
        terminal_host=terminal_host,
        thread_map=FileThreadMap(tmp_path / "threads.json", SettableClock()),
        inbox_directory=FakeInboxDirectory(),
        confirmations=_store(tmp_path),
        slack=slack,
        clock=clock if clock is not None else SettableClock(),
    )


@pytest.mark.parametrize(
    "contents",
    [b"{not json", b"[1, 2]", b"\xff\xfe", b'{"slack:1": {"thread_ts": 5}}'],
    ids=["not-json", "not-an-object", "not-text", "malformed-entry"],
)
def test_store_reads_a_damaged_file_as_no_questions(tmp_path: Path, contents: bytes) -> None:
    (tmp_path / "confirmations.json").write_bytes(contents)
    store = _store(tmp_path)

    assert store.all_pending() == []
    assert store.get(_CONVERSATION) is None
    store.put(_question(_PAN_CANDIDATE))
    assert store.all_pending() == [_question(_PAN_CANDIDATE)]


def test_store_reads_a_folder_in_its_place_as_no_questions(tmp_path: Path) -> None:
    (tmp_path / "confirmations.json").mkdir()

    assert _store(tmp_path).all_pending() == []


def test_store_keeps_one_question_per_thread_and_drops_expired_ones(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put(_question(_DOCS_CANDIDATE, thread_ts="old", asked_at=_ASKED_AT - timedelta(hours=1)))
    store.put(_question(_DOCS_CANDIDATE))
    store.put(_question(_PAN_CANDIDATE))

    assert store.get(Conversation.slack("old")) is None
    assert store.get(_CONVERSATION) == _question(_PAN_CANDIDATE)
    store.clear(_CONVERSATION)
    assert store.get(_CONVERSATION) is None


def test_store_keeps_the_latest_answer_and_clears_it(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put(_question(_PAN_CANDIDATE, answer=None))
    first_reply = _ASKED_AT + timedelta(minutes=2)
    second_reply = first_reply + timedelta(minutes=1)

    store.mark_answered(_CONVERSATION, first_reply, "yes")
    store.mark_answered(_CONVERSATION, second_reply, "no wait")
    store.mark_answered(Conversation.slack("1700000000.000999"), first_reply, "yes")

    stored = store.get(_CONVERSATION)
    assert stored is not None
    assert (stored.answered_at, stored.answer_text) == (second_reply, "no wait")
    assert store.get(Conversation.slack("1700000000.000999")) is None
    asked_again = second_reply + timedelta(minutes=1)
    store.clear_answer(_CONVERSATION, asked_again)
    assert store.get(_CONVERSATION) == _question(
        _PAN_CANDIDATE, answer=None, asked_again_at=asked_again
    )


def test_store_keeps_a_slack_question_and_an_x_question_with_the_same_id_apart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    x_post = Conversation(source=ThreadSource.X, id=_THREAD)
    slack_question = _question(_PAN_CANDIDATE, answer=None)
    store.put(slack_question)
    store.put(_question(_DOCS_CANDIDATE, answer=None).model_copy(update={"conversation": x_post}))

    store.mark_answered(x_post, _ASKED_AT + timedelta(minutes=2), "yes")
    store.clear(x_post)

    assert store.get(_CONVERSATION) == slack_question
    assert store.get(x_post) is None


def test_store_loads_a_flat_question_from_before_conversation_and_rewrites_it(
    tmp_path: Path,
) -> None:
    question = _question(_PAN_CANDIDATE, answer=None)
    flat_question = question.model_dump(mode="json")
    del flat_question["conversation"]
    flat_question.update({"thread_ts": _THREAD, "channel": "C0TEST", "source": "slack"})
    (tmp_path / "confirmations.json").write_text(json.dumps({f"slack:{_THREAD}": flat_question}))
    store = _store(tmp_path)

    loaded = store.get(_CONVERSATION)
    store.mark_answered(_CONVERSATION, _ASKED_AT + timedelta(minutes=2), "yes")

    assert loaded == question
    stored = json.loads((tmp_path / "confirmations.json").read_text())
    assert list(stored) == [f"slack:{_THREAD}"]
    assert stored[f"slack:{_THREAD}"]["conversation"] == _CONVERSATION.model_dump(mode="json")
    assert not {"thread_ts", "channel", "source"} & set(stored[f"slack:{_THREAD}"])
    reloaded = store.get(_CONVERSATION)
    assert reloaded is not None
    assert reloaded.answer_text == "yes"


@pytest.mark.parametrize(
    "author, minutes_after_asking, requester, expected",
    [
        ("U0TEST", 2, "U0TEST", True),
        ("U000OTHER", 2, "U0TEST", False),
        ("U0TEST", -1, "U0TEST", False),
        ("U0TEST", 0, "U0TEST", False),
        ("U0TEST", 31, "U0TEST", False),
        ("U000OTHER", 2, None, False),
        ("U0TEST", 2, None, False),
    ],
    ids=[
        "requester-after",
        "another-user",
        "before-asking",
        "same-moment",
        "expired",
        "stored-without-requester-another-user",
        "stored-without-requester-never",
    ],
)
def test_is_answer(
    author: str, minutes_after_asking: int, requester: str | None, expected: bool
) -> None:
    question = _question(_PAN_CANDIDATE, answer=None).model_copy(update={"requester": requester})
    received_at = _ASKED_AT + timedelta(minutes=minutes_after_asking)

    assert is_answer(question, author, received_at) is expected


@pytest.mark.parametrize(
    "choice, expected",
    [
        ("yes", "yes"),
        (" Y ", "yes"),
        ("YES", "yes"),
        ("1", "1"),
        ("3", "3"),
        ("4", None),
        ("0", None),
        ("no", None),
        ("sure", None),
    ],
)
def test_normalize_choice(choice: str, expected: str | None) -> None:
    assert normalize_choice(choice) == expected


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

    stored = _store(tmp_path).get(_CONVERSATION)
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

    assert _store(tmp_path).get(_CONVERSATION) is None
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
    _store(tmp_path).put(_question(*candidates, answer=choice))
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
    assert _store(tmp_path).get(_CONVERSATION) is None
    assert slack.status_texts() == ["Working: add the tests"]
    assert [ephemeral[2] for ephemeral in slack.ephemerals] == ["U0TEST"]


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
    _store(tmp_path).put(_question(*candidates, answer=choice))
    terminal_host = _host(_pan_session(), _docs_session())
    slack = RecordingSlack()

    assert _confirm(tmp_path, terminal_host, slack, choice) is None

    assert terminal_host.sent == []
    assert slack.texts() == [expected_reply]
    # Asked again, so the reply that picked nothing no longer answers it.
    assert _store(tmp_path).get(_CONVERSATION) == _question(
        *candidates, answer=None, asked_again_at=_ASKED_AT
    )


@pytest.mark.parametrize(
    "answer, choice",
    [
        (None, "yes"),
        ("no", "yes"),
        ("N", "yes"),
        ("nope, wrong one", "yes"),
        ("<@U000BOT> cancel", "yes"),
        ("stop", "yes"),
        ("none of those", "yes"),
        ("neither", "2"),
        ("not those, the api one", "yes"),
        ("don't send it", "yes"),
        ("yes please", "1"),
        ("the second one", "2"),
        ("2nd", "2"),
        ("12", "2"),
        ("not 2, 3", "2"),
        ("not 2, 3", "3"),
        ("2", "yes"),
        ("the docs one", "yes"),
        ("yes, but not that one", "yes"),
    ],
    ids=[
        "unanswered",
        "no",
        "n",
        "nope",
        "cancel-after-mention",
        "stop",
        "none-of-those",
        "neither",
        "not-those",
        "dont-send",
        "number-not-typed",
        "number-in-words",
        "ordinal",
        "other-number",
        "negated-number",
        "number-after-a-negation",
        "number-for-yes",
        "words-for-yes",
        "yes-then-negation",
    ],
)
def test_confirm_refuses_without_an_answer_that_picks_the_choice(
    tmp_path: Path, answer: str | None, choice: str
) -> None:
    _store(tmp_path).put(_question(_PAN_CANDIDATE, _DOCS_CANDIDATE, answer=answer))
    terminal_host = _host(_pan_session(), _docs_session())
    slack = RecordingSlack()

    with pytest.raises(UnansweredQuestionError):
        _confirm(tmp_path, terminal_host, slack, choice)

    assert terminal_host.sent == []
    assert slack.texts() == []
    assert _store(tmp_path).get(_CONVERSATION) is not None


def test_confirm_with_no_question_posts_nothing(tmp_path: Path) -> None:
    terminal_host = _host(_pan_session())
    slack = RecordingSlack()

    assert _confirm(tmp_path, terminal_host, slack, "yes") is None

    assert terminal_host.sent == []
    assert slack.texts() == []


def test_confirm_after_the_question_expired_sends_nothing(tmp_path: Path) -> None:
    _store(tmp_path).put(_question(_PAN_CANDIDATE))
    terminal_host = _host(_pan_session())
    slack = RecordingSlack()

    clock = SettableClock(_ASKED_AT + timedelta(minutes=31))
    assert _confirm(tmp_path, terminal_host, slack, "yes", clock=clock) is None

    assert terminal_host.sent == []
    assert slack.texts() == [EXPIRED_REPLY]
    assert _store(tmp_path).get(_CONVERSATION) is None


@pytest.mark.parametrize(
    "candidate, terminals",
    [
        (_PAN_CANDIDATE, []),
        (_PAN_CANDIDATE, [_pan_session(name="other-session")]),
        (_PAN_CANDIDATE, [_pan_session(state=TerminalState.FINISHED)]),
        (_DOCS_CANDIDATE, [make_terminal("t4", where="work / 2", cwd=Path("/tmp/elsewhere"))]),
        (_DOCS_CANDIDATE, [make_terminal("t4", where="work / 9", cwd=_DOCS_FOLDER)]),
    ],
    ids=[
        "gone",
        "address-reused-by-another-session",
        "finished",
        "unnamed-in-another-folder",
        "unnamed-with-another-label",
    ],
)
def test_confirm_to_a_session_no_longer_running_sends_nothing(
    tmp_path: Path, candidate: ConfirmationCandidate, terminals: list[Terminal]
) -> None:
    _store(tmp_path).put(_question(candidate))
    terminal_host = _host(*terminals)
    slack = RecordingSlack()

    with pytest.raises(TargetNotFoundError):
        _confirm(tmp_path, terminal_host, slack, "yes")

    assert terminal_host.sent == []
    assert slack.texts() == [f"{candidate.label} is no longer running; not sent"]
    assert _store(tmp_path).get(_CONVERSATION) is None


@pytest.mark.parametrize(
    "candidates, choice, session, expected_error, expected_reply",
    [
        (
            [_PAN_CANDIDATE],
            "yes",
            _pan_session(agent_status=AgentStatus.WORKING),
            SessionBusyError,
            "stream-pan is busy; reply yes again later, or anything else to cancel.",
        ),
        (
            [_DOCS_CANDIDATE, _PAN_CANDIDATE],
            "2",
            _pan_session(agent_status=AgentStatus.BLOCKED),
            TargetBlockedError,
            "stream-pan is waiting on a prompt at the terminal; nothing was sent; "
            "reply 2 again later, or anything else to cancel.",
        ),
    ],
    ids=["busy", "waiting-on-a-prompt"],
)
def test_confirm_to_a_session_not_ready_keeps_the_question_open(
    tmp_path: Path,
    candidates: list[ConfirmationCandidate],
    choice: str,
    session: Terminal,
    expected_error: type[Exception],
    expected_reply: str,
) -> None:
    _store(tmp_path).put(_question(*candidates, answer=choice))
    terminal_host = _host(session, _docs_session())
    slack = RecordingSlack()

    with pytest.raises(expected_error):
        _confirm(tmp_path, terminal_host, slack, choice)

    assert terminal_host.sent == []
    assert slack.texts() == [expected_reply]
    # Still open, but the person must answer again.
    assert _store(tmp_path).get(_CONVERSATION) == _question(
        *candidates, answer=None, asked_again_at=_ASKED_AT
    )


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
        conversation=_CONVERSATION, confirmations=_store(tmp_path), slack=slack
    )

    assert cancelled is expected_cancelled
    assert slack.texts() == expected_texts
    assert _store(tmp_path).get(_CONVERSATION) is None


@pytest.mark.parametrize(
    "minutes_after_first_ask, expected",
    [(4, False), (6, True)],
    ids=["before-asked-again", "after-asked-again"],
)
def test_is_answer_counts_only_replies_after_pan_last_asked(
    minutes_after_first_ask: int, expected: bool
) -> None:
    question = _question(
        _PAN_CANDIDATE, answer=None, asked_again_at=_ASKED_AT + timedelta(minutes=5)
    )
    received_at = _ASKED_AT + timedelta(minutes=minutes_after_first_ask)

    assert is_answer(question, "U0TEST", received_at) is expected


def test_propose_records_when_it_asked(tmp_path: Path) -> None:
    slack = RecordingSlack()

    pending = _propose(tmp_path, _host(_pan_session()), slack, "t3")

    assert pending is not None
    assert pending.asked_at == _ASKED_AT


@pytest.mark.parametrize(
    "minutes_after_first_ask, asked_again_minutes, expected_live",
    [(29, None, True), (31, None, False), (31, 20, True), (51, 20, False)],
    ids=["within-first-window", "first-window-over", "re-ask-extends", "extended-window-over"],
)
def test_question_expiry_counts_from_when_pan_last_asked(
    tmp_path: Path,
    minutes_after_first_ask: int,
    asked_again_minutes: int | None,
    expected_live: bool,
) -> None:
    asked_again_at = (
        _ASKED_AT + timedelta(minutes=asked_again_minutes)
        if asked_again_minutes is not None
        else None
    )
    question = _question(_PAN_CANDIDATE, answer=None, asked_again_at=asked_again_at)
    now = _ASKED_AT + timedelta(minutes=minutes_after_first_ask)

    assert (live_pending(question, now) is not None) is expected_live

    # put() keeps a question that is still live when another is stored.
    store = _store(tmp_path)
    store.put(question)
    store.put(_question(_DOCS_CANDIDATE, thread_ts="1700000000.000999", asked_at=now))
    assert (store.get(_CONVERSATION) is not None) is expected_live
