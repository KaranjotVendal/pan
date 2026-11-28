from __future__ import annotations

from dataclasses import dataclass, field

from pan.backend.delivery import is_live_agent_session
from pan.core.models import (
    MappedProject,
    MappedSession,
    ProjectMap,
    SessionRole,
    Terminal,
)


def _mapped(
    terminal: Terminal,
    role: SessionRole,
    *,
    parent: str | None,
    branch: str | None,
    goal: str | None,
) -> MappedSession:
    return MappedSession(
        address=terminal.address,
        name=terminal.name,
        cwd=terminal.cwd,
        agent=terminal.agent,
        status=terminal.agent_status,
        state=terminal.state,
        role=role,
        parent=parent,
        depth=0 if parent is None else 1,
        branch=branch,
        goal=goal,
    )


@dataclass
class FakeProjectMapSource:
    # A ProjectMapSource laid out by hand. `projects` lists each project as its label and its
    # addresses: the orchestrator first, then its streams (each one level down). A project whose
    # orchestrator is not in the listing is left out, its streams standing alone. Every other
    # live agent session is standalone, and the remaining terminals are `other`. `branches` and
    # `goals` give a session's branch and goal by address. Each listing it maps is kept in
    # `listings`.
    projects: list[tuple[str, list[str]]] = field(default_factory=list)
    branches: dict[str, str] = field(default_factory=dict)
    goals: dict[str, str] = field(default_factory=dict)
    listings: list[list[Terminal]] = field(default_factory=list)

    def project_map(self, terminals: list[Terminal]) -> ProjectMap:
        self.listings.append(terminals)
        by_address = {terminal.address: terminal for terminal in terminals}
        placed: set[str] = set()
        mapped_projects: list[MappedProject] = []
        for label, (root, *streams) in self.projects:
            if root not in by_address:
                continue
            members = [
                (by_address[root], SessionRole.ORCHESTRATOR, None),
                *(
                    (by_address[stream], SessionRole.STREAM, root)
                    for stream in streams
                    if stream in by_address
                ),
            ]
            mapped_projects.append(
                MappedProject(
                    label=label,
                    root=root,
                    sessions=[
                        self._session(terminal, role, parent) for terminal, role, parent in members
                    ],
                )
            )
            placed.update(terminal.address for terminal, _role, _parent in members)
        unplaced = [terminal for terminal in terminals if terminal.address not in placed]
        return ProjectMap(
            projects=mapped_projects,
            standalone=[
                self._session(terminal, SessionRole.STANDALONE, None)
                for terminal in unplaced
                if is_live_agent_session(terminal)
            ],
            other=[
                self._session(terminal, SessionRole.STANDALONE, None)
                for terminal in unplaced
                if not is_live_agent_session(terminal)
            ],
        )

    def _session(self, terminal: Terminal, role: SessionRole, parent: str | None) -> MappedSession:
        return _mapped(
            terminal,
            role,
            parent=parent,
            branch=self.branches.get(terminal.address),
            goal=self.goals.get(terminal.address),
        )
