from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from pan.adapters.git_worktree import ShellGitWorktreeAdapter
from pan.errors import SpawnError


class FakeCompleted:
    def __init__(self, returncode: int = 0) -> None:
        self.stdout = ""
        self.stderr = ""
        self.returncode = returncode


def _install_runner(monkeypatch: pytest.MonkeyPatch, returncode: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> FakeCompleted:
        calls.append(command)
        return FakeCompleted(returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_create_worktree_builds_command_and_returns_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_runner(monkeypatch)

    worktree_path = ShellGitWorktreeAdapter().create_worktree(
        repo=Path("/repo"), branch="pan-task-3", base=Path("/base")
    )

    assert worktree_path == Path("/base/pan-task-3")
    assert calls[0] == [
        "git",
        "-C",
        "/repo",
        "worktree",
        "add",
        "/base/pan-task-3",
        "-b",
        "pan-task-3",
    ]


def test_remove_worktree_builds_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_runner(monkeypatch)

    ShellGitWorktreeAdapter().remove_worktree(Path("/base/pan-task-3"))

    assert calls[0] == [
        "git",
        "-C",
        "/base/pan-task-3",
        "worktree",
        "remove",
        "--force",
        "/base/pan-task-3",
    ]


def _create(adapter: ShellGitWorktreeAdapter) -> object:
    return adapter.create_worktree(repo=Path("/repo"), branch="pan-task-3", base=Path("/base"))


def _remove(adapter: ShellGitWorktreeAdapter) -> object:
    return adapter.remove_worktree(Path("/base/pan-task-3"))


@pytest.mark.parametrize("operation", [_create, _remove], ids=["create", "remove"])
def test_command_failure_raises_spawn_error(
    monkeypatch: pytest.MonkeyPatch,
    operation: Any,
) -> None:
    _install_runner(monkeypatch, returncode=1)

    with pytest.raises(SpawnError):
        operation(ShellGitWorktreeAdapter())


def test_missing_git_binary_raises_spawn_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_oserror(command: list[str], **_kwargs: Any) -> FakeCompleted:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(SpawnError):
        ShellGitWorktreeAdapter().remove_worktree(Path("/base/x"))


@pytest.mark.parametrize("hostile_branch", ["../escape", "/abs/path", "a/../../b"])
def test_create_worktree_rejects_branch_escaping_base(
    monkeypatch: pytest.MonkeyPatch, hostile_branch: str
) -> None:
    calls = _install_runner(monkeypatch)

    with pytest.raises(SpawnError):
        ShellGitWorktreeAdapter().create_worktree(
            repo=Path("/repo"), branch=hostile_branch, base=Path("/base")
        )

    # No git command runs for a rejected branch.
    assert calls == []
