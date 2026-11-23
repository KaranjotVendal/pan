from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from pan.config import CLAUDE_AGENT_NAMES, ORCHESTRATOR_ROLE, PAN_ROLE_LABEL
from pan.errors import TerminalGoneError, TerminalHostError, TerminalNameTakenError
from pan.logging import initialise_logger
from pan.models import Terminal, TerminalState, WorkerStatus
from pan.seams import TerminalHost, ThreadMap
from pan.watcher import WAKE_INSTRUCTION

logger = initialise_logger(__name__)

# The first task given to a freshly started orchestrator. It is stateless by design (all
# state is in the inbox and the thread map), so a fresh session after a reboot is correct.
# It drains once on boot because a send to a booting agent can be dropped, and anything
# that arrived while pan was down is already waiting in the inbox.
ORCHESTRATOR_BOOT_BRIEF = (
    "You are the persistent pan orchestrator. Read src/pan/skills/orchestrating/SKILL.md and "
    "follow it: run its drain-classify-route loop once now, then again each time you are "
    "woken with a new-inbox-item message."
)

# Records in these statuses expect a live worker terminal behind them.
_LIVE_STATUSES = frozenset({WorkerStatus.SPAWNING, WorkerStatus.RUNNING, WorkerStatus.BLOCKED})


class UpReport(BaseModel, frozen=True):
    orchestrator_address: str
    started: bool
    woke: bool
    lost_threads: list[str]


def _find(terminal_host: TerminalHost, terminal: str) -> Terminal | None:
    try:
        return terminal_host.get(terminal)
    except TerminalGoneError:
        return None


def _ensure_orchestrator(
    terminal_host: TerminalHost, terminal: str, cwd: Path, command: list[str]
) -> tuple[Terminal, bool]:
    # Returns the orchestrator terminal and whether it was started now. A live Claude
    # session under the name is left alone; a finished one is removed and replaced. A live
    # terminal under the name running anything else is the person's, so pan refuses to
    # touch it rather than killing it.
    existing = _find(terminal_host, terminal)
    if existing is not None and existing.state is TerminalState.LIVE:
        if existing.agent in CLAUDE_AGENT_NAMES:
            return existing, False
        raise TerminalNameTakenError(
            f"live terminal {existing.address} is named {terminal} but is not running Claude "
            "Code; stop it or set a different orchestrator.terminal"
        )
    if existing is not None:
        terminal_host.remove(existing.address)
    started = terminal_host.run(
        name=terminal,
        cwd=cwd,
        argv=[*command, ORCHESTRATOR_BOOT_BRIEF],
        labels={PAN_ROLE_LABEL: ORCHESTRATOR_ROLE},
    )
    logger.info(f"orchestrator started terminal={started.address}")
    return started, True


def reconcile_lost_workers(terminal_host: TerminalHost, thread_map: ThreadMap) -> list[str]:
    # Mark FAILED every record that expects a live worker whose terminal is gone (killed,
    # crashed, or lost to a reboot: the host recovers no worker, since hc workers run
    # without --keep). A later follow-up in the thread reopens in the same worktree. Nothing
    # is posted to Slack here; reporting stuck states belongs to the planned heartbeat.
    lost: list[str] = []
    for record in thread_map.records():
        if record.status not in _LIVE_STATUSES:
            continue
        terminal = (
            _find(terminal_host, record.terminal_address) if record.terminal_address else None
        )
        if terminal is not None and terminal.state is TerminalState.LIVE:
            continue
        thread_map.update_status(record.thread_ts, WorkerStatus.FAILED)
        lost.append(record.thread_ts)
    if lost:
        logger.info(f"reconcile marked lost workers failed count={len(lost)}")
    return lost


def bring_up(
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    *,
    terminal: str,
    cwd: Path,
    command: list[str],
) -> UpReport:
    # Idempotent: ensure the orchestrator runs, reconcile workers lost while pan was down,
    # and wake an already-running orchestrator once so a backlog in the inbox is drained.
    orchestrator, started = _ensure_orchestrator(terminal_host, terminal, cwd, command)
    lost = reconcile_lost_workers(terminal_host, thread_map)
    woke = False
    if not started:
        try:
            terminal_host.send(orchestrator.address, WAKE_INSTRUCTION)
            woke = True
        except TerminalHostError as error:
            logger.warning(f"pan up could not wake orchestrator: {type(error).__name__}")
    return UpReport(
        orchestrator_address=orchestrator.address, started=started, woke=woke, lost_threads=lost
    )
