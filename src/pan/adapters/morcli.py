from __future__ import annotations

import json
import subprocess
from typing import Any

from pan.errors import MorcliError
from pan.logging import initialise_logger
from pan.models import WorkerStatus

logger = initialise_logger(__name__)

_MORCLI = "morcli"

# morcli/herdr agent status string -> pan WorkerStatus. SPAWNING is never reported by
# morcli (it is the pre-launch state set in the thread map). "unknown" means morcli
# cannot determine the session's state, surfaced as FAILED so the user investigates.
_STATUS_MAP = {
    "working": WorkerStatus.RUNNING,
    "idle": WorkerStatus.RUNNING,
    "blocked": WorkerStatus.BLOCKED,
    "done": WorkerStatus.DONE,
    "unknown": WorkerStatus.FAILED,
}


class ShellMorcliAdapter:
    def session_status(self, handle: str) -> WorkerStatus:
        for stream in self._run_streams():
            if not isinstance(stream, dict):
                continue
            if stream.get("session_id") == handle or stream.get("workspace_id") == handle:
                status = self._map_status(stream.get("status"), handle)
                logger.info(f"morcli status handle={handle} status={status}")
                return status
        raise MorcliError(f"no morcli stream for handle={handle}")

    def resolve_session(self, workspace_id: str) -> str | None:
        # Turn a freshly-created workspace id into morcli's session handle, best-effort.
        # Returns None (not raising) when no stream matches yet — the just-spawned-session
        # indexing lag is expected, and the handle is resolved later on the next status
        # query, where the workspace id is still a valid morcli match.
        for stream in self._run_streams():
            if not isinstance(stream, dict):
                continue
            if (
                stream.get("workspace_id") == workspace_id
                or stream.get("session_id") == workspace_id
            ):
                session_id = stream.get("session_id")
                if isinstance(session_id, str):
                    logger.info(f"morcli resolved workspace={workspace_id} session={session_id}")
                    return session_id
        return None

    def _map_status(self, raw_status: object, handle: str) -> WorkerStatus:
        mapped = _STATUS_MAP.get(raw_status) if isinstance(raw_status, str) else None
        if mapped is None:
            raise MorcliError(f"unrecognized morcli status for handle={handle}")
        return mapped

    def _run_streams(self) -> list[Any]:
        command = [_MORCLI, "streams", "--json"]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as error:
            raise MorcliError("failed to run morcli streams") from error

        if completed.returncode != 0:
            detail = completed.stderr.strip()
            raise MorcliError(f"morcli streams exited with code {completed.returncode}: {detail}")

        try:
            parsed = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise MorcliError("morcli streams produced non-JSON output") from error

        if not isinstance(parsed, list):
            raise MorcliError("morcli streams output is not a list")
        return parsed
