from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pan.core.errors import PanError
from pan.core.models import BranchStatus, WorktreeStatus


@dataclass
class FakeGitWorktree:
    # An in-memory GitWorktreeAdapter. `status` is what worktree_status returns; `branches`
    # maps a repository to what branch_status returns there (None when it is absent);
    # `current_branches` maps a folder to what current_branch returns (None when absent). `errors`
    # maps an operation name ("create", "status", "branch_status", "remove", "prune",
    # "delete_branch") to an error it raises. Every call but create is recorded in `calls` as a
    # tuple naming the operation and its arguments; create appends "worktree" to `timeline`, so
    # spawn tests can order it against the terminal host's steps.
    status: WorktreeStatus | None = None
    branches: dict[Path, BranchStatus] = field(default_factory=dict)
    current_branches: dict[Path, str] = field(default_factory=dict)
    errors: dict[str, PanError] = field(default_factory=dict)
    calls: list[tuple[object, ...]] = field(default_factory=list)
    timeline: list[str] = field(default_factory=list)

    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path:
        self.timeline.append("worktree")
        if "create" in self.errors:
            raise self.errors["create"]
        return base / branch

    def worktree_status(self, path: Path) -> WorktreeStatus:
        self.calls.append(("status", path))
        if "status" in self.errors:
            raise self.errors["status"]
        assert self.status is not None, "set FakeGitWorktree.status first"
        return self.status

    def current_branch(self, path: Path) -> str | None:
        self.calls.append(("current_branch", path))
        return self.current_branches.get(path)

    def branch_status(self, repo: Path, branch: str) -> BranchStatus | None:
        self.calls.append(("branch_status", repo, branch))
        if "branch_status" in self.errors:
            raise self.errors["branch_status"]
        return self.branches.get(repo)

    def remove_worktree(self, path: Path, *, force: bool) -> None:
        self.calls.append(("remove", path, force))
        if "remove" in self.errors:
            raise self.errors["remove"]

    def prune_worktrees(self, repo: Path) -> None:
        self.calls.append(("prune", repo))
        if "prune" in self.errors:
            raise self.errors["prune"]

    def delete_branch(self, repo: Path, branch: str, *, force: bool) -> None:
        self.calls.append(("delete_branch", repo, branch, force))
        if "delete_branch" in self.errors:
            raise self.errors["delete_branch"]
