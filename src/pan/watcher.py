from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from pan.logging import initialise_logger
from pan.seams import HerdrAdapter

logger = initialise_logger(__name__)

WAKE_INSTRUCTION = (
    "A new item is in the pan inbox. Run your orchestrating drain-classify-route loop now: "
    "shell `pan inbox drain --json`, then classify and route each item per the "
    "orchestrating skill."
)


class WatchdogInboxWatcher:
    def __init__(self, herdr: HerdrAdapter, orchestrator_pane_id: str, inbox_dir: Path) -> None:
        self._herdr = herdr
        self._orchestrator_pane_id = orchestrator_pane_id
        self._inbox_dir = inbox_dir

    def on_inbox_changed(self) -> None:
        # INV-2: send a fixed wake instruction (so the orchestrator's Claude TUI actually
        # wakes — a bare Enter with an empty prompt does nothing) and then the Enter that
        # submits it. This carries NO task payload; the payload lives in the durable inbox.
        self._herdr.send_text(self._orchestrator_pane_id, WAKE_INSTRUCTION)
        self._herdr.nudge(self._orchestrator_pane_id)
        logger.info("watcher woke orchestrator")

    def start(self) -> None:  # pragma: no cover - live filesystem observer, not unit-tested
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(
            _InboxEventHandler(self.on_inbox_changed), str(self._inbox_dir), recursive=False
        )
        observer.start()
        logger.info("watcher observing inbox")
        try:
            observer.join()
        finally:
            observer.stop()
            observer.join()


class _InboxEventHandler(FileSystemEventHandler):  # pragma: no cover - exercised only live
    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._on_change()
