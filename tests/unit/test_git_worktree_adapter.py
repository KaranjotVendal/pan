from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from terminal_host_fakes import FakeTerminalHost

from pan.adapters.git_worktree import ShellGitWorktreeAdapter
from pan.errors import GitWorktreeError, PanError, SpawnError, UnsavedWorkError
from pan.models import ThreadRecord, WorkerStatus
from pan.session_control import done_thread
from pan.threadmap import FileThreadMap


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


@pytest.mark.parametrize(
    "force, expected_flags",
    [(True, ["--force"]), (False, [])],
    ids=["force", "no-force"],
)
def test_remove_worktree_builds_command(
    monkeypatch: pytest.MonkeyPatch, force: bool, expected_flags: list[str]
) -> None:
    calls = _install_runner(monkeypatch)

    ShellGitWorktreeAdapter().remove_worktree(Path("/base/pan-task-3"), force=force)

    assert calls[0] == [
        "git",
        "-C",
        "/base/pan-task-3",
        "worktree",
        "remove",
        *expected_flags,
        "/base/pan-task-3",
    ]


@pytest.mark.parametrize("force, expected_flag", [(True, "-D"), (False, "-d")], ids=["D", "d"])
def test_delete_branch_builds_command(
    monkeypatch: pytest.MonkeyPatch, force: bool, expected_flag: str
) -> None:
    calls = _install_runner(monkeypatch)

    ShellGitWorktreeAdapter().delete_branch(Path("/repo"), "pan-task-3", force=force)

    assert calls[0] == ["git", "-C", "/repo", "branch", expected_flag, "pan-task-3"]


def _create(adapter: ShellGitWorktreeAdapter) -> object:
    return adapter.create_worktree(repo=Path("/repo"), branch="pan-task-3", base=Path("/base"))


def _remove(adapter: ShellGitWorktreeAdapter) -> object:
    return adapter.remove_worktree(Path("/base/pan-task-3"), force=True)


def _delete_branch(adapter: ShellGitWorktreeAdapter) -> object:
    return adapter.delete_branch(Path("/repo"), "pan-task-3", force=False)


def _status(adapter: ShellGitWorktreeAdapter) -> object:
    return adapter.worktree_status(Path("/base/pan-task-3"))


_OPERATIONS_AND_ERRORS = [
    (_create, SpawnError),
    (_remove, GitWorktreeError),
    (_delete_branch, GitWorktreeError),
    (_status, GitWorktreeError),
]
_OPERATION_IDS = ["create", "remove", "delete-branch", "status"]


@pytest.mark.parametrize("operation, expected_error", _OPERATIONS_AND_ERRORS, ids=_OPERATION_IDS)
def test_command_failure_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[ShellGitWorktreeAdapter], object],
    expected_error: type[PanError],
) -> None:
    _install_runner(monkeypatch, returncode=1)

    with pytest.raises(expected_error):
        operation(ShellGitWorktreeAdapter())


@pytest.mark.parametrize("operation, expected_error", _OPERATIONS_AND_ERRORS, ids=_OPERATION_IDS)
def test_missing_git_binary_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[ShellGitWorktreeAdapter], object],
    expected_error: type[PanError],
) -> None:
    def raise_oserror(command: list[str], **_kwargs: Any) -> FakeCompleted:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(expected_error):
        operation(ShellGitWorktreeAdapter())


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


# Real git, in temporary repositories: an `origin` bare repo, a clone of it (the main
# worktree, on main) and a pan worktree made by create_worktree, as spawn makes it.

_BRANCH = "pan-task"


@pytest.fixture
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the person's git config (hooks, signing, default branch) out of the tests.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for variable in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(variable, "Test User")
    for variable in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(variable, "test@example.com")


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    )
    return completed.stdout


def _commit_file(cwd: Path, relative_path: str, content: str) -> None:
    (cwd / relative_path).write_text(content)
    _git(cwd, "add", relative_path)
    _git(cwd, "commit", "-q", "-m", f"change {relative_path}")


@pytest.fixture
def main_repo(tmp_path: Path, isolated_git: None) -> Path:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    _commit_file(seed, "README.md", "seed\n")
    _git(seed, "push", "-q", "origin", "HEAD:main")
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    return repo


@pytest.fixture
def worktree(main_repo: Path, tmp_path: Path) -> Path:
    worktree_path = ShellGitWorktreeAdapter().create_worktree(
        main_repo, _BRANCH, tmp_path / "worktrees"
    )
    settings = worktree_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}")
    return worktree_path


def test_status_of_a_fresh_worktree(worktree: Path, main_repo: Path) -> None:
    status = ShellGitWorktreeAdapter().worktree_status(worktree)

    assert status.main_repo.resolve() == main_repo.resolve()
    assert status.is_main_worktree is False
    assert status.branch == _BRANCH
    assert status.changed_paths == [".claude/settings.json"]
    assert status.upstream is None
    assert status.commits_not_on_upstream == 0
    assert status.commits_not_in_base == 0


def test_status_lists_uncommitted_and_renamed_files(worktree: Path) -> None:
    (worktree / "README.md").write_text("edited\n")
    (worktree / "notes.txt").write_text("new\n")
    _commit_file(worktree, "old_name.txt", "to rename\n")
    _git(worktree, "mv", "old_name.txt", "new_name.txt")

    status = ShellGitWorktreeAdapter().worktree_status(worktree)

    assert sorted(status.changed_paths) == [
        ".claude/settings.json",
        "README.md",
        "new_name.txt",
        "notes.txt",
    ]


def test_status_leaves_out_ignored_files(worktree: Path) -> None:
    _commit_file(worktree, ".gitignore", "build/\n")
    (worktree / "build").mkdir()
    (worktree / "build" / "output.bin").write_text("x")

    status = ShellGitWorktreeAdapter().worktree_status(worktree)

    assert status.changed_paths == [".claude/settings.json"]


def test_status_counts_commits_without_upstream_against_the_base(worktree: Path) -> None:
    _commit_file(worktree, "feature.txt", "work\n")

    status = ShellGitWorktreeAdapter().worktree_status(worktree)

    assert status.upstream is None
    assert status.commits_not_in_base == 1


def test_status_counts_commits_against_the_upstream(worktree: Path) -> None:
    _commit_file(worktree, "feature.txt", "work\n")
    _git(worktree, "push", "-q", "-u", "origin", _BRANCH)
    adapter = ShellGitWorktreeAdapter()

    pushed = adapter.worktree_status(worktree)
    _commit_file(worktree, "more.txt", "more work\n")
    ahead = adapter.worktree_status(worktree)

    assert pushed.upstream == f"origin/{_BRANCH}"
    assert pushed.commits_not_on_upstream == 0
    assert pushed.commits_not_in_base == 1
    assert ahead.commits_not_on_upstream == 1


def test_status_counts_no_commit_once_merged_into_the_base(worktree: Path, main_repo: Path) -> None:
    _commit_file(worktree, "feature.txt", "work\n")
    _git(main_repo, "merge", "-q", "--ff-only", _BRANCH)

    status = ShellGitWorktreeAdapter().worktree_status(worktree)

    assert status.commits_not_in_base == 0


def test_status_reports_a_detached_head(worktree: Path) -> None:
    _git(worktree, "checkout", "-q", "--detach")

    status = ShellGitWorktreeAdapter().worktree_status(worktree)

    assert status.branch is None


def test_status_of_the_main_worktree(main_repo: Path) -> None:
    status = ShellGitWorktreeAdapter().worktree_status(main_repo)

    assert status.is_main_worktree is True


def test_remove_without_force_refuses_untracked_files(worktree: Path) -> None:
    with pytest.raises(GitWorktreeError):
        ShellGitWorktreeAdapter().remove_worktree(worktree, force=False)

    assert worktree.is_dir()


@pytest.mark.parametrize("leave_settings_file", [True, False], ids=["force", "clean"])
def test_remove_worktree_removes_it(worktree: Path, leave_settings_file: bool) -> None:
    if not leave_settings_file:
        (worktree / ".claude" / "settings.json").unlink()

    ShellGitWorktreeAdapter().remove_worktree(worktree, force=leave_settings_file)

    assert not worktree.exists()


def test_delete_branch_refuses_unmerged_commits_unless_forced(
    worktree: Path, main_repo: Path
) -> None:
    _commit_file(worktree, "feature.txt", "work\n")
    adapter = ShellGitWorktreeAdapter()
    adapter.remove_worktree(worktree, force=True)

    with pytest.raises(GitWorktreeError):
        adapter.delete_branch(main_repo, _BRANCH, force=False)
    adapter.delete_branch(main_repo, _BRANCH, force=True)

    assert _git(main_repo, "branch", "--list", _BRANCH) == ""


def test_delete_branch_without_force_deletes_a_pushed_branch(
    worktree: Path, main_repo: Path
) -> None:
    _commit_file(worktree, "feature.txt", "work\n")
    _git(worktree, "push", "-q", "-u", "origin", _BRANCH)
    adapter = ShellGitWorktreeAdapter()
    adapter.remove_worktree(worktree, force=True)

    adapter.delete_branch(main_repo, _BRANCH, force=False)

    assert _git(main_repo, "branch", "--list", _BRANCH) == ""


class _FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 11, 23, 10, 0, 0, tzinfo=UTC)


class _RecordingSlack:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        self.texts.append(text)

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.parametrize(
    "commit_work, discard, expected_reply",
    [
        (False, False, f"Closed {_BRANCH} and removed its worktree and branch."),
        (True, True, f"Closed {_BRANCH} and removed its worktree and branch."),
        (
            True,
            False,
            f"{_BRANCH} has 1 unpushed commit; kept it. "
            "Push or commit them, or send `@pan done --discard`.",
        ),
    ],
    ids=["clean", "discard-unmerged-commit", "keep-unmerged-commit"],
)
def test_done_thread_with_real_git(
    worktree: Path,
    main_repo: Path,
    tmp_path: Path,
    commit_work: bool,
    discard: bool,
    expected_reply: str,
) -> None:
    if commit_work:
        _commit_file(worktree, "feature.txt", "work\n")
    clock = _FixedClock()
    thread_map = FileThreadMap(tmp_path / "threads.json", clock)
    thread_map.tie(
        ThreadRecord(
            thread_ts="1700000000.000100",
            terminal_name=_BRANCH,
            channel="C0TEST",
            worktree_path=worktree,
            status=WorkerStatus.IDLE,
            created_at=clock.now(),
            updated_at=clock.now(),
        )
    )
    slack = _RecordingSlack()
    closes = discard or not commit_work

    try:
        done_thread(
            thread_ts="1700000000.000100",
            channel="C0TEST",
            discard=discard,
            worktree_base=tmp_path / "worktrees",
            thread_map=thread_map,
            terminal_host=FakeTerminalHost(),
            git=ShellGitWorktreeAdapter(),
            slack=slack,
        )
    except UnsavedWorkError:
        assert not closes

    assert slack.texts == [expected_reply]
    assert worktree.exists() is not closes
    branch_listing = _git(main_repo, "branch", "--list", _BRANCH)
    assert (branch_listing == "") is closes
