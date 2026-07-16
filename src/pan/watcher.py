from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from pan.logging import initialise_logger
from pan.seams import HerdrAdapter

logger = initialise_logger(__name__)


class WatchdogInboxWatcher:
    def __init__(self, herdr: HerdrAdapter, orchestrator_pane_id: str, inbox_dir: Path) -> None:
        self._herdr = herdr
        self._orchestrator_pane_id = orchestrator_pane_id
        self._inbox_dir = inbox_dir

    def on_inbox_changed(self) -> None:
        # Exactly one fixed, content-free nudge to the orchestrator pane. The payload
        # never crosses the pane — it lives in the durable inbox (INV-2).
        self._herdr.nudge(self._orchestrator_pane_id)
        logger.info("watcher nudged orchestrator")

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
