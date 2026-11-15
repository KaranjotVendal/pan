from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from pan.errors import HcError
from pan.logging import initialise_logger
from pan.seams import HcAdapter

logger = initialise_logger(__name__)

WAKE_INSTRUCTION = (
    "A new item is in the pan inbox. Run your orchestrating drain-classify-route loop now: "
    "shell `pan inbox drain --json`, then classify and route each item per the "
    "orchestrating skill."
)


class WatchdogInboxWatcher:
    def __init__(self, hc: HcAdapter, orchestrator_terminal: str, inbox_dir: Path) -> None:
        self._hc = hc
        self._orchestrator_terminal = orchestrator_terminal
        self._inbox_dir = inbox_dir

    def on_inbox_changed(self) -> None:
        # INV-2: send the fixed wake instruction; `hc send` pastes it and presses Enter.
        # It carries NO task payload; the payload lives in the durable inbox. A blocked or
        # missing orchestrator (or any hc failure) is logged, never raised: the item stays
        # in the inbox, the next change re-nudges, and `pan up` sends a wake, whereas a
        # crash would only make launchd restart the watcher in a loop.
        try:
            self._hc.send(self._orchestrator_terminal, WAKE_INSTRUCTION)
        except HcError as error:
            logger.warning(f"watcher could not wake orchestrator: {type(error).__name__}")
            return
        logger.info("watcher woke orchestrator")

    def start(self) -> None:
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


class _InboxEventHandler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        self._on_change()
