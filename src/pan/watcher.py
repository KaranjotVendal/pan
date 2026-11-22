from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from watchdog.events import (
    FileCreatedEvent,
    FileMovedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from pan.config import WATCHER_DEBOUNCE_SECONDS
from pan.errors import HcError
from pan.inbox import is_inbox_entry_name
from pan.logging import initialise_logger
from pan.seams import HcAdapter

logger = initialise_logger(__name__)

WAKE_INSTRUCTION = (
    "A new item is in the pan inbox. Run your orchestrating drain-classify-route loop now: "
    "shell `pan inbox drain --json`, then classify and route each item per the "
    "orchestrating skill."
)


class Timer(Protocol):
    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


TimerFactory = Callable[[float, Callable[[], None]], Timer]


class WakeDebouncer:
    # Trailing-edge debounce: each trigger restarts the countdown, and the action runs once
    # the triggers stop for `delay_seconds`. Trailing (not leading) so an item that lands
    # just after a wake is never left waiting for a wake that was suppressed.
    def __init__(
        self,
        action: Callable[[], None],
        delay_seconds: float,
        timer_factory: TimerFactory = threading.Timer,
    ) -> None:
        self._action = action
        self._delay_seconds = delay_seconds
        self._timer_factory = timer_factory
        self._lock = threading.Lock()
        self._pending: Timer | None = None

    def trigger(self) -> None:
        with self._lock:
            if self._pending is not None:
                self._pending.cancel()
            timer = self._timer_factory(self._delay_seconds, self._action)
            timer.daemon = True
            timer.start()
            self._pending = timer

    def cancel(self) -> None:
        with self._lock:
            if self._pending is not None:
                self._pending.cancel()
                self._pending = None


class WatchdogInboxWatcher:
    def __init__(
        self,
        hc: HcAdapter,
        orchestrator_terminal: str,
        inbox_dir: Path,
        *,
        debounce_seconds: float = WATCHER_DEBOUNCE_SECONDS,
        timer_factory: TimerFactory = threading.Timer,
    ) -> None:
        self._hc = hc
        self._orchestrator_terminal = orchestrator_terminal
        self._inbox_dir = inbox_dir
        self._debouncer = WakeDebouncer(self.on_inbox_changed, debounce_seconds, timer_factory)

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
            _InboxEventHandler(self._debouncer.trigger), str(self._inbox_dir), recursive=False
        )
        observer.start()
        logger.info("watcher observing inbox")
        try:
            observer.join()
        finally:
            self._debouncer.cancel()
            observer.stop()
            observer.join()


class _InboxEventHandler(FileSystemEventHandler):
    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        # Forward only a new pending entry: the append's rename onto `<id>.json` (or a
        # create, on backends that report it so). The temp-file writes and the drain's own
        # claim, quarantine and delete events would otherwise each wake the orchestrator.
        if event.is_directory:
            return
        if isinstance(event, FileMovedEvent):
            arrived = event.dest_path
        elif isinstance(event, FileCreatedEvent):
            arrived = event.src_path
        else:
            return
        if is_inbox_entry_name(Path(os.fsdecode(arrived)).name):
            self._on_change()
