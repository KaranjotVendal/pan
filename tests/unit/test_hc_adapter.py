from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from pan.core.errors import (
    TargetBlockedError,
    TerminalGoneError,
    TerminalHostError,
    TerminalNameTakenError,
)
from pan.core.models import AgentStatus, TerminalState
from pan.data.adapters.hc import HcError, ShellHcAdapter

# Shapes captured from live `hc` 0.1.21 output (paths anonymised). `ls` reports `status`
# as a flat string and omits it for programs that report none; `get` reports it as an
# object (or null) and carries labels; `run --bg --json` returns an `ls`-shaped entry.
LS_OUTPUT = {
    "hidden_finished": 0,
    "terminals": [
        {
            "address": "t6",
            "agent": "claude-code",
            "cwd": "/repo/a",
            "state": "live",
            "status": "idle",
            "turn": 23,
            "where": "repo-a / 1",
        },
        {
            "address": "t42",
            "agent": "claude-code",
            "cwd": "/repo/b",
            "name": "my-agent",
            "state": "live",
            "status": "working",
            "turn": 0,
        },
        {"address": "t1", "agent": "zsh", "cwd": "/home", "state": "live", "turn": 0},
    ],
}
GET_OUTPUT = {
    "address": "t42",
    "argv": ["claude", "--dangerously-skip-permissions"],
    "cwd": "/repo/b",
    "exit_code": None,
    "foreground": {"cwd": "/repo/b/src", "name": "2.1.325", "pid": 21424},
    "labels": {"pan.role": "worker", "where": "repo-b / 1"},
    "name": "my-agent",
    "parent": None,
    "state": "live",
    "status": {"app": "claude-code", "msg": "Reading files", "state": "blocked"},
    "turn": 0,
}
GET_SHELL_OUTPUT = {
    "address": "t1",
    "cwd": "/home",
    "foreground": {"argv": ["/bin/zsh", "-l"], "cwd": "/home", "name": "zsh", "pid": 2452},
    "labels": {},
    "name": None,
    "state": "finished",
    "status": None,
}
RUN_OUTPUT = {
    "address": "t9",
    "agent": "claude",
    "cwd": "/wt/pan-abc",
    "name": "pan-abc",
    "state": "live",
    "turn": 0,
}


class FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, responses: list[FakeCompleted]
) -> list[list[str]]:
    calls: list[list[str]] = []
    pending = list(responses)

    def fake_run(command: list[str], **_kwargs: Any) -> FakeCompleted:
        calls.append(command)
        return pending.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _ok(payload: dict[str, Any]) -> FakeCompleted:
    return FakeCompleted(stdout=json.dumps(payload))


def _hc_error(kind: str, returncode: int) -> FakeCompleted:
    stderr = json.dumps({"error": {"kind": kind, "message": "nope", "hint": "hint"}})
    return FakeCompleted(returncode=returncode, stderr=stderr)


def test_run_builds_argv_and_parses_the_new_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_runner(monkeypatch, [_ok(RUN_OUTPUT)])

    terminal = ShellHcAdapter().run(
        name="pan-abc",
        cwd=Path("/wt/pan-abc"),
        argv=["claude", "do the thing"],
        labels={"pan.role": "worker", "pan.thread": "1700000000.000100"},
    )

    assert calls[0] == [
        "hc",
        "run",
        "--bg",
        "--json",
        "--no-parent",
        "--name",
        "pan-abc",
        "--cwd",
        "/wt/pan-abc",
        "--label",
        "pan.role=worker",
        "--label",
        "pan.thread=1700000000.000100",
        "--",
        "claude",
        "do the thing",
    ]
    assert terminal.address == "t9"
    assert terminal.name == "pan-abc"
    assert terminal.state is TerminalState.LIVE
    assert terminal.agent_status is AgentStatus.NONE


def test_list_terminals_parses_every_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_runner(monkeypatch, [_ok(LS_OUTPUT)])

    terminals = ShellHcAdapter().list_terminals()

    assert calls[0] == ["hc", "ls", "--json"]
    assert [terminal.address for terminal in terminals] == ["t6", "t42", "t1"]
    first, second, shell = terminals
    assert first.where == "repo-a / 1"
    assert first.name is None
    assert first.agent == "claude-code"
    assert first.agent_status is AgentStatus.IDLE
    assert second.name == "my-agent"
    assert second.cwd == Path("/repo/b")
    assert first.turn == 23
    assert second.turn == 0
    assert shell.agent_status is AgentStatus.NONE


@pytest.mark.parametrize(
    "raw_parent, expected",
    [("t7", "t7"), ("box/t7", "box/t7"), (None, None), ("", None), (17, None)],
    ids=["local", "remote", "null", "empty", "not-a-string"],
)
def test_list_terminals_parses_the_parent(
    monkeypatch: pytest.MonkeyPatch, raw_parent: object, expected: str | None
) -> None:
    entry = {"address": "t48", "cwd": "/repo/c", "state": "live", "parent": raw_parent}
    _install_runner(monkeypatch, [_ok({"terminals": [entry]})])

    (terminal,) = ShellHcAdapter().list_terminals()

    assert terminal.parent == expected


def test_an_entry_without_a_parent_has_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(monkeypatch, [_ok(LS_OUTPUT)])

    assert [terminal.parent for terminal in ShellHcAdapter().list_terminals()] == [None] * 3


def test_list_terminals_filters_by_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_runner(monkeypatch, [_ok({"hidden_finished": 0, "terminals": []})])

    assert ShellHcAdapter().list_terminals({"pan.role": "worker"}) == []
    assert calls[0] == ["hc", "ls", "--json", "--label", "pan.role=worker"]


def test_get_parses_the_status_object_and_where_label(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_runner(monkeypatch, [_ok(GET_OUTPUT)])

    terminal = ShellHcAdapter().get("my-agent")

    assert calls[0] == ["hc", "get", "--json", "my-agent"]
    assert terminal.address == "t42"
    assert terminal.agent == "claude-code"
    assert terminal.agent_status is AgentStatus.BLOCKED
    assert terminal.where == "repo-b / 1"
    assert terminal.cwd == Path("/repo/b")


def test_get_of_a_finished_shell_has_no_agent_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(monkeypatch, [_ok(GET_SHELL_OUTPUT)])

    terminal = ShellHcAdapter().get("t1")

    assert terminal.state is TerminalState.FINISHED
    assert terminal.agent == "zsh"
    assert terminal.agent_status is AgentStatus.NONE


def test_get_of_a_fresh_claude_session_names_the_launched_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Before its first turn ends Claude Code reports no status, and its foreground name is
    # its version number, so the launched program names the agent.
    fresh_output = {**GET_OUTPUT, "status": None, "argv": ["/usr/local/bin/claude"], "turn": 0}
    _install_runner(monkeypatch, [_ok(fresh_output)])

    terminal = ShellHcAdapter().get("my-agent")

    assert terminal.agent == "claude"
    assert terminal.agent_status is AgentStatus.NONE
    assert terminal.turn == 0


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        ("idle", AgentStatus.IDLE),
        ("working", AgentStatus.WORKING),
        ("done", AgentStatus.DONE),
        ("blocked", AgentStatus.BLOCKED),
        ("error", AgentStatus.ERROR),
        ("mystery", AgentStatus.UNKNOWN),
        (17, AgentStatus.UNKNOWN),
    ],
)
def test_ls_status_strings_map_to_agent_status(
    monkeypatch: pytest.MonkeyPatch, raw_status: object, expected: AgentStatus
) -> None:
    entry = {"address": "t2", "cwd": "/x", "state": "live", "status": raw_status}
    _install_runner(monkeypatch, [_ok({"terminals": [entry]})])

    assert ShellHcAdapter().list_terminals()[0].agent_status is expected


def test_entries_without_an_address_or_cwd_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [{"cwd": "/x", "state": "live"}, {"address": "t3", "state": "live"}, "junk"]
    _install_runner(monkeypatch, [_ok({"terminals": entries})])

    assert ShellHcAdapter().list_terminals() == []


def test_send_pastes_and_submits_in_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_runner(monkeypatch, [_ok({"started": None, "status": None, "turn": 0})])

    ShellHcAdapter().send("pan-orchestrator", "wake up")

    assert calls[0] == ["hc", "send", "--json", "pan-orchestrator", "wake up"]


@pytest.mark.parametrize("complete", [True, False])
def test_read_returns_text_and_completeness(
    monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    payload = {"complete": complete, "how": "history", "text": "line one\nline two"}
    calls = _install_runner(monkeypatch, [_ok(payload)])

    terminal_read = ShellHcAdapter().read("t9", 150)

    assert calls[0] == ["hc", "read", "--json", "--lines", "150", "t9"]
    assert terminal_read.text == "line one\nline two"
    assert terminal_read.complete is complete


def test_remove_kills_then_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_runner(monkeypatch, [_ok(GET_SHELL_OUTPUT), _ok({"removed": "t1"})])

    ShellHcAdapter().remove("t1")

    assert calls == [["hc", "kill", "--json", "t1"], ["hc", "rm", "--json", "t1"]]


@pytest.mark.parametrize(
    "responses",
    [
        [_hc_error("not_found", 3)],
        [_ok(GET_SHELL_OUTPUT), _hc_error("not_found", 3)],
    ],
)
def test_remove_is_idempotent_for_a_gone_terminal(
    monkeypatch: pytest.MonkeyPatch, responses: list[FakeCompleted]
) -> None:
    _install_runner(monkeypatch, responses)

    ShellHcAdapter().remove("t1")


def test_hc_error_is_a_terminal_host_error() -> None:
    # Callers outside the adapter catch the neutral TerminalHostError, so hc's own
    # failures must reach them through it.
    assert issubclass(HcError, TerminalHostError)


@pytest.mark.parametrize(
    "kind, returncode, expected_error",
    [
        ("not_found", 3, TerminalGoneError),
        ("finished", 11, TerminalGoneError),
        ("blocked", 9, TargetBlockedError),
        ("name_taken", 1, TerminalNameTakenError),
        ("spawn_failed", 1, HcError),
    ],
)
def test_error_kinds_map_to_typed_errors(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    returncode: int,
    expected_error: type[TerminalHostError],
) -> None:
    _install_runner(monkeypatch, [_hc_error(kind, returncode)])

    with pytest.raises(expected_error) as raised:
        ShellHcAdapter().send("t9", "hello")
    assert type(raised.value) is expected_error


@pytest.mark.parametrize(
    "completed",
    [
        FakeCompleted(returncode=2, stderr="plain text failure"),
        FakeCompleted(stdout="not json"),
        FakeCompleted(stdout=json.dumps(["not", "an", "object"])),
    ],
)
def test_malformed_output_raises_hc_error(
    monkeypatch: pytest.MonkeyPatch, completed: FakeCompleted
) -> None:
    _install_runner(monkeypatch, [completed])

    with pytest.raises(HcError):
        ShellHcAdapter().list_terminals()


def test_missing_hc_binary_raises_hc_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_oserror(command: list[str], **_kwargs: Any) -> FakeCompleted:
        raise FileNotFoundError("hc not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(HcError):
        ShellHcAdapter().send("t9", "hello")
