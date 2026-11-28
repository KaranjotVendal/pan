from __future__ import annotations

from pathlib import Path

import pytest
from terminal_host_fakes import make_terminal

from pan.backend.project_map import ProjectPlace, is_agent_session, map_projects
from pan.core.models import AgentStatus, SessionRole, Terminal, TerminalState

_ORCHESTRATOR = make_terminal("t7", where="billing / 1", cwd=Path("/work/billing"))
_STREAM_SAME_SPACE = make_terminal(
    "t48", name="invoice-export", where="billing / 7", cwd=Path("/work/billing-wt"), parent="t7"
)
# A stream the person moved to its own space: the parent link, not the space, places it.
_STREAM_OTHER_SPACE = make_terminal(
    "t20", name="refund-bug", where="refund-bug / diagnosis", parent="t7"
)
_STANDALONE = make_terminal("t30", name="docs-site", where="docs / 1")


def _places(*terminals: Terminal) -> dict[str, ProjectPlace]:
    return map_projects(list(terminals))


def _summary_of(places: dict[str, ProjectPlace]) -> dict[str, tuple[str | None, str, str, str]]:
    return {
        address: (place.parent, place.role.value, place.project, place.project_root)
        for address, place in places.items()
    }


def test_an_orchestrator_and_its_streams_share_its_project() -> None:
    places = _places(_ORCHESTRATOR, _STREAM_SAME_SPACE, _STREAM_OTHER_SPACE, _STANDALONE)

    assert _summary_of(places) == {
        "t7": (None, "orchestrator", "billing", "t7"),
        "t48": ("t7", "stream", "billing", "t7"),
        "t20": ("t7", "stream", "billing", "t7"),
        "t30": (None, "standalone", "docs", "t30"),
    }


@pytest.mark.parametrize(
    "parent",
    [
        make_terminal("t7", state=TerminalState.FINISHED),
        make_terminal("t7", agent="zsh", agent_status=AgentStatus.NONE),
        make_terminal("t7", agent="node", agent_status=AgentStatus.NONE),
        None,
    ],
    ids=["finished-parent", "shell-parent", "dev-server-parent", "gone-parent"],
)
def test_a_stream_without_a_live_parent_stands_alone(parent: Terminal | None) -> None:
    orphan = make_terminal("t48", name="invoice-export", parent="t7")
    terminals = [orphan] if parent is None else [parent, orphan]

    place = map_projects(terminals)["t48"]

    assert place == ProjectPlace(
        parent=None, role=SessionRole.STANDALONE, project="invoice-export", project_root="t48"
    )


@pytest.mark.parametrize(
    "agent, agent_status, expected",
    [
        ("claude-code", AgentStatus.IDLE, True),
        ("claude", AgentStatus.NONE, True),
        ("pi", AgentStatus.NONE, True),
        ("2.1.325", AgentStatus.WORKING, True),
        ("zsh", AgentStatus.NONE, False),
        ("-zsh", AgentStatus.NONE, False),
        ("/bin/zsh", AgentStatus.NONE, False),
        ("nu", AgentStatus.NONE, False),
        ("node", AgentStatus.NONE, False),
        ("tail", AgentStatus.NONE, False),
        (None, AgentStatus.NONE, False),
    ],
)
def test_is_agent_session(agent: str | None, agent_status: AgentStatus, *, expected: bool) -> None:
    assert is_agent_session(agent, agent_status) is expected


@pytest.mark.parametrize(
    "child",
    [
        make_terminal("t9", agent="node", agent_status=AgentStatus.NONE, parent="t7"),
        make_terminal("t9", agent="zsh", agent_status=AgentStatus.NONE, parent="t7"),
        make_terminal("t9", agent=None, agent_status=AgentStatus.NONE, parent="t7"),
    ],
    ids=["dev-server", "shell", "no-program"],
)
def test_a_non_agent_child_is_never_a_stream_and_never_makes_an_orchestrator(
    child: Terminal,
) -> None:
    places = _places(make_terminal("t7", where="billing / 1"), child)

    assert places["t7"].role is SessionRole.STANDALONE
    assert places["t9"].role is SessionRole.STANDALONE
    assert places["t9"].parent is None


def test_a_claude_session_that_has_not_reported_yet_is_a_stream() -> None:
    fresh = make_terminal("t9", agent="claude", agent_status=AgentStatus.NONE, parent="t7")

    places = _places(make_terminal("t7", where="billing / 1"), fresh)

    assert places["t7"].role is SessionRole.ORCHESTRATOR
    assert places["t9"].role is SessionRole.STREAM


def test_a_remote_child_of_a_local_parent_joins_its_project() -> None:
    local_stream = make_terminal("t137", name="build-copy", parent="t1k")
    remote = make_terminal("box/t13", name="build", where="build / 1", parent="t137")

    places = _places(make_terminal("t1k", where="platform / 2"), local_stream, remote)

    assert _summary_of(places) == {
        "t1k": (None, "orchestrator", "platform", "t1k"),
        "t137": ("t1k", "stream", "platform", "t1k"),
        "box/t13": ("t137", "stream", "platform", "t1k"),
    }


def test_a_remote_child_prefers_a_parent_on_its_own_machine() -> None:
    places = _places(
        make_terminal("t5", where="local / 1"),
        make_terminal("box/t5", where="remote / 1"),
        make_terminal("box/t13", name="build", parent="t5"),
    )

    assert places["box/t13"].parent == "box/t5"
    assert places["box/t5"].role is SessionRole.ORCHESTRATOR
    assert places["t5"].role is SessionRole.STANDALONE


@pytest.mark.parametrize(
    "terminals",
    [
        [make_terminal("t2", parent="t3"), make_terminal("t3", parent="t2")],
        [
            make_terminal("t4", parent="t2"),
            make_terminal("t2", parent="t3"),
            make_terminal("t3", parent="t2"),
        ],
        [make_terminal("t2", parent="t2")],
        [make_terminal("ta", parent="t2"), make_terminal("t2", parent="ta")],
    ],
    ids=["two-cycle", "chain-into-a-cycle", "own-parent", "base-36-number"],
)
def test_a_parent_cycle_is_cut_at_its_lowest_address(terminals: list[Terminal]) -> None:
    places = map_projects(terminals)

    assert places["t2"].parent is None
    assert {place.project_root for place in places.values()} == {"t2"}


@pytest.mark.parametrize(
    "root, expected",
    [
        (make_terminal("t1", name="api", where="payments / 3", cwd=Path("/w/api")), "payments"),
        (make_terminal("t1", name="api", where="payments", cwd=Path("/w/api")), "payments"),
        (make_terminal("t1", name="api", where=None, cwd=Path("/w/api-repo")), "api"),
        (make_terminal("t1", name=None, where=None, cwd=Path("/w/api-repo")), "api-repo"),
        (make_terminal("t1", name=None, where=" / 1", cwd=Path("/")), "t1"),
    ],
    ids=["space-of-where", "where-without-tab", "name", "folder", "address"],
)
def test_the_project_label_falls_back_from_space_to_name_to_folder(
    root: Terminal, expected: str
) -> None:
    child = make_terminal("t2", name="child", where="elsewhere / 1", parent="t1")

    places = _places(root, child)

    assert places["t2"].project == expected


def test_a_cycle_is_cut_in_terminal_number_order_not_text_order() -> None:
    # In text order "t10" < "t9"; hc numbers t9 before t10.
    places = _places(make_terminal("t10", parent="t9"), make_terminal("t9", parent="t10"))

    assert places["t9"].parent is None
    assert places["t9"].role is SessionRole.ORCHESTRATOR
    assert places["t10"].parent == "t9"
