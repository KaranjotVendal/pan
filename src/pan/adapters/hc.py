from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from pan.errors import (
    TargetBlockedError,
    TerminalGoneError,
    TerminalHostError,
    TerminalNameTakenError,
)
from pan.logging import initialise_logger
from pan.models import AgentStatus, Terminal, TerminalRead, TerminalState

logger = initialise_logger(__name__)

_HC = "hc"


# A failure of the `hc` program itself (missing binary, unexpected output, or an hc error
# kind pan has no typed error for). Callers outside this adapter catch TerminalHostError.
class HcError(TerminalHostError): ...


# hc error kinds (from the `{error: {kind, message, hint}}` object on stderr) that pan
# handles distinctly; any other kind is a plain HcError.
_ERROR_KINDS: dict[str, type[TerminalHostError]] = {
    "not_found": TerminalGoneError,
    "finished": TerminalGoneError,
    "blocked": TargetBlockedError,
    "name_taken": TerminalNameTakenError,
}


def _map_agent_status(raw_status: object) -> AgentStatus:
    # `hc ls` reports status as a flat string, `hc get` as an object whose `state` holds
    # it; both omit it (or send null) for a program that reports none. An unrecognized
    # value absorbs to UNKNOWN so an unexpected vendor value never crashes the adapter.
    if isinstance(raw_status, dict):
        raw_status = raw_status.get("state")
    if raw_status is None:
        return AgentStatus.NONE
    if isinstance(raw_status, str):
        try:
            return AgentStatus(raw_status)
        except ValueError:
            return AgentStatus.UNKNOWN
    return AgentStatus.UNKNOWN


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _agent_name(entry: dict) -> str | None:
    # `ls` names the foreground program in `agent`; `get` carries it as the status app,
    # falling back to the launched program (argv[0]) and then the foreground program's name
    # for a program that reports no status yet. Claude Code's foreground name is its version
    # number, so a fresh session is only recognisable by what was launched.
    agent = _optional_string(entry.get("agent"))
    if agent is not None:
        return agent
    status = entry.get("status")
    if isinstance(status, dict):
        agent = _optional_string(status.get("app"))
        if agent is not None:
            return agent
    argv = entry.get("argv")
    if isinstance(argv, list) and argv:
        launched = _optional_string(argv[0])
        if launched is not None:
            return Path(launched).name
    foreground = entry.get("foreground")
    if isinstance(foreground, dict):
        return _optional_string(foreground.get("name"))
    return None


def _turn(entry: dict) -> int | None:
    turn = entry.get("turn")
    return turn if isinstance(turn, int) and not isinstance(turn, bool) else None


def _where(entry: dict) -> str | None:
    where = _optional_string(entry.get("where"))
    if where is not None:
        return where
    labels = entry.get("labels")
    if isinstance(labels, dict):
        return _optional_string(labels.get("where"))
    return None


def _to_terminal(entry: object) -> Terminal | None:
    # One hc terminal entry (from ls, get or run) to the Terminal domain value. Entries
    # without an address or a launch folder cannot be addressed or joined, so they are
    # skipped rather than failing the whole listing.
    if not isinstance(entry, dict):
        return None
    address = _optional_string(entry.get("address"))
    cwd = _optional_string(entry.get("cwd"))
    if address is None or cwd is None:
        return None
    state = TerminalState.LIVE if entry.get("state") == "live" else TerminalState.FINISHED
    return Terminal(
        address=address,
        name=_optional_string(entry.get("name")),
        where=_where(entry),
        cwd=Path(cwd),
        state=state,
        agent=_agent_name(entry),
        agent_status=_map_agent_status(entry.get("status")),
        turn=_turn(entry),
    )


def _label_arguments(labels: dict[str, str]) -> list[str]:
    arguments: list[str] = []
    for key, value in labels.items():
        arguments.extend(["--label", f"{key}={value}"])
    return arguments


class ShellHcAdapter:
    # The hc implementation of the TerminalHost seam. hc labels are supported natively.
    def run(self, *, name: str, cwd: Path, argv: list[str], labels: dict[str, str]) -> Terminal:
        # Start a program in a new background terminal. `--no-parent` keeps it independent
        # of whichever terminal invoked pan, so it outlives that session and hc types no
        # notes about it into it (INV-2).
        result = self._run(
            [
                "run",
                "--bg",
                "--json",
                "--no-parent",
                "--name",
                name,
                "--cwd",
                str(cwd),
                *_label_arguments(labels),
                "--",
                *argv,
            ]
        )
        terminal = _to_terminal(result)
        if terminal is None:
            raise HcError(f"hc run returned no terminal for name={name}")
        logger.info(f"hc run terminal={terminal.address} name={name}")
        return terminal

    def send(self, terminal: str, text: str) -> None:
        # `hc send` pastes the text and presses Enter in one call. A blocked target raises
        # TargetBlockedError; pan never passes --force past a person's decision.
        self._run(["send", "--json", terminal, text])
        logger.info(f"hc send terminal={terminal} len={len(text)}")

    def read(self, terminal: str, lines: int) -> TerminalRead:
        result = self._run(["read", "--json", "--lines", str(lines), terminal])
        text = result.get("text")
        if not isinstance(text, str):
            raise HcError(f"hc read returned no text for terminal={terminal}")
        complete = result.get("complete") is True
        logger.info(f"hc read terminal={terminal} lines={lines} complete={complete}")
        return TerminalRead(text=text, complete=complete)

    def get(self, terminal: str) -> Terminal:
        found = _to_terminal(self._run(["get", "--json", terminal]))
        if found is None:
            raise HcError(f"hc get returned no terminal for terminal={terminal}")
        return found

    def list_terminals(self, labels: dict[str, str] | None = None) -> list[Terminal]:
        result = self._run(["ls", "--json", *_label_arguments(labels or {})])
        entries = result.get("terminals")
        if not isinstance(entries, list):
            raise HcError("hc ls output is missing a terminals array")
        terminals = [terminal for terminal in map(_to_terminal, entries) if terminal is not None]
        logger.info(f"hc ls terminals={len(terminals)}")
        return terminals

    def remove(self, terminal: str) -> None:
        # Kill then remove. A terminal that is already gone counts as removed, so stopping
        # twice (or stopping a worker lost to a reboot) succeeds.
        try:
            self._run(["kill", "--json", terminal])
            self._run(["rm", "--json", terminal])
        except TerminalGoneError:
            logger.info(f"hc remove terminal={terminal} already gone")
            return
        logger.info(f"hc remove terminal={terminal}")

    def _run(self, args: list[str]) -> dict[str, Any]:
        command = [_HC, *args]
        subcommand = args[0]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as error:
            raise HcError(f"failed to run hc {subcommand}") from error

        if completed.returncode != 0:
            raise self._error_for(subcommand, completed.returncode, completed.stderr)

        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise HcError(f"hc {subcommand} produced non-JSON output") from error
        if not isinstance(parsed, dict):
            raise HcError(f"hc {subcommand} output is not a JSON object")
        return parsed

    def _error_for(self, subcommand: str, returncode: int, stderr: str) -> TerminalHostError:
        # hc reports failures as `{error: {kind, message, hint}}` on stderr. The kind picks
        # the typed error; the message is hc's own (terminal names, never payloads).
        kind = None
        message = ""
        try:
            parsed = json.loads(stderr)
        except json.JSONDecodeError:
            parsed = None
        error_object = parsed.get("error") if isinstance(parsed, dict) else None
        if isinstance(error_object, dict):
            kind = error_object.get("kind")
            message = str(error_object.get("message", ""))
        error_type: type[TerminalHostError] = (
            _ERROR_KINDS.get(kind, HcError) if isinstance(kind, str) else HcError
        )
        return error_type(f"hc {subcommand} failed (code {returncode}, kind {kind}): {message}")
