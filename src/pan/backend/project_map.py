from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from pan.backend.delivery import is_live_agent_session
from pan.core.models import (
    MappedProject,
    MappedSession,
    ProjectMap,
    SessionRole,
    Terminal,
)
from pan.core.seams import GitWorktreeAdapter
from pan.data.workset import read_workset_goal
from pan.utils.config import STREAM_NAME_PREFIX, WORKSET_RELATIVE_PATH
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# pan's built-in project map: the ProjectMapSource behind config `project_map` "builtin"
# (backend.project_map_sources). Only that registry imports this module; everything else
# reads the map through the seam. It mirrors herdrmap's map (`herdrmap map --json --all`), so
# it can be compared with herdrmap's output and deleted once herdrmap is plugged in.

# The Herdr app's "space / tab" label; the space is the part before the separator.
_WHERE_SEPARATOR = " / "
# hc addresses a terminal on another machine as machine/tN; a local one has no slash.
_REMOTE_ADDRESS_SEPARATOR = "/"
# hc numbers terminals in base 36 (t9, ta, ..., tz, t10), after a one-letter prefix.
_TERMINAL_NUMBER = re.compile(r"^[a-z]([0-9a-z]+)$")
_TERMINAL_NUMBER_BASE = 36


class ProjectPlace(BaseModel, frozen=True):
    # Where one terminal sits in the project map: its live parent (None when it has none),
    # its role, and the root of its parent chain (an address) with that root's label.
    parent: str | None
    role: SessionRole
    project: str
    project_root: str


def is_remote(address: str) -> bool:
    # Its folder is on another machine, so it cannot be read from here.
    return _REMOTE_ADDRESS_SEPARATOR in address


def _parent_address(terminal: Terminal, mapped: dict[str, Terminal]) -> str | None:
    # hc may report a remote terminal's parent without its machine: the parent is then that
    # machine's terminal when one exists, else the local one (a Mac session that started work
    # on another machine).
    parent = terminal.parent
    if parent is None:
        return None
    machine, separator, _number = terminal.address.rpartition(_REMOTE_ADDRESS_SEPARATOR)
    if separator and not is_remote(parent):
        same_machine = f"{machine}{separator}{parent}"
        if same_machine in mapped:
            return same_machine
    return parent


def _live_parents(terminals: list[Terminal]) -> dict[str, str]:
    # Each mapped terminal's parent, when that parent is itself mapped. Only live agent
    # sessions are mapped. A parent that is gone (or finished, or not an agent session)
    # counts as no parent: the stream is left without its orchestrator, so it stands alone as
    # the root of its own project rather than being grouped under a session that no longer
    # coordinates it.
    mapped = {
        terminal.address: terminal for terminal in terminals if is_live_agent_session(terminal)
    }
    parents: dict[str, str] = {}
    for address, terminal in mapped.items():
        parent = _parent_address(terminal, mapped)
        if parent is not None and parent != address and parent in mapped:
            parents[address] = parent
    return parents


def address_order(address: str) -> tuple[str, int, int, str]:
    # Addresses in hc's own order: by machine (local first), then by terminal number, so t9
    # comes before t10. An address in another shape sorts after, by its text.
    machine, _separator, local = address.rpartition(_REMOTE_ADDRESS_SEPARATOR)
    match = _TERMINAL_NUMBER.match(local)
    if match is None:
        return (machine, 1, 0, local)
    return (machine, 0, int(match.group(1), _TERMINAL_NUMBER_BASE), local)


def _without_cycles(parents: dict[str, str]) -> dict[str, str]:
    # hc should never report a cycle, but a chain is walked to its root, so one would loop
    # forever. Each cycle is cut at its lowest address (address_order), which then roots the
    # project.
    acyclic = dict(parents)
    for start in sorted(acyclic, key=address_order):
        path: list[str] = []
        current = start
        while current in acyclic and current not in path:
            path.append(current)
            current = acyclic[current]
        if current in path:
            cut = min(path[path.index(current) :], key=address_order)
            logger.warning(f"project map parent cycle cut at terminal={cut}")
            del acyclic[cut]
    return acyclic


def _root_of(address: str, parents: dict[str, str]) -> str:
    while address in parents:
        address = parents[address]
    return address


def project_label(root: Terminal) -> str:
    # The Herdr space the root sits in, else its name, else its folder's name, else (a root
    # folder) its address.
    if root.where:
        space = root.where.partition(_WHERE_SEPARATOR)[0].strip()
        if space:
            return space
    return root.name or root.cwd.name or root.address


def map_projects(terminals: list[Terminal]) -> dict[str, ProjectPlace]:
    # Every terminal's place, by address. An orchestrator has live children and no live
    # parent; a stream has a live parent (wherever it sits in the Herdr app: spaces are not
    # reliable, parent links are); everything else, non-agent terminals included, is
    # standalone.
    by_address = {terminal.address: terminal for terminal in terminals}
    parents = _without_cycles(_live_parents(terminals))
    has_children = set(parents.values())
    places: dict[str, ProjectPlace] = {}
    for terminal in terminals:
        parent = parents.get(terminal.address)
        if parent is not None:
            role = SessionRole.STREAM
        elif terminal.address in has_children:
            role = SessionRole.ORCHESTRATOR
        else:
            role = SessionRole.STANDALONE
        root = by_address[_root_of(terminal.address, parents)]
        places[terminal.address] = ProjectPlace(
            parent=parent, role=role, project=project_label(root), project_root=root.address
        )
    return places


class _BranchReader:
    # One git read per distinct local folder of a live agent session; other terminals (shells,
    # dev servers, finished ones) and folders on another machine get no branch.
    def __init__(self, git: GitWorktreeAdapter) -> None:
        self._git = git
        self._branches: dict[Path, str | None] = {}

    def branch_of(self, terminal: Terminal) -> str | None:
        if is_remote(terminal.address) or not is_live_agent_session(terminal):
            return None
        if terminal.cwd not in self._branches:
            self._branches[terminal.cwd] = self._git.current_branch(terminal.cwd)
        return self._branches[terminal.cwd]


def _workset_goal(stream: Terminal, root: Terminal) -> str | None:
    # The stream's workset file, looked for under its project root's folder and then its own
    # (the same folder for a root). Folders on another machine are not read.
    if stream.name is None:
        return None
    stream_folder_name = stream.name.removeprefix(STREAM_NAME_PREFIX)
    if not stream_folder_name or "/" in stream_folder_name or stream_folder_name in {".", ".."}:
        # hc names never hold these; refuse rather than read outside docs/streams.
        return None
    relative_path = WORKSET_RELATIVE_PATH.format(stream=stream_folder_name)
    folders: list[Path] = []
    for candidate in (root, stream):
        if not is_remote(candidate.address) and candidate.cwd not in folders:
            folders.append(candidate.cwd)
    for folder in folders:
        goal = read_workset_goal(folder / relative_path)
        if goal is not None:
            return goal
    return None


def _depth_of(address: str, places: dict[str, ProjectPlace]) -> int:
    depth = 0
    parent = places[address].parent
    while parent is not None:
        depth += 1
        parent = places[parent].parent
    return depth


def _tree_order(root: str, children: dict[str, list[str]]) -> list[str]:
    # The root, then each child followed by its own subtree, children in the host's order.
    ordered: list[str] = []
    pending = [root]
    while pending:
        address = pending.pop()
        ordered.append(address)
        pending.extend(reversed(children.get(address, [])))
    return ordered


class BuiltinProjectMapSource:
    # The map built from the host's own listing (parent links, see map_projects): projects in
    # the host's order of their orchestrators, then the standalone agent sessions, then every
    # other terminal, as herdrmap lays out `map --json --all`. Each live local agent session's
    # branch costs one git read per folder; a stream's goal is read from its workset file.
    def __init__(self, git: GitWorktreeAdapter) -> None:
        self._git = git

    def project_map(self, terminals: list[Terminal]) -> ProjectMap:
        places = map_projects(terminals)
        by_address = {terminal.address: terminal for terminal in terminals}
        branch_reader = _BranchReader(self._git)
        sessions: dict[str, MappedSession] = {}
        children: dict[str, list[str]] = {}
        for terminal in terminals:
            place = places[terminal.address]
            if place.parent is not None:
                children.setdefault(place.parent, []).append(terminal.address)
            sessions[terminal.address] = MappedSession(
                address=terminal.address,
                name=terminal.name,
                cwd=terminal.cwd,
                agent=terminal.agent,
                status=terminal.agent_status,
                state=terminal.state,
                role=place.role,
                parent=place.parent,
                depth=_depth_of(terminal.address, places),
                branch=branch_reader.branch_of(terminal),
                goal=(
                    _workset_goal(terminal, by_address[place.project_root])
                    if place.role is SessionRole.STREAM
                    else None
                ),
            )
        projects = [
            MappedProject(
                label=places[terminal.address].project,
                root=terminal.address,
                sessions=[sessions[address] for address in _tree_order(terminal.address, children)],
            )
            for terminal in terminals
            if places[terminal.address].role is SessionRole.ORCHESTRATOR
        ]
        standalone = [
            sessions[terminal.address]
            for terminal in terminals
            if places[terminal.address].role is SessionRole.STANDALONE
            and is_live_agent_session(terminal)
        ]
        other = [
            sessions[terminal.address]
            for terminal in terminals
            if not is_live_agent_session(terminal)
        ]
        logger.debug(
            f"project map built projects={len(projects)} standalone={len(standalone)} "
            f"other={len(other)}"
        )
        return ProjectMap(projects=projects, standalone=standalone, other=other)
