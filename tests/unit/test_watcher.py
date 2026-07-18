from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, call

import pytest

from pan.watcher import WAKE_INSTRUCTION, WatchdogInboxWatcher, _InboxEventHandler


def test_on_inbox_changed_sends_wake_instruction_then_nudges_in_order() -> None:
    # A single manager mock records call order across both seams: the fixed wake
    # instruction must be typed first, then the Enter that submits it. Without the
    # send_text the orchestrator's Claude TUI never wakes (bare Enter is a no-op).
    herdr = Mock()

    watcher = WatchdogInboxWatcher(herdr, "%orchestrator", Path("/tmp/pan-inbox"))
    watcher.on_inbox_changed()

    herdr.assert_has_calls(
        [
            call.send_text("%orchestrator", WAKE_INSTRUCTION),
            call.nudge("%orchestrator"),
        ]
    )


def test_start_schedules_handler_on_inbox_dir_and_starts_observer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A mocked Observer lets us assert the wiring without real filesystem-event timing
    # (the mock's join returns immediately instead of blocking the launchd-supervised loop).
    observer = Mock()
    monkeypatch.setattr("pan.watcher.Observer", lambda: observer)
    inbox_dir = tmp_path / "inbox"

    watcher = WatchdogInboxWatcher(Mock(), "%orchestrator", inbox_dir)
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
