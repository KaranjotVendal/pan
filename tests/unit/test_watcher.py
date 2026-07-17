from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, call

from pan.watcher import WAKE_INSTRUCTION, WatchdogInboxWatcher


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
