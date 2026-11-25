from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from pan.core.errors import TerminalGoneError, TerminalNameTakenError
from pan.core.models import TerminalState
from pan.data.adapters.hc import ShellHcAdapter

# Opt-in only: runs the real `hc` against a PRIVATE core (its own HC_SESSION and scratch XDG
# folders), never the person's core, and only ever starts a plain program (`cat`), never an
# agent. Enable with PAN_HC_INTEGRATION=1.
pytestmark = pytest.mark.skipif(
    os.environ.get("PAN_HC_INTEGRATION") != "1" or shutil.which("hc") is None,
    reason="set PAN_HC_INTEGRATION=1 with hc on PATH to run the private-core hc test",
)

# hc keeps its socket under XDG_RUNTIME_DIR and refuses paths over ~103 bytes, so the runtime
# folder is a short mkdtemp rather than pytest's long tmp_path.
_RUNTIME_PREFIX = "pc"


def _hc_json(*args: str) -> dict:
    completed = subprocess.run(["hc", *args, "--json"], capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


@pytest.fixture
def private_core(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    runtime_dir = tempfile.mkdtemp(prefix=_RUNTIME_PREFIX)
    monkeypatch.setenv("HC_SESSION", f"pt{os.getpid() % 100000}")
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime_dir)
    for variable in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        folder = tmp_path / variable.lower()
        folder.mkdir()
        monkeypatch.setenv(variable, str(folder))
    monkeypatch.delenv("HC_TERMINAL_ID", raising=False)
    monkeypatch.delenv("HC_SOCKET", raising=False)

    status = _hc_json("server", "status")
    # Guard: a freshly started private core has no terminals. Anything else means the
    # environment reached a core that is not ours, so stop before touching it.
    if status.get("live") != 0 or status.get("finished") != 0:
        shutil.rmtree(runtime_dir, ignore_errors=True)
        pytest.fail("hc did not start a fresh private core; refusing to continue")
    try:
        yield
    finally:
        subprocess.run(["hc", "server", "stop", "--json"], capture_output=True, check=False)
        shutil.rmtree(runtime_dir, ignore_errors=True)


@pytest.mark.usefixtures("private_core")
def test_adapter_round_trip_against_a_private_core(tmp_path: Path) -> None:
    adapter = ShellHcAdapter()

    terminal = adapter.run(
        name="pan-probe",
        cwd=tmp_path,
        argv=["cat"],
        labels={"pan.role": "worker", "pan.thread": "1700000000.000100"},
    )
    assert terminal.name == "pan-probe"
    assert terminal.state is TerminalState.LIVE

    with pytest.raises(TerminalNameTakenError):
        adapter.run(name="pan-probe", cwd=tmp_path, argv=["cat"], labels={})

    assert [found.address for found in adapter.list_terminals({"pan.role": "worker"})] == [
        terminal.address
    ]
    assert adapter.list_terminals({"pan.role": "orchestrator"}) == []
    assert adapter.get("pan-probe").address == terminal.address

    adapter.send("pan-probe", "hello from pan")
    assert "hello from pan" in adapter.read(terminal.address, 20).text

    adapter.remove("pan-probe")
    adapter.remove("pan-probe")
    with pytest.raises(TerminalGoneError):
        adapter.get(terminal.address)
