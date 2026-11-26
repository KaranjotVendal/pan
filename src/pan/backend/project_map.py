from __future__ import annotations

import re

from pydantic import BaseModel

from pan.core.models import AgentStatus, SessionRole, Terminal, TerminalState
from pan.utils.config import KNOWN_AGENT_NAMES
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

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


def is_agent_session(agent: str | None, agent_status: AgentStatus) -> bool:
    # A coding agent: it reports a status, or hc names a known agent as its program (one that
    # has not reported yet). Anything else (a shell, a dev server, `tail`) is not a session:
    # it is never a stream and never makes its parent an orchestrator.
    return agent_status is not AgentStatus.NONE or agent in KNOWN_AGENT_NAMES


def is_live_agent_session(terminal: Terminal) -> bool:
    return terminal.state is TerminalState.LIVE and is_agent_session(
        terminal.agent, terminal.agent_status
    )


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
