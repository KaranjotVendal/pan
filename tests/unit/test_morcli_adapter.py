from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from pan.adapters.morcli import ShellMorcliAdapter
from pan.errors import MorcliError
from pan.models import WorkerStatus


class FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _install_runner(
    monkeypatch: pytest.MonkeyPatch, stdout: str = "", returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> FakeCompleted:
        calls.append(command)
        return FakeCompleted(stdout, returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def _streams_json(status: str, session_id: str = "sess-1") -> str:
    return json.dumps([{"session_id": session_id, "workspace_id": "w1", "status": status}])


def test_session_status_builds_streams_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_runner(monkeypatch, stdout=_streams_json("working"))

    ShellMorcliAdapter().session_status("sess-1")

    assert calls[0] == ["morcli", "streams", "--json"]


@pytest.mark.parametrize(
    "raw_status, expected",
    [
        ("working", WorkerStatus.RUNNING),
        ("idle", WorkerStatus.RUNNING),
        ("blocked", WorkerStatus.BLOCKED),
        ("done", WorkerStatus.DONE),
        ("unknown", WorkerStatus.FAILED),
    ],
)
def test_status_mapping(
    monkeypatch: pytest.MonkeyPatch, raw_status: str, expected: WorkerStatus
) -> None:
    _install_runner(monkeypatch, stdout=_streams_json(raw_status))

    assert ShellMorcliAdapter().session_status("sess-1") is expected


def test_handle_matched_by_workspace_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(monkeypatch, stdout=_streams_json("blocked"))

    assert ShellMorcliAdapter().session_status("w1") is WorkerStatus.BLOCKED


def test_nonzero_exit_raises_morcli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(monkeypatch, returncode=1)

    with pytest.raises(MorcliError):
        ShellMorcliAdapter().session_status("sess-1")


def test_missing_morcli_binary_raises_morcli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_oserror(command: list[str], **_kwargs: Any) -> FakeCompleted:
        raise FileNotFoundError("morcli not found")

    monkeypatch.setattr(subprocess, "run", raise_oserror)

    with pytest.raises(MorcliError):
        ShellMorcliAdapter().session_status("sess-1")


def test_unknown_handle_raises_morcli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(monkeypatch, stdout=_streams_json("working", session_id="other"))

    with pytest.raises(MorcliError):
        ShellMorcliAdapter().session_status("sess-1")


def test_unrecognized_status_raises_morcli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runner(monkeypatch, stdout=_streams_json("frobnicated"))

    with pytest.raises(MorcliError):
        ShellMorcliAdapter().session_status("sess-1")


def test_non_json_output_raises_morcli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(monkeypatch, stdout="not json")

    with pytest.raises(MorcliError):
        ShellMorcliAdapter().session_status("sess-1")


def test_non_list_json_raises_morcli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_runner(monkeypatch, stdout="{}")

    with pytest.raises(MorcliError):
        ShellMorcliAdapter().session_status("sess-1")


def test_non_dict_stream_entries_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = json.dumps(["garbage", {"session_id": "sess-1", "status": "done"}])
    _install_runner(monkeypatch, stdout=stdout)

    assert ShellMorcliAdapter().session_status("sess-1") is WorkerStatus.DONE


def test_resolve_session_returns_session_id_for_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_runner(monkeypatch, stdout=_streams_json("working", session_id="sess-42"))

    resolved = ShellMorcliAdapter().resolve_session("w1")

    assert resolved == "sess-42"
    assert calls[0] == ["morcli", "streams", "--json"]


def test_resolve_session_returns_none_when_not_indexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The just-spawned-session lag: morcli has no stream for the workspace yet, so the
    # handle resolution is best-effort and returns None rather than raising.
    _install_runner(monkeypatch, stdout=_streams_json("working", session_id="sess-42"))

    assert ShellMorcliAdapter().resolve_session("unknown-workspace") is None


def test_resolve_session_nonzero_exit_raises_morcli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_runner(monkeypatch, returncode=1)

    with pytest.raises(MorcliError):
        ShellMorcliAdapter().resolve_session("w1")


def _install_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    streams_stdout: str,
    open_stdout: str,
    open_returncode: int = 0,
) -> list[list[str]]:
    # transcript makes two subprocess calls: `morcli streams --json` (handle resolution
    # via resolve_session) then `morcli open <handle>`. Dispatch on the subcommand.
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: Any) -> FakeCompleted:
        calls.append(command)
        if command[1] == "streams":
            return FakeCompleted(streams_stdout, 0)
        return FakeCompleted(open_stdout, open_returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_transcript_resolves_handle_then_opens_and_returns_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # streams maps workspace_id w1 -> session_id sess-9, so open targets session:sess-9.
    calls = _install_dispatch(
        monkeypatch, _streams_json("working", session_id="sess-9"), "TRANSCRIPT BODY"
    )

    content = ShellMorcliAdapter().transcript("w1")

    assert content == "TRANSCRIPT BODY"
    assert calls[-1] == ["morcli", "open", "session:sess-9"]


def test_transcript_falls_back_to_raw_handle_when_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No matching stream -> resolve_session returns None -> open the handle as given.
    calls = _install_dispatch(monkeypatch, json.dumps([]), "BODY")

    content = ShellMorcliAdapter().transcript("raw-uuid")

    assert content == "BODY"
    assert calls[-1] == ["morcli", "open", "session:raw-uuid"]


def test_transcript_open_failure_raises_morcli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_dispatch(monkeypatch, json.dumps([]), "", open_returncode=2)

    with pytest.raises(MorcliError):
        ShellMorcliAdapter().transcript("x")
