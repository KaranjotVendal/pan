from __future__ import annotations

from pathlib import Path

import pytest
from inbox_fakes import FakeInboxDirectory, FakeSessionFolders
from terminal_host_fakes import make_terminal

from pan.config import INBOX_ROUTE_MAX_PROJECTS
from pan.errors import (
    InboxThreadAmbiguousError,
    PanError,
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


@pytest.mark.parametrize(
    "selector, terminals, listings, sessions, folders, expected_error",
    [
        (
            "pan with slack",
            [make_terminal("t4", cwd=_OTHER_REPO)],
            {_OTHER_REPO: [_SLACK_THREAD]},
            {"thr_1": "session-1"},
            {"session-1": _REPO},
            ThreadSessionNotRunningError,
        ),
        (
            "pan with slack",
            [make_terminal("t3", cwd=_REPO), make_terminal("t4", cwd=_REPO)],
            {_REPO: [_SLACK_THREAD]},
            {"thr_1": "session-1"},
            {"session-1": _REPO},
            TargetAmbiguousError,
        ),
        (
            "pan",
            [make_terminal("t3", cwd=_REPO)],
            {_REPO: [_SLACK_THREAD, InboxThread(thread_id="thr_9", name="pan docs")]},
            {},
            {},
            InboxThreadAmbiguousError,
        ),
        (
            "pan with slack",
            [make_terminal("t3", cwd=_REPO, agent="zsh")],
            {_REPO: [_SLACK_THREAD]},
            {"thr_1": "session-1"},
            {"session-1": _REPO},
            TargetNotFoundError,
        ),
    ],
    ids=[
        "writer-not-running",
        "several-sessions-in-the-writers-folder",
        "several-threads-match",
        "only-claude-sessions-are-listed-and-reached",
    ],
)
def test_route_by_inbox_thread_refusals(
    selector: str,
    terminals: list[Terminal],
    listings: dict[Path, list[InboxThread]],
    sessions: dict[str, str],
    folders: dict[str, Path],
    expected_error: type[PanError],
) -> None:
    with pytest.raises(expected_error):
        _route(selector, terminals, listings=listings, sessions=sessions, folders=folders)


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

    assert inbox_directory.listed == [
        Path(f"/tmp/repo-{index}").resolve() for index in range(INBOX_ROUTE_MAX_PROJECTS)
    ]


@pytest.mark.parametrize(
    "selector, expected_address",
    [
        ("t7", "t7"),
        ("pan-7", "t7"),
        ("web / one", "t3"),
        ("pan with slack", "t3"),
        ("api title words", "t9"),
    ],
    ids=["address", "name", "herdr-label", "inbox-thread", "words"],
)
def test_route_session_resolution_order(selector: str, expected_address: str) -> None:
    terminals = [
        make_terminal("t7", name="pan-7", cwd=_OTHER_REPO),
        make_terminal("t3", where="web / one", cwd=_REPO),
        make_terminal("t9", name="desk-api", cwd=Path("/tmp/api")).model_copy(
            update={"title": "Title words here"}
        ),
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
    make_terminal("t9", name="desk-api", cwd=Path("/tmp/api")).model_copy(
        update={"title": "Fix the build"}
    ),
    make_terminal("t6", name="finished", state=TerminalState.FINISHED),
]


@pytest.mark.parametrize(
    "selector, expected_address",
    [
        ("desk api", "t9"),
        ("web one", "t3"),
        ("session control", "t4"),
        ("build fix", "t9"),
        ("finished", None),
        ("nothing", None),
    ],
    ids=["name-words", "label-words", "folder-words", "title-words", "live-only", "no-match"],
)
def test_route_by_words(selector: str, expected_address: str | None) -> None:
    found = route_by_words(selector, _WORD_TERMINALS)

    assert (found.address if found is not None else None) == expected_address


def test_route_by_words_refuses_several_matches_with_the_candidates() -> None:
    with pytest.raises(TargetAmbiguousError) as raised:
        route_by_words("web", _WORD_TERMINALS)

    assert [candidate.address for candidate in raised.value.candidates] == ["t3", "t4"]
