from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pan.errors import GitWorktreeError
from pan.models import WorktreeStatus


@dataclass
class FakeGitWorktree:
    # An in-memory GitWorktreeAdapter. `status` is what worktree_status returns; `errors` maps
    # an operation name ("status", "remove", "delete_branch") to an error it raises. Every
    # call is recorded in `calls` as a tuple naming the operation and its arguments.
    status: WorktreeStatus | None = None
    errors: dict[str, GitWorktreeError] = field(default_factory=dict)
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def create_worktree(self, repo: Path, branch: str, base: Path) -> Path:  # pragma: no cover
        raise NotImplementedError

    def worktree_status(self, path: Path) -> WorktreeStatus:
        self.calls.append(("status", path))
        if "status" in self.errors:
            raise self.errors["status"]
        assert self.status is not None, "set FakeGitWorktree.status first"
        return self.status

    def remove_worktree(self, path: Path, *, force: bool) -> None:
        self.calls.append(("remove", path, force))
        if "remove" in self.errors:
            raise self.errors["remove"]

    def delete_branch(self, repo: Path, branch: str, *, force: bool) -> None:
        self.calls.append(("delete_branch", repo, branch, force))
        if "delete_branch" in self.errors:
            raise self.errors["delete_branch"]
