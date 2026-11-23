from __future__ import annotations

import subprocess
from pathlib import Path

from pan.errors import GitWorktreeError, PanError, SpawnError
from pan.logging import initialise_logger
from pan.models import WorktreeStatus

logger = initialise_logger(__name__)

_GIT = "git"
# `git status --porcelain -z` marks a renamed or copied entry with R or C; its old path
# follows as the next NUL-separated field.
_RENAME_STATUS_LETTERS = frozenset("RC")
# The remote's default branch, as `git clone` (or `git remote set-head`) records it.
_REMOTE_DEFAULT_BRANCH_REF = "refs/remotes/origin/HEAD"
_PORCELAIN_WORKTREE_PREFIX = "worktree "
_PORCELAIN_BRANCH_PREFIX = "branch "


class ShellGitWorktreeAdapter:
    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path:
        worktree_path = base / branch
        # Guard against a branch name that would place the worktree outside `base`
        # (e.g. "../escape" or an absolute path).
        if not worktree_path.resolve().is_relative_to(base.resolve()):
            raise SpawnError(f"worktree branch escapes base: branch={branch!r}")

        self._run(
            ["-C", str(repo), "worktree", "add", str(worktree_path), "-b", branch], SpawnError
        )
        logger.info(f"git worktree created path={worktree_path} branch={branch}")
        return worktree_path

    def worktree_status(self, path: Path) -> WorktreeStatus:
        # Only reading commands run here, so the worktree and its repository are unchanged.
        main_repo, main_branch = self._main_worktree(path)
        top_level = Path(self._run(["-C", str(path), "rev-parse", "--show-toplevel"]).strip())
        branch = self._run_optional(["-C", str(path), "symbolic-ref", "-q", "--short", "HEAD"])
        upstream = self._run_optional(
            ["-C", str(path), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"]
        )
        commits_not_on_upstream = (
            self._count_commits(path, [upstream]) if upstream is not None else 0
        )
        base_refs = [
            ref
            for ref in (
                self._run_optional(
                    ["-C", str(path), "symbolic-ref", "-q", _REMOTE_DEFAULT_BRANCH_REF]
                ),
                main_branch,
            )
            if ref is not None
        ]
        return WorktreeStatus(
            main_repo=main_repo,
            is_main_worktree=top_level.resolve() == main_repo.resolve(),
            branch=branch,
            changed_paths=self._changed_paths(path),
            upstream=upstream,
            commits_not_on_upstream=commits_not_on_upstream,
            commits_not_in_base=self._count_commits(path, base_refs),
        )

    def remove_worktree(self, path: Path, *, force: bool) -> None:
        # Without force, git refuses a worktree holding uncommitted or untracked files
        # (ignored files never block it). The path is absolute, so running from inside the
        # worktree being removed is fine.
        force_flag = ["--force"] if force else []
        self._run(["-C", str(path), "worktree", "remove", *force_flag, str(path)])
        logger.info(f"git worktree removed path={path} force={force}")

    def delete_branch(self, repo: Path, branch: str, *, force: bool) -> None:
        delete_flag = "-D" if force else "-d"
        self._run(["-C", str(repo), "branch", delete_flag, branch])
        logger.info(f"git branch deleted repo={repo} branch={branch} force={force}")

    def _main_worktree(self, path: Path) -> tuple[Path, str | None]:
        # The first entry of `git worktree list` is always the main worktree; its `branch`
        # line is absent when its HEAD is detached.
        listing = self._run(["-C", str(path), "worktree", "list", "--porcelain"])
        main_repo: Path | None = None
        main_branch: str | None = None
        for line in listing.splitlines():
            if not line:
                break
            if line.startswith(_PORCELAIN_WORKTREE_PREFIX):
                main_repo = Path(line.removeprefix(_PORCELAIN_WORKTREE_PREFIX))
            elif line.startswith(_PORCELAIN_BRANCH_PREFIX):
                main_branch = line.removeprefix(_PORCELAIN_BRANCH_PREFIX)
        if main_repo is None:
            raise GitWorktreeError(f"git worktree list named no main worktree for {path}")
        return main_repo, main_branch

    def _changed_paths(self, path: Path) -> list[str]:
        raw_status = self._run(
            ["-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        )
        fields = raw_status.split("\0")
        changed: list[str] = []
        field_index = 0
        while field_index < len(fields):
            entry = fields[field_index]
            field_index += 1
            if len(entry) < 4:
                continue
            status_letters, changed_path = entry[:2], entry[3:]
            changed.append(changed_path)
            if _RENAME_STATUS_LETTERS.intersection(status_letters):
                # Skip the old path of a rename or copy.
                field_index += 1
        return changed

    def _count_commits(self, path: Path, excluded_refs: list[str]) -> int:
        # HEAD's commits reachable from none of `excluded_refs` (all of them when it is empty).
        output = self._run(
            ["-C", str(path), "rev-list", "--count", "HEAD", "--not", *excluded_refs, "--"]
        )
        return int(output.strip())

    def _run_optional(self, args: list[str]) -> str | None:
        # A query whose non-zero exit means "there is none" (no upstream, detached HEAD).
        try:
            output = self._run(args).strip()
        except GitWorktreeError:
            return None
        return output or None

    def _run(self, args: list[str], error_class: type[PanError] = GitWorktreeError) -> str:
        command = [_GIT, *args]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=False)
        except OSError as error:
            raise error_class(f"failed to run {' '.join(command)}") from error

        if completed.returncode != 0:
            detail = completed.stderr.strip()
            raise error_class(f"git command exited with code {completed.returncode}: {detail}")
        return completed.stdout
