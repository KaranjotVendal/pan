from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pan.adapters.herdr import ShellHerdrAdapter
from pan.errors import HerdrError
from pan.models import AgentStatus


class FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, responses: list[tuple[str, int]]
) -> list[list[str]]:
    calls: list[list[str]] = []
    pending = list(responses)

    def fake_run(command: list[str], **_kwargs: Any) -> FakeCompleted:
        calls.append(command)
        stdout, returncode = pending.pop(0)
        return FakeCompleted(stdout, returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _envelope(result: dict[str, Any]) -> str:
    return json.dumps({"id": "cli:test", "result": result})


def test_create_workspace_builds_command_and_parses_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_out = _envelope(
        {
            "type": "workspace_created",
            "workspace": {"workspace_id": "w42", "active_tab_id": "w42:t1"},
        }
    )
    pane_out = _envelope(
        {"type": "pane_list", "panes": [{"pane_id": "w42:p1", "tab_id": "w42:t1"}]}
    )
    calls = _install_runner(monkeypatch, [(create_out, 0), (pane_out, 0)])

    workspace_id, pane_id = ShellHerdrAdapter().create_workspace("pan-task-3", Path("/tmp/wt"))

    assert workspace_id == "w42"
    assert pane_id == "w42:p1"
    assert calls[0] == [
        "herdr",
        "workspace",
        "create",
        "--cwd",
        "/tmp/wt",
        "--label",
        "pan-task-3",
        "--no-focus",
    ]
    assert calls[1] == ["herdr", "pane", "list", "--workspace", "w42"]


def test_create_workspace_selects_pane_in_active_tab(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_out = _envelope({"workspace": {"workspace_id": "w7", "active_tab_id": "w7:t2"}})
    pane_out = _envelope(
        {
            "panes": [
                {"pane_id": "w7:p1", "tab_id": "w7:t1"},
                {"pane_id": "w7:p9", "tab_id": "w7:t2"},
            ]
        }
    )
    _install_runner(monkeypatch, [(create_out, 0), (pane_out, 0)])

    _workspace_id, pane_id = ShellHerdrAdapter().create_workspace("x", Path("/tmp/x"))

    assert pane_id == "w7:p9"


def test_create_workspace_falls_back_to_first_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No active_tab_id on the workspace -> the first listed pane is used.
    create_out = _envelope({"workspace": {"workspace_id": "w5"}})
    pane_out = _envelope({"panes": [{"pane_id": "w5:p1", "tab_id": "w5:t1"}, {"pane_id": "w5:p2"}]})
    _install_runner(monkeypatch, [(create_out, 0), (pane_out, 0)])

    _workspace_id, pane_id = ShellHerdrAdapter().create_workspace("x", Path("/tmp/x"))

    assert pane_id == "w5:p1"


@pytest.mark.parametrize(
    "method_name, argument, expected_tail",
    [
        ("nudge", None, ["pane", "send-keys", "w1:p1", "Enter"]),
        ("kill_pane", None, ["pane", "close", "w1:p1"]),
        ("send_text", "hello world", ["pane", "send-text", "w1:p1", "hello world"]),
    ],
)
def test_pane_commands_build_correct_argv(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    argument: str | None,
    expected_tail: list[str],
) -> None:
    # These subcommands print NOTHING on success; empty stdout with returncode 0
    # must not raise and must issue the correct herdr argv.
    calls = _install_runner(monkeypatch, [("", 0)])
    adapter = ShellHerdrAdapter()

    method = getattr(adapter, method_name)
    if argument is None:
        method("w1:p1")
    else:
        method("w1:p1", argument)

    assert calls[0] == ["herdr", *expected_tail]


def test_nonzero_exit_raises_herdr_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A no-output command still fails hard on a non-zero returncode.
    _install_runner(monkeypatch, [("", 3)])

    with pytest.raises(HerdrError):
        ShellHerdrAdapter().nudge("w1:p1")


def test_non_json_output_raises_herdr_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # JSON-returning commands (create_workspace) still validate the envelope.
    _install_runner(monkeypatch, [("not json at all", 0)])

    with pytest.raises(HerdrError):
        ShellHerdrAdapter().create_workspace("x", Path("/tmp/x"))


def test_missing_herdr_binary_raises_herdr_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_oserror(command: list[str], **_kwargs: Any) -> FakeCompleted:
        raise FileNotFoundError("herdr not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(HerdrError):
        ShellHerdrAdapter().nudge("w1:p1")


def test_create_workspace_with_no_panes_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    create_out = _envelope({"workspace": {"workspace_id": "w1", "active_tab_id": "w1:t1"}})
    pane_out = _envelope({"panes": []})
    _install_runner(monkeypatch, [(create_out, 0), (pane_out, 0)])

    with pytest.raises(HerdrError):
        ShellHerdrAdapter().create_workspace("x", Path("/tmp/x"))


def test_list_workspaces_builds_commands_and_parses_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `workspace list` enumerates the live workspaces; a `pane list` per workspace
    # resolves each pane's id / cwd / agent status. Every pane becomes a LiveSession
    # and the vendor agent-status string maps into AgentStatus (an unrecognized string
    # absorbs to UNKNOWN so an unexpected value never crashes the adapter).
    list_out = _envelope({"workspaces": [{"workspace_id": "1", "label": "pan-backend"}]})
    pane_out = _envelope(
        {
            "panes": [
                {"pane_id": "1-1", "tab_id": "1:1", "cwd": "/tmp/wt", "agent_status": "working"},
                {"pane_id": "1-2", "tab_id": "1:1", "cwd": "/tmp/wt", "agent_status": "mystery"},
            ]
        }
    )
    calls = _install_runner(monkeypatch, [(list_out, 0), (pane_out, 0)])

    sessions = ShellHerdrAdapter().list_workspaces()

    assert calls[0] == ["herdr", "workspace", "list"]
    assert calls[1] == ["herdr", "pane", "list", "--workspace", "1"]
    assert [session.pane_id for session in sessions] == ["1-1", "1-2"]
    first_session = sessions[0]
    assert first_session.workspace_name == "pan-backend"
    assert first_session.workspace_id == "1"
    assert first_session.cwd == Path("/tmp/wt")
    assert first_session.agent_status is AgentStatus.WORKING
    # An unrecognized vendor status absorbs to UNKNOWN, never a crash.
    assert sessions[1].agent_status is AgentStatus.UNKNOWN


def test_list_workspaces_reads_cwd_from_workspace_when_pane_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_out = _envelope(
        {"workspaces": [{"workspace_id": "2", "label": "pan-ui", "cwd": "/tmp/ui"}]}
    )
    pane_out = _envelope({"panes": [{"pane_id": "2-1", "agent_status": "idle"}]})
    _install_runner(monkeypatch, [(list_out, 0), (pane_out, 0)])

    sessions = ShellHerdrAdapter().list_workspaces()

    assert sessions[0].cwd == Path("/tmp/ui")
    assert sessions[0].agent_status is AgentStatus.IDLE


def test_list_workspaces_defaults_missing_agent_status_to_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_out = _envelope({"workspaces": [{"workspace_id": "3", "label": "pan-x"}]})
    pane_out = _envelope({"panes": [{"pane_id": "3-1", "cwd": "/tmp/x"}]})
    _install_runner(monkeypatch, [(list_out, 0), (pane_out, 0)])

    sessions = ShellHerdrAdapter().list_workspaces()

    assert sessions[0].agent_status is AgentStatus.UNKNOWN


def test_list_workspaces_nonzero_exit_raises_herdr_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runner(monkeypatch, [("", 5)])

    with pytest.raises(HerdrError):
        ShellHerdrAdapter().list_workspaces()


def test_list_workspaces_missing_workspaces_array_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A malformed envelope with no workspaces array is a hard herdr failure.
    _install_runner(monkeypatch, [(_envelope({"workspaces": "not-a-list"}), 0)])

    with pytest.raises(HerdrError):
        ShellHerdrAdapter().list_workspaces()


def test_list_workspaces_drops_pane_with_no_resolvable_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A pane carrying neither its own cwd nor a workspace cwd cannot be a LiveSession
    # (cwd is required for the pan-ownership join), so it is silently omitted.
    list_out = _envelope({"workspaces": [{"workspace_id": "4", "label": "pan-y"}]})
    pane_out = _envelope({"panes": [{"pane_id": "4-1", "agent_status": "idle"}]})
    _install_runner(monkeypatch, [(list_out, 0), (pane_out, 0)])

    assert ShellHerdrAdapter().list_workspaces() == []
