from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pan.core.errors import TerminalGoneError, TerminalHostError
from pan.core.models import AgentStatus, Terminal, TerminalRead, TerminalState


@dataclass
class RunRequest:
    name: str
    cwd: Path
    argv: list[str]
    labels: dict[str, str]


def make_terminal(
    address: str,
    *,
    name: str | None = None,
    where: str | None = None,
    cwd: Path = Path("/tmp/x"),
    state: TerminalState = TerminalState.LIVE,
    agent: str | None = "claude-code",
    agent_status: AgentStatus = AgentStatus.IDLE,
    turn: int | None = None,
    parent: str | None = None,
) -> Terminal:
    return Terminal(
        address=address,
        name=name,
        where=where,
        cwd=cwd,
        state=state,
        agent=agent,
        agent_status=agent_status,
        turn=turn,
        parent=parent,
    )


@dataclass
class FakeTerminalHost:
    # An in-memory TerminalHost. `terminals` maps address -> Terminal; `labels` maps address ->
    # its labels for `list_terminals` filtering. `errors` maps an operation name ("run",
    # "send", "read", "get", "list") to an error it raises.
    terminals: dict[str, Terminal] = field(default_factory=dict)
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: dict[str, TerminalHostError] = field(default_factory=dict)
    read_result: TerminalRead = field(
        default_factory=lambda: TerminalRead(text="recent output", complete=True)
    )
    runs: list[RunRequest] = field(default_factory=list)
    sent: list[tuple[str, str]] = field(default_factory=list)
    reads: list[tuple[str, int]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)

    def add(self, terminal: Terminal, labels: dict[str, str] | None = None) -> Terminal:
        self.terminals[terminal.address] = terminal
        self.labels[terminal.address] = labels or {}
        return terminal

    def run(self, *, name: str, cwd: Path, argv: list[str], labels: dict[str, str]) -> Terminal:
        self.timeline.append("run")
        if "run" in self.errors:
            raise self.errors["run"]
        self.runs.append(RunRequest(name=name, cwd=cwd, argv=argv, labels=labels))
        terminal = make_terminal(
            f"t{100 + len(self.runs)}",
            name=name,
            cwd=cwd,
            agent=argv[0],
            agent_status=AgentStatus.NONE,
        )
        return self.add(terminal, labels)

    def send(self, terminal: str, text: str) -> None:
        self.timeline.append("send")
        if "send" in self.errors:
            raise self.errors["send"]
        self.sent.append((terminal, text))

    def read(self, terminal: str, lines: int) -> TerminalRead:
        if "read" in self.errors:
            raise self.errors["read"]
        self.reads.append((terminal, lines))
        return self.read_result

    def get(self, terminal: str) -> Terminal:
        if "get" in self.errors:
            raise self.errors["get"]
        for found in self.terminals.values():
            if terminal in (found.address, found.name):
                return found
        raise TerminalGoneError(f"no terminal {terminal}")

    def list_terminals(self, labels: dict[str, str] | None = None) -> list[Terminal]:
        if "list" in self.errors:
            raise self.errors["list"]
        wanted = labels or {}
        return [
            terminal
            for terminal in self.terminals.values()
            if terminal.state is TerminalState.LIVE
            and all(
                self.labels[terminal.address].get(key) == value for key, value in wanted.items()
            )
        ]

    def remove(self, terminal: str) -> None:
        self.timeline.append("remove")
        self.removed.append(terminal)
        for address, found in list(self.terminals.items()):
            if terminal in (found.address, found.name):
                del self.terminals[address]
