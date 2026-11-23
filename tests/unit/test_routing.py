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
    project_folders,
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


def _session(address: str, folder: str, status_ms: int | None, created_ms: int | None) -> Terminal:
    return make_terminal(address, cwd=Path(folder)).model_copy(
        update={"status_changed_ms": status_ms, "created_ms": created_ms}
    )


@pytest.mark.parametrize(
    "sessions, expected_folder_names",
    [
        (
            [
                _session("t1", "/tmp/old", 100, 10),
                _session("t2", "/tmp/new", 300, 10),
                _session("t3", "/tmp/mid", 200, 10),
            ],
            ["new", "mid", "old"],
        ),
        (
            [_session("t1", "/tmp/status", 150, 10), _session("t2", "/tmp/created", None, 200)],
            ["created", "status"],
        ),
        (
            [
                _session("t1", "/tmp/unknown-a", None, None),
                _session("t2", "/tmp/known", 5, None),
                _session("t3", "/tmp/unknown-b", None, None),
            ],
            ["known", "unknown-a", "unknown-b"],
        ),
        (
            [
                _session("t1", "/tmp/shared", 100, 10),
                _session("t2", "/tmp/other", 200, 10),
                _session("t3", "/tmp/shared", 300, 10),
            ],
            ["shared", "other"],
        ),
    ],
    ids=[
        "newest-status-change-first",
        "created-time-when-no-status-change",
        "no-time-goes-last-in-listing-order",
        "a-folder-is-as-active-as-its-most-active-session",
    ],
)
def test_project_folders_most_recently_active_first(
    sessions: list[Terminal], expected_folder_names: list[str]
) -> None:
    assert [folder.name for folder in project_folders(sessions)] == expected_folder_names


def test_project_folders_respects_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routing, "INBOX_ROUTE_MAX_PROJECTS", 3)
    sessions = [_session(f"t{index}", f"/tmp/repo-{index}", index, None) for index in range(5)]

    assert [folder.name for folder in project_folders(sessions)] == [
        "repo-4",
        "repo-3",
        "repo-2",
    ]


class ConcurrencyInboxDirectory:
    # Records the order listings start in and the most that ran at once; each takes a while.
    def __init__(self, seconds_per_listing: float) -> None:
        self._seconds_per_listing = seconds_per_listing
        self._lock = threading.Lock()
        self._running = 0
        self.peak = 0
        self.started: list[Path] = []

    def find_thread(self, project_path: Path, thread_name: str) -> InboxThread | None:
        return None

    def list_threads(self, project_path: Path) -> list[InboxThread]:
        with self._lock:
            self._running += 1
            self.peak = max(self.peak, self._running)
            self.started.append(project_path)
        time.sleep(self._seconds_per_listing)
        with self._lock:
            self._running -= 1
        return []

    def latest_agent_session(self, thread_id: str) -> str | None:
        return None


@pytest.mark.parametrize("workers", [1, 2, 3], ids=["one-worker", "two-workers", "three-workers"])
def test_listings_run_with_a_worker_count_separate_from_the_cap(
    monkeypatch: pytest.MonkeyPatch, workers: int
) -> None:
    monkeypatch.setattr(routing, "INBOX_ROUTE_WORKERS", workers)
    monkeypatch.setattr(routing, "INBOX_ROUTE_MAX_PROJECTS", 6)
    inbox_directory = ConcurrencyInboxDirectory(seconds_per_listing=0.05)
    sessions = [_session(f"t{index}", f"/tmp/repo-{index}", index, None) for index in range(8)]

    with pytest.raises(TargetNotFoundError):
        route_session(
            "nothing like it",
            sessions,
            inbox_directory=inbox_directory,
            session_folders=FakeSessionFolders(),
        )

    assert inbox_directory.peak == workers
    assert len(inbox_directory.started) == 6


def test_a_budget_that_runs_out_skips_the_stalest_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routing, "INBOX_ROUTE_WORKERS", 1)
    monkeypatch.setattr(routing, "INBOX_ROUTE_LISTING_BUDGET_SECONDS", 0.25)
    inbox_directory = ConcurrencyInboxDirectory(seconds_per_listing=0.1)
    sessions = [_session(f"t{index}", f"/tmp/repo-{index}", index, None) for index in range(6)]

    with pytest.raises(TargetNotFoundError):
        route_session(
            "nothing like it",
            sessions,
            inbox_directory=inbox_directory,
            session_folders=FakeSessionFolders(),
        )
    time.sleep(0.15)

    started = [folder.name for folder in inbox_directory.started]
    assert started == ["repo-5", "repo-4", "repo-3"][: len(started)]
    assert 1 <= len(started) <= 3
    assert "repo-0" not in started


def test_activity_times_are_fetched_for_sessions_the_listing_gave_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routing, "INBOX_ROUTE_WORKERS", 1)
    listed = [_session(f"t{index}", f"/tmp/repo-{index}", None, None) for index in range(3)]
    details = {
        "t1": _session("t1", "/tmp/repo-1", 300, 1),
        "t2": _session("t2", "/tmp/repo-2", 100, 1),
    }

    def session_details(address: str) -> Terminal:
        if address not in details:
            raise TargetNotFoundError(address)
        return details[address]

    inbox_directory = ConcurrencyInboxDirectory(seconds_per_listing=0)
    with pytest.raises(TargetNotFoundError):
        route_session(
            "nothing like it",
            listed,
            inbox_directory=inbox_directory,
            session_folders=FakeSessionFolders(),
            session_details=session_details,
        )

    # t0's details failed, so its folder has no time and goes last.
    assert [folder.name for folder in inbox_directory.started] == ["repo-1", "repo-2", "repo-0"]
