from __future__ import annotations

import subprocess
from pathlib import Path

from pan.errors import SpawnError
from pan.logging import initialise_logger

logger = initialise_logger(__name__)

_GIT = "git"


class ShellGitWorktreeAdapter:
    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path:
        worktree_path = base / branch
        # Guard against a branch name that would place the worktree outside `base`
        # (e.g. "../escape" or an absolute path).
        if not worktree_path.resolve().is_relative_to(base.resolve()):
            raise SpawnError(f"worktree branch escapes base: branch={branch!r}")

        self._run(["-C", str(repo), "worktree", "add", str(worktree_path), "-b", branch])
        logger.info(f"git worktree created path={worktree_path} branch={branch}")
        return worktree_path

    def remove_worktree(self, path: Path) -> None:
        # A worker leaves its worktree dirty (untracked build output, uncommitted
        # edits), so teardown must force removal — the caller has already captured
        # the worker's result by this point.
        self._run(["-C", str(path), "worktree", "remove", "--force", str(path)])
        logger.info(f"git worktree removed path={path}")

    def _run(self, args: list[str]) -> None:
        command = [_GIT, *args]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as error:
            raise SpawnError(f"failed to run {' '.join(command)}") from error

        if completed.returncode != 0:
            detail = completed.stderr.strip()
            raise SpawnError(
                f"git worktree command exited with code {completed.returncode}: {detail}"
            )
