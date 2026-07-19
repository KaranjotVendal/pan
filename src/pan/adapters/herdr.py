from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from pan.errors import HerdrError
from pan.logging import initialise_logger
from pan.models import AgentStatus, LiveSession

logger = initialise_logger(__name__)

_HERDR = "herdr"


def _map_agent_status(raw_status: object) -> AgentStatus:
    # Map herdr's vendor agent-status string to the closed AgentStatus set; any value
    # that is not a known member (or a missing/non-string one) absorbs to UNKNOWN so an
    # unexpected vendor value never crashes the adapter (tech-spec R-9).
    if isinstance(raw_status, str):
        try:
            return AgentStatus(raw_status)
        except ValueError:
            return AgentStatus.UNKNOWN
    return AgentStatus.UNKNOWN


class ShellHerdrAdapter:
    def create_workspace(self, label: str, cwd: Path) -> tuple[str, str]:
        create_result = self._run(
            ["workspace", "create", "--cwd", str(cwd), "--label", label, "--no-focus"]
        )
        workspace = create_result.get("workspace")
        if not isinstance(workspace, dict) or "workspace_id" not in workspace:
            raise HerdrError(f"herdr workspace create returned no workspace for label={label}")
        workspace_id = str(workspace["workspace_id"])
        active_tab_id = workspace.get("active_tab_id")

        # `workspace create` returns only workspace metadata (no pane), so resolve the
        # created pane with a follow-up `pane list`.
        pane_result = self._run(["pane", "list", "--workspace", workspace_id])
        panes = pane_result.get("panes")
        if not isinstance(panes, list) or not panes:
            raise HerdrError(f"herdr pane list returned no panes for workspace={workspace_id}")
        pane_id = self._select_pane_id(panes, active_tab_id)

        logger.info(f"herdr workspace created workspace={workspace_id} pane={pane_id}")
        return workspace_id, pane_id

    def nudge(self, pane_id: str) -> None:
        # The fixed, content-free nudge: a bare Enter keypress wakes the orchestrator
        # session to run its drain loop; no payload ever crosses the pane (INV-2).
        self._run(["pane", "send-keys", pane_id, "Enter"], expect_json=False)
        logger.info(f"herdr nudge pane={pane_id}")

    def send_text(self, pane_id: str, text: str) -> None:
        self._run(["pane", "send-text", pane_id, text], expect_json=False)
        logger.info(f"herdr send-text pane={pane_id} len={len(text)}")

    def kill_pane(self, pane_id: str) -> None:
        self._run(["pane", "close", pane_id], expect_json=False)
        logger.info(f"herdr kill pane={pane_id}")

    def list_workspaces(self) -> list[LiveSession]:
        # Enumerate every live herdr workspace, then each workspace's panes, mapping the
        # vendor JSON to LiveSession domain values (INV-8, BR-2). Each pane becomes one
        # LiveSession; its agent_status carries whether it is an active agent or an idle
        # shell. The exact `workspace list` / `pane list` JSON shape is version-dependent
        # and deferred to live verification (tech-spec R-9).
        workspaces = self._run(["workspace", "list"]).get("workspaces")
        if not isinstance(workspaces, list):
            raise HerdrError("herdr workspace list output is missing a workspaces array")

        sessions: list[LiveSession] = []
        for workspace in workspaces:
            if not isinstance(workspace, dict) or "workspace_id" not in workspace:
                continue
            workspace_id = str(workspace["workspace_id"])
            workspace_name = str(workspace.get("label") or workspace.get("name") or workspace_id)
            workspace_cwd = workspace.get("cwd")

            panes = self._run(["pane", "list", "--workspace", workspace_id]).get("panes")
            if not isinstance(panes, list):
                continue
            for pane in panes:
                if not isinstance(pane, dict) or "pane_id" not in pane:
                    continue
                cwd_value = pane.get("cwd") or workspace_cwd
                if not isinstance(cwd_value, str):
                    continue
                sessions.append(
                    LiveSession(
                        workspace_name=workspace_name,
                        workspace_id=workspace_id,
                        pane_id=str(pane["pane_id"]),
                        cwd=Path(cwd_value),
                        agent_status=_map_agent_status(pane.get("agent_status")),
                    )
                )

        logger.info(f"herdr list_workspaces sessions={len(sessions)}")
        return sessions

    def _select_pane_id(self, panes: list[Any], active_tab_id: object) -> str:
        if isinstance(active_tab_id, str):
            for pane in panes:
                if (
                    isinstance(pane, dict)
                    and pane.get("tab_id") == active_tab_id
                    and "pane_id" in pane
                ):
                    return str(pane["pane_id"])
        first_pane = panes[0]
        if not isinstance(first_pane, dict) or "pane_id" not in first_pane:
            raise HerdrError("herdr pane list entry is missing a pane_id")
        return str(first_pane["pane_id"])

    def _run(self, args: list[str], expect_json: bool = True) -> dict[str, Any]:
        command = [_HERDR, *args]
        subcommand = " ".join(args[:2])
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as error:
            raise HerdrError(f"failed to run herdr {subcommand}") from error

        if completed.returncode != 0:
            raise HerdrError(f"herdr {subcommand} exited with code {completed.returncode}")

        # Some herdr subcommands (pane send-text/send-keys/close) print nothing on
        # success; there is no envelope to parse, so callers pass expect_json=False.
        if not expect_json:
            return {}

        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise HerdrError(f"herdr {subcommand} produced non-JSON output") from error

        result = envelope.get("result") if isinstance(envelope, dict) else None
        if not isinstance(result, dict):
            raise HerdrError(f"herdr {subcommand} output is missing a result object")
        return result
