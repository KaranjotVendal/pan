from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from inbox_fakes import FakeInboxDirectory, FakeSessionFolders
from terminal_host_fakes import make_terminal

from pan import routing
from pan.config import INBOX_ROUTE_MAX_PROJECTS
from pan.errors import (
    InboxThreadAmbiguousError,
    TargetAmbiguousError,
    TargetNotFoundError,
    ThreadSessionNotRunningError,
)
from pan.models import InboxThread, Terminal, TerminalState
from pan.routing import (
    RoutedSession,
    matching_threads,
    route_by_words,
    route_session,
    words_match,
)

_REPO = Path("/tmp/repo").resolve()
_OTHER_REPO = Path("/tmp/other").resolve()
_SLACK_THREAD = InboxThread(thread_id="thr_1", name="pan with Slack and X")
_DOCS_THREAD = InboxThread(thread_id="thr_2", name="docs: tidy the README")


@pytest.mark.parametrize(
    "selector, text, expected",
    [
        ("pan with slack", "pan with Slack and X", True),
        ("PAN slack", "pan with Slack and X", True),
        ("pan sla", "pan with Slack and X", True),
        ("pan x slack", "pan with Slack and X", True),
        ("pan teams", "pan with Slack and X", False),
        ("ith", "pan with Slack and X", False),
        ("session-control", "t3 web / one session-control", True),
        ("", "anything", False),
        (": -", "anything", False),
    ],
    ids=[
        "every-word",
        "case-and-order-ignored",
        "word-prefix",
        "short-word",
        "a-missing-word",
        "inside-a-word-does-not-count",
        "hyphenated-selector",
        "empty-selector",
        "punctuation-only-selector",
    ],
)
def test_words_match(selector: str, text: str, expected: bool) -> None:
    assert words_match(selector, text) is expected


@pytest.mark.parametrize(
    "selector, expected",
    [
        ("pan with slack and x", [_SLACK_THREAD]),
        ("pan slack", [_SLACK_THREAD]),
        ("tidy", [_DOCS_THREAD]),
        ("nothing", []),
    ],
    ids=["exact-name", "words", "one-word", "no-match"],
)
def test_matching_threads(selector: str, expected: list[InboxThread]) -> None:
    assert matching_threads(selector, [_SLACK_THREAD, _DOCS_THREAD]) == expected


def test_an_exact_thread_name_wins_over_word_matches() -> None:
    longer = InboxThread(thread_id="thr_3", name="pan with Slack and X follow-up")

    assert matching_threads("pan with Slack and X", [longer, _SLACK_THREAD]) == [_SLACK_THREAD]


def _route(
    selector: str,
    terminals: list[Terminal],
    *,
    listings: dict[Path, list[InboxThread]] | None = None,
    sessions: dict[str, str] | None = None,
    folders: dict[str, Path] | None = None,
) -> RoutedSession:
    return route_session(
        selector,
        terminals,
        inbox_directory=FakeInboxDirectory(listings=listings, sessions=sessions),
        session_folders=FakeSessionFolders(folders),
    )


def test_route_by_inbox_thread_reaches_the_session_that_writes_it() -> None:
    writer = make_terminal("t3", cwd=_REPO)
    other = make_terminal("t4", cwd=_OTHER_REPO)

    routed = _route(
        "pan with slack",
        [other, writer],
        listings={_REPO: [_SLACK_THREAD]},
        sessions={"thr_1": "session-1"},
        folders={"session-1": _REPO},
    )

    assert routed == RoutedSession(terminal=writer, inbox_thread=_SLACK_THREAD)


# The thread step's refusals, each with what the words step finds after it.
_THREAD_REFUSALS = {
    "writer-not-running": (
        "pan with slack",
        [make_terminal("t4", cwd=_OTHER_REPO)],
        {_OTHER_REPO: [_SLACK_THREAD]},
        ThreadSessionNotRunningError,
    ),
    "several-sessions-in-the-writers-folder": (
        "pan with slack",
        [make_terminal("t3", cwd=_REPO), make_terminal("t4", cwd=_REPO)],
        {_REPO: [_SLACK_THREAD]},
        TargetAmbiguousError,
    ),
    "several-threads-match": (
        "pan",
        [make_terminal("t3", cwd=_REPO)],
        {_REPO: [_SLACK_THREAD, InboxThread(thread_id="thr_9", name="pan docs")]},
        InboxThreadAmbiguousError,
    ),
}
# Terminals only the words step can find (not Claude, so the thread step never lists them).
_WORD_MATCHES = {
    "no-word-match": [],
    "one-word-match": [
        make_terminal("t20", name="pan-with-slack-a", cwd=Path("/tmp/words-a"), agent="zsh")
    ],
    "several-word-matches": [
        make_terminal("t20", name="pan-with-slack-a", cwd=Path("/tmp/words-a"), agent="zsh"),
        make_terminal("t21", name="pan-with-slack-b", cwd=Path("/tmp/words-b"), agent="zsh"),
    ],
}


@pytest.mark.parametrize("word_case", list(_WORD_MATCHES))
@pytest.mark.parametrize("refusal_case", list(_THREAD_REFUSALS))
def test_a_thread_step_refusal_falls_through_to_the_words(
    refusal_case: str, word_case: str
) -> None:
    selector, thread_terminals, listings, refusal = _THREAD_REFUSALS[refusal_case]
    word_terminals = _WORD_MATCHES[word_case]
    terminals = [*thread_terminals, *word_terminals]

    def route() -> RoutedSession:
        return _route(
            selector,
            terminals,
            listings=listings,
            sessions={"thr_1": "session-1"},
            folders={"session-1": _REPO},
        )

    if word_case == "one-word-match":
        # One word match wins over the thread step's refusal.
        assert route() == RoutedSession(terminal=word_terminals[0])
    elif word_case == "several-word-matches":
        # The words' pick list wins over the thread step's refusal.
        with pytest.raises(TargetAmbiguousError) as raised:
            route()
        assert [candidate.address for candidate in raised.value.candidates] == ["t20", "t21"]
    else:
        # With no word match, the thread step's refusal is reported.
        with pytest.raises(refusal):
            route()


def test_only_claude_sessions_are_reached_by_inbox_thread() -> None:
    with pytest.raises(TargetNotFoundError):
        _route(
            "pan with slack",
            [make_terminal("t3", cwd=_REPO, agent="zsh")],
            listings={_REPO: [_SLACK_THREAD]},
            sessions={"thr_1": "session-1"},
            folders={"session-1": _REPO},
        )


@pytest.mark.parametrize(
    "sessions, folders",
    [({}, {}), ({"thr_1": "session-1"}, {})],
    ids=["no-agent-session-in-the-thread", "no-transcript-folder"],
)
def test_route_by_inbox_thread_falls_through_to_words_when_a_step_cannot_be_read(
    sessions: dict[str, str], folders: dict[str, Path]
) -> None:
    by_words = make_terminal("t5", name="pan-slack", cwd=_OTHER_REPO)

    routed = _route(
        "pan slack",
        [make_terminal("t3", cwd=_REPO), by_words],
        listings={_REPO: [_SLACK_THREAD]},
        sessions=sessions,
        folders=folders,
    )

    assert routed == RoutedSession(terminal=by_words)


def test_inbox_routing_lists_each_folder_once_and_caps_the_folders() -> None:
    terminals = [
        make_terminal(f"t{index}", cwd=Path(f"/tmp/repo-{index}"))
        for index in range(INBOX_ROUTE_MAX_PROJECTS + 2)
    ]
    terminals.append(make_terminal("t99", cwd=Path("/tmp/repo-0")))
    inbox_directory = FakeInboxDirectory()

    with pytest.raises(TargetNotFoundError):
        route_session(
            "nothing like it",
            terminals,
            inbox_directory=inbox_directory,
            session_folders=FakeSessionFolders(),
        )

    assert sorted(inbox_directory.listed) == sorted(
        Path(f"/tmp/repo-{index}").resolve() for index in range(INBOX_ROUTE_MAX_PROJECTS)
    )


@pytest.mark.parametrize(
    "selector, expected_address",
    [
        ("t7", "t7"),
        ("pan-7", "t7"),
        ("web / one", "t3"),
        ("pan with slack", "t3"),
        ("desk api", "t9"),
    ],
    ids=["address", "name", "herdr-label", "inbox-thread", "words"],
)
def test_route_session_resolution_order(selector: str, expected_address: str) -> None:
    terminals = [
        make_terminal("t7", name="pan-7", cwd=_OTHER_REPO),
        make_terminal("t3", where="web / one", cwd=_REPO),
        make_terminal("t9", name="desk-api", cwd=Path("/tmp/api")),
    ]

    routed = _route(
        selector,
        terminals,
        listings={_REPO: [_SLACK_THREAD]},
        sessions={"thr_1": "session-1"},
        folders={"session-1": _REPO},
    )

    assert routed.terminal.address == expected_address


def test_an_exact_name_wins_before_the_inbox_is_read() -> None:
    inbox_directory = FakeInboxDirectory(listings={_REPO: [_SLACK_THREAD]})

    routed = route_session(
        "pan-7",
        [make_terminal("t7", name="pan-7", cwd=_REPO)],
        inbox_directory=inbox_directory,
        session_folders=FakeSessionFolders(),
    )

    assert routed.terminal.address == "t7"
    assert inbox_directory.listed == []


_WORD_TERMINALS = [
    make_terminal("t3", where="web / one", cwd=Path("/tmp/web")),
    make_terminal("t4", where="web / two", cwd=Path("/tmp/session-control")),
    make_terminal("t9", name="desk-api", cwd=Path("/tmp/api")),
    make_terminal("t6", name="finished", state=TerminalState.FINISHED),
]


@pytest.mark.parametrize(
    "selector, expected_address",
    [
        ("desk api", "t9"),
        ("web one", "t3"),
        ("session control", "t4"),
        ("finished", None),
        ("nothing", None),
    ],
    ids=["name-words", "label-words", "folder-words", "live-only", "no-match"],
)
def test_route_by_words(selector: str, expected_address: str | None) -> None:
    found = route_by_words(selector, _WORD_TERMINALS)

    assert (found.address if found is not None else None) == expected_address


def test_route_by_words_refuses_several_matches_with_the_candidates() -> None:
    with pytest.raises(TargetAmbiguousError) as raised:
        route_by_words("web", _WORD_TERMINALS)

    assert [candidate.address for candidate in raised.value.candidates] == ["t3", "t4"]


class TimedInboxDirectory:
    # Lists one thread per folder after that folder's behaviour: answer at once, answer after a
    # short delay, hang until released, or fail.
    def __init__(self, behaviours: dict[Path, str], release: threading.Event) -> None:
        self._behaviours = behaviours
        self._release = release

    def find_thread(self, project_path: Path, thread_name: str) -> InboxThread | None:
        return None

    def list_threads(self, project_path: Path) -> list[InboxThread]:
        behaviour = self._behaviours[project_path]
        if behaviour == "slow":
            time.sleep(0.05)
        elif behaviour == "hanging":
            self._release.wait(timeout=5)
        elif behaviour == "failing":
            raise RuntimeError("listing failed")
        return [InboxThread(thread_id=f"thr_{project_path.name}", name=f"{project_path.name} work")]

    def latest_agent_session(self, thread_id: str) -> str | None:
        return f"session-{thread_id.removeprefix('thr_')}"


@pytest.mark.parametrize(
    "writer_behaviour, other_behaviour, expect_routed",
    [
        ("quick", "hanging", True),
        ("slow", "quick", True),
        ("hanging", "quick", False),
        ("failing", "quick", False),
    ],
    ids=[
        "a-hanging-project-does-not-block-the-others",
        "a-slow-project-within-the-budget-counts",
        "a-hanging-project-is-skipped",
        "a-failing-project-is-skipped",
    ],
)
def test_inbox_listings_run_at_once_within_one_time_budget(
    monkeypatch: pytest.MonkeyPatch,
    writer_behaviour: str,
    other_behaviour: str,
    expect_routed: bool,
) -> None:
    monkeypatch.setattr(routing, "INBOX_ROUTE_LISTING_BUDGET_SECONDS", 0.3)
    writer_folder = Path("/tmp/alpha").resolve()
    other_folder = Path("/tmp/beta").resolve()
    writer = make_terminal("t3", cwd=writer_folder)
    release = threading.Event()
    inbox_directory = TimedInboxDirectory(
        {writer_folder: writer_behaviour, other_folder: other_behaviour}, release
    )
    started = time.monotonic()
    try:
        if expect_routed:
            routed = route_session(
                "alpha work",
                [writer, make_terminal("t4", cwd=other_folder)],
                inbox_directory=inbox_directory,
                session_folders=FakeSessionFolders({"session-alpha": writer_folder}),
            )
            assert routed.terminal == writer
        else:
            with pytest.raises(TargetNotFoundError):
                route_session(
                    "alpha work",
                    [writer, make_terminal("t4", cwd=other_folder)],
                    inbox_directory=inbox_directory,
                    session_folders=FakeSessionFolders({"session-alpha": writer_folder}),
                )
        assert time.monotonic() - started < 2
    finally:
        release.set()
