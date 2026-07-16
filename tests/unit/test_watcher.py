from __future__ import annotations

from pathlib import Path

import pytest

from pan.watcher import WatchdogInboxWatcher


class FakeHerdr:
    def __init__(self) -> None:
        self.nudged: list[str] = []
        self.sent: list[tuple[str, str]] = []

    def create_workspace(self, label: str, cwd: Path) -> tuple[str, str]:  # pragma: no cover
        raise NotImplementedError

    def nudge(self, pane_id: str) -> None:
        self.nudged.append(pane_id)

    def send_text(self, pane_id: str, text: str) -> None:  # pragma: no cover
        self.sent.append((pane_id, text))

    def kill_pane(self, pane_id: str) -> None:  # pragma: no cover
        raise NotImplementedError


@pytest.mark.parametrize("num_changes", [1, 2, 3])
def test_each_change_issues_one_fixed_nudge(num_changes: int) -> None:
    herdr = FakeHerdr()
    watcher = WatchdogInboxWatcher(herdr, "%orchestrator", Path("/tmp/pan-inbox"))

    for _ in range(num_changes):
        watcher.on_inbox_changed()

    assert herdr.nudged == ["%orchestrator"] * num_changes


def test_on_inbox_changed_sends_no_payload_across_the_pane() -> None:
    # INV-2: only the fixed content-free nudge crosses the pane; the payload
    # travels through the durable inbox, never send_text.
    herdr = FakeHerdr()
    watcher = WatchdogInboxWatcher(herdr, "%orchestrator", Path("/tmp/pan-inbox"))

    watcher.on_inbox_changed()

    assert herdr.sent == []
