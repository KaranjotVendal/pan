from __future__ import annotations

import subprocess
from pathlib import Path

from pan.core.errors import GitWorktreeError, PanError, SpawnError
from pan.core.models import BranchStatus, WorktreeStatus
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

_GIT = "git"
# `git status --porcelain -z` marks a renamed or copied entry with R or C; its old path
# follows as the next NUL-separated field.
_RENAME_STATUS_LETTERS = frozenset("RC")
# The remote's default branch, as `git clone` (or `git remote set-head`) records it.
_REMOTE_DEFAULT_BRANCH_REF = "refs/remotes/origin/HEAD"
_UNTRACKED_STATUS = "??"
_LOCAL_BRANCH_PREFIX = "refs/heads/"
_GONE_UPSTREAM_TRACK = "[gone]"
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
        main_repo, _main_branch = self._main_worktree(path)
        top_level = Path(self._run(["-C", str(path), "rev-parse", "--show-toplevel"]).strip())
        branch = self._run_optional(["-C", str(path), "symbolic-ref", "-q", "--short", "HEAD"])
        untracked_paths, modified_paths = self._changed_paths(path)
        detached_commits = 0
        if branch is None:
            detached_commits = self._count_commits(path, "HEAD", ["--branches", "--remotes"])
        return WorktreeStatus(
            main_repo=main_repo,
            is_main_worktree=top_level.resolve() == main_repo.resolve(),
            branch=branch,
            untracked_paths=untracked_paths,
            modified_paths=modified_paths,
            detached_commits=detached_commits,
        )

    def branch_status(self, repo: Path, branch: str) -> BranchStatus | None:
        # Only reading commands run here. `%(upstream:track)` is "[gone]" when the upstream is
        # configured but its remote branch no longer exists.
        branch_ref = f"{_LOCAL_BRANCH_PREFIX}{branch}"
        listing = self._run(
            [
                "-C",
                str(repo),
                "for-each-ref",
                "--format=%(refname)%00%(upstream:short)%00%(upstream:track)",
                branch_ref,
            ]
        )
        # The pattern also matches refs below it (refs/heads/<branch>/...), so match exactly.
        matching_rows = [
            row.split("\0") for row in listing.splitlines() if row.startswith(f"{branch_ref}\0")
        ]
        if not matching_rows:
            return None
        _refname, upstream_name, upstream_track = matching_rows[0]
        upstream = upstream_name or None
        upstream_gone = upstream is not None and upstream_track == _GONE_UPSTREAM_TRACK
        commits_not_on_upstream = 0
        if upstream is not None and not upstream_gone:
            commits_not_on_upstream = self._count_commits(repo, branch_ref, [upstream])
        _main_repo, main_branch = self._main_worktree(repo)
        base_refs = [
            ref
            for ref in (
                self._run_optional(
                    ["-C", str(repo), "symbolic-ref", "-q", _REMOTE_DEFAULT_BRANCH_REF]
                ),
                main_branch,
            )
            if ref is not None and ref != branch_ref
        ]
        return BranchStatus(
            upstream=upstream,
            upstream_gone=upstream_gone,
            commits_not_on_upstream=commits_not_on_upstream,
            commits_not_in_base=self._count_commits(repo, branch_ref, base_refs),
        )

    def remove_worktree(self, path: Path, *, force: bool) -> None:
        # Without force, git refuses a worktree holding uncommitted or untracked files
        # (ignored files never block it). The path is absolute, so running from inside the
        # worktree being removed is fine.
        force_flag = ["--force"] if force else []
        self._run(["-C", str(path), "worktree", "remove", *force_flag, str(path)])
        logger.info(f"git worktree removed path={path} force={force}")

    def prune_worktrees(self, repo: Path) -> None:
        self._run(["-C", str(repo), "worktree", "prune"])
        logger.info(f"git worktrees pruned repo={repo}")

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

    def _changed_paths(self, path: Path) -> tuple[list[str], list[str]]:
        # (untracked, modified) paths from `git status`; `??` marks an untracked file.
        raw_status = self._run(
            ["-C", str(path), "status", "--porcelain=v1", "-z", "--untracked-files=all"]
        )
        fields = raw_status.split("\0")
        untracked: list[str] = []
        modified: list[str] = []
        field_index = 0
        while field_index < len(fields):
            entry = fields[field_index]
            field_index += 1
            if len(entry) < 4:
                continue
            status_letters, changed_path = entry[:2], entry[3:]
            if status_letters == _UNTRACKED_STATUS:
                untracked.append(changed_path)
                continue
            modified.append(changed_path)
            if _RENAME_STATUS_LETTERS.intersection(status_letters):
                # Skip the old path of a rename or copy.
                field_index += 1
        return untracked, modified

    def _count_commits(self, cwd: Path, revision: str, excluded: list[str]) -> int:
        # The commits of `revision` reachable from none of `excluded` (refs, or rev-list
        # options such as --branches); all of them when `excluded` is empty.
        output = self._run(
            ["-C", str(cwd), "rev-list", "--count", revision, "--not", *excluded, "--"]
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
