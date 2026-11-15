from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from hc_fakes import FakeHc

from pan.errors import HcError, TargetBlockedError, TerminalGoneError
from pan.watcher import WAKE_INSTRUCTION, WatchdogInboxWatcher, _InboxEventHandler


def test_on_inbox_changed_sends_only_the_fixed_wake_instruction() -> None:
    # One hc send pastes the fixed instruction and presses Enter; no payload, no separate
    # keypress (INV-2).
    hc = FakeHc()

    WatchdogInboxWatcher(hc, "pan-orchestrator", Path("/tmp/pan-inbox")).on_inbox_changed()

    assert hc.sent == [("pan-orchestrator", WAKE_INSTRUCTION)]


@pytest.mark.parametrize(
    "send_error",
    [TargetBlockedError("blocked"), TerminalGoneError("gone"), HcError("hc missing")],
)
def test_on_inbox_changed_logs_and_survives_hc_failures(send_error: HcError) -> None:
    # Under launchd a raise would crash-loop the watcher; the item stays in the inbox and
    # the next change (or `pan up`) wakes the orchestrator instead.
    hc = FakeHc(errors={"send": send_error})

    WatchdogInboxWatcher(hc, "pan-orchestrator", Path("/tmp/pan-inbox")).on_inbox_changed()

    assert hc.sent == []


def test_start_schedules_handler_on_inbox_dir_and_starts_observer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A mocked Observer lets us assert the wiring without real filesystem-event timing
    # (the mock's join returns immediately instead of blocking the launchd-supervised loop).
    observer = Mock()
    monkeypatch.setattr("pan.watcher.Observer", lambda: observer)
    inbox_dir = tmp_path / "inbox"

    watcher = WatchdogInboxWatcher(Mock(), "pan-orchestrator", inbox_dir)
    watcher.start()

    # The inbox dir is created on demand, then one handler is scheduled on it
    # (non-recursive) and the observer is started.
    assert inbox_dir.is_dir()
    (handler, path), kwargs = observer.schedule.call_args
    assert isinstance(handler, _InboxEventHandler)
    assert path == str(inbox_dir)
    assert kwargs == {"recursive": False}
    observer.start.assert_called_once_with()


def test_inbox_event_handler_forwards_every_event_to_the_callback() -> None:
    received: list[bool] = []
    handler = _InboxEventHandler(lambda: received.append(True))

    handler.on_any_event(Mock())

    assert received == [True]
