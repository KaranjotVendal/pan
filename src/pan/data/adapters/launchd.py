from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from pan.core.errors import ServiceError
from pan.core.models import LaunchdJobState
from pan.utils.config import LAUNCHD_BOOTSTRAP_ATTEMPTS, LAUNCHD_BOOTSTRAP_RETRY_SECONDS
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

_LAUNCHCTL = "launchctl"

# `launchctl print <domain>/<label>` lists the job's own fields one tab deep
# ("\tpid = 412", "\tlast exit code = 0", "\tlast exit code = (never exited)"); nested blocks
# (endpoints, event triggers) sit deeper and are skipped, so their `state` lines never mix in.
_TOP_LEVEL_FIELD = re.compile(r"^\t(?P<key>[a-z][a-z ]*?) = (?P<value>.*)$")
_LEADING_INTEGER = re.compile(r"^-?\d+")


def _leading_integer(value: str | None) -> int | None:
    if value is None:
        return None
    match = _LEADING_INTEGER.match(value.strip())
    return int(match.group()) if match else None


def parse_job_state(print_output: str) -> LaunchdJobState:
    fields: dict[str, str] = {}
    for line in print_output.splitlines():
        match = _TOP_LEVEL_FIELD.match(line)
        if match is not None:
            fields.setdefault(match.group("key"), match.group("value").strip())
    return LaunchdJobState(
        pid=_leading_integer(fields.get("pid")),
        last_exit_code=_leading_integer(fields.get("last exit code")),
        state=fields.get("state"),
    )


class LaunchctlServiceManager:
    # Loads agents into the logged-in user's GUI domain (gui/<uid>), where per-user
    # LaunchAgents run at login.
    def __init__(
        self,
        launch_agents_dir: Path,
        *,
        uid: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._launch_agents_dir = launch_agents_dir
        self._domain = f"gui/{uid if uid is not None else os.getuid()}"
        self._sleep = sleep

    def _plist_path(self, label: str) -> Path:
        return self._launch_agents_dir / f"{label}.plist"

    def plist_installed(self, label: str) -> bool:
        return self._plist_path(label).is_file()

    def write_plist(self, label: str, content: str) -> Path:
        plist_path = self._plist_path(label)
        try:
            self._launch_agents_dir.mkdir(parents=True, exist_ok=True)
            plist_path.write_text(content)
        except OSError as error:
            raise ServiceError(f"could not write {plist_path}: {error}") from error
        logger.info(f"launchd plist written path={plist_path}")
        return plist_path

    def remove_plist(self, label: str) -> bool:
        plist_path = self._plist_path(label)
        try:
            plist_path.unlink()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise ServiceError(f"could not delete {plist_path}: {error}") from error
        logger.info(f"launchd plist deleted path={plist_path}")
        return True

    def ensure_directory(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ServiceError(f"could not create {path}: {error}") from error

    def job_state(self, label: str) -> LaunchdJobState | None:
        # Any failure of `print` (113 "Could not find service" when it is not loaded) reads
        # as not loaded: status must not fail on a job launchd does not know.
        completed = self._run(["print", f"{self._domain}/{label}"])
        if completed.returncode != 0:
            logger.debug(
                f"launchd job not loaded label={label} code={completed.returncode} "
                f"detail={completed.stderr.strip() or completed.stdout.strip()}"
            )
            return None
        return parse_job_state(completed.stdout)

    def bootstrap(self, plist_path: Path) -> None:
        command = ["bootstrap", self._domain, str(plist_path)]
        detail = ""
        for attempt in range(1, LAUNCHD_BOOTSTRAP_ATTEMPTS + 1):
            completed = self._run(command)
            if completed.returncode == 0:
                logger.info(f"launchd job loaded plist={plist_path} attempt={attempt}")
                return
            detail = completed.stderr.strip() or completed.stdout.strip()
            logger.warning(
                f"launchctl bootstrap failed plist={plist_path} attempt={attempt} "
                f"code={completed.returncode} detail={detail}"
            )
            if attempt < LAUNCHD_BOOTSTRAP_ATTEMPTS:
                self._sleep(LAUNCHD_BOOTSTRAP_RETRY_SECONDS)
        raise ServiceError(
            f"launchctl {' '.join(command)} failed after {LAUNCHD_BOOTSTRAP_ATTEMPTS} "
            f"attempts: {detail}"
        )

    def bootout(self, label: str) -> None:
        target = f"{self._domain}/{label}"
        completed = self._run(["bootout", target])
        if completed.returncode == 0:
            logger.info(f"launchd job unloaded label={label}")
            return
        if self.job_state(label) is None:
            # Already gone (it was never loaded, or launchd finished removing it meanwhile).
            logger.info(f"launchd job already unloaded label={label}")
            return
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ServiceError(
            f"launchctl bootout {target} exited with code {completed.returncode}: {detail}"
        )

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        command = [_LAUNCHCTL, *args]
        try:
            return subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as error:
            raise ServiceError(f"failed to run {' '.join(command)}: {error}") from error


def build_service_manager(home: Path) -> LaunchctlServiceManager:
    if sys.platform != "darwin":
        raise ServiceError("pan service manages launchd agents, which exist only on macOS")
    return LaunchctlServiceManager(home / "Library" / "LaunchAgents")
