from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock

import pytest
from terminal_host_fakes import FakeTerminalHost
from watchdog.events import (
    DirModifiedEvent,
    FileClosedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
)

from pan.backend.orchestrating_skill import SkillVersion, orchestrating_skill, wake_instruction
from pan.core.errors import TargetBlockedError, TerminalGoneError, TerminalHostError
from pan.data.adapters.hc import HcError
from pan.frontend.watcher import WakeDebouncer, WatchdogInboxWatcher, _InboxEventHandler
from pan.utils.config import WATCHER_DEBOUNCE_SECONDS


class FakeTimer:
    def __init__(self, interval: float, function: Callable[[], None]) -> None:
        self.interval = interval
        self.function = function
        self.daemon = False
        self.started = False
        self.cancelled = False

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True

    def fire(self) -> None:
        if not self.cancelled:
            self.function()


class FakeTimerFactory:
    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(self, interval: float, function: Callable[[], None]) -> FakeTimer:
        timer = FakeTimer(interval, function)
        self.timers.append(timer)
        return timer


def test_on_inbox_changed_sends_only_the_content_free_wake_instruction() -> None:
    # One hc send pastes the wake instruction (fixed text plus the skill's path and version)
    # and presses Enter; no message content, no separate keypress (INV-2).
    terminal_host = FakeTerminalHost()

    WatchdogInboxWatcher(
        terminal_host, "pan-orchestrator", Path("/tmp/pan-inbox")
    ).on_inbox_changed()

    assert terminal_host.sent == [("pan-orchestrator", wake_instruction(orchestrating_skill()))]


@pytest.mark.parametrize(
    "send_error",
    [
        TargetBlockedError("blocked"),
        TerminalGoneError("gone"),
        TerminalHostError("host missing"),
        HcError("hc missing"),
    ],
)
def test_on_inbox_changed_logs_and_survives_terminal_host_failures(
    send_error: TerminalHostError,
) -> None:
    # Under launchd a raise would crash-loop the watcher; the item stays in the inbox and
    # the next change (or `pan up`) wakes the orchestrator instead.
    terminal_host = FakeTerminalHost(errors={"send": send_error})

    WatchdogInboxWatcher(
        terminal_host, "pan-orchestrator", Path("/tmp/pan-inbox")
    ).on_inbox_changed()

    assert terminal_host.sent == []


def test_on_inbox_changed_sends_a_versionless_wake_when_the_skill_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing_skill = tmp_path / "SKILL.md"
    monkeypatch.setattr(
        "pan.frontend.watcher.orchestrating_skill", lambda: orchestrating_skill(missing_skill)
    )
    terminal_host = FakeTerminalHost()

    WatchdogInboxWatcher(
        terminal_host, "pan-orchestrator", Path("/tmp/pan-inbox")
    ).on_inbox_changed()

    expected = wake_instruction(SkillVersion(path=missing_skill, version=None))
    assert terminal_host.sent == [("pan-orchestrator", expected)]
    assert "pan could not read its version" in expected


def test_debouncer_runs_the_action_once_for_a_burst_of_triggers() -> None:
    # One inbox append (plus the drain it causes) fires several filesystem events within
    # milliseconds; the trailing debounce turns the burst into a single wake.
    actions: list[bool] = []
    timer_factory = FakeTimerFactory()
    debouncer = WakeDebouncer(lambda: actions.append(True), 1.0, timer_factory)

    for _ in range(5):
        debouncer.trigger()
    for timer in timer_factory.timers:
        timer.fire()

    assert actions == [True]
    *replaced, last = timer_factory.timers
    assert all(timer.cancelled for timer in replaced)
    assert last.started and last.daemon and not last.cancelled
    assert last.interval == 1.0


def test_debouncer_runs_again_for_a_trigger_after_the_quiet_period() -> None:
    actions: list[bool] = []
    timer_factory = FakeTimerFactory()
    debouncer = WakeDebouncer(lambda: actions.append(True), 1.0, timer_factory)

    debouncer.trigger()
    timer_factory.timers[0].fire()
    debouncer.trigger()
    timer_factory.timers[1].fire()

    assert actions == [True, True]


def test_debouncer_cancel_drops_a_pending_action() -> None:
    actions: list[bool] = []
    timer_factory = FakeTimerFactory()
    debouncer = WakeDebouncer(lambda: actions.append(True), 1.0, timer_factory)

    debouncer.trigger()
    debouncer.cancel()
    timer_factory.timers[0].fire()

    assert actions == []


def test_start_schedules_handler_on_inbox_dir_and_starts_observer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A mocked Observer lets us assert the wiring without real filesystem-event timing
    # (the mock's join returns immediately instead of blocking the launchd-supervised loop).
    observer = Mock()
    monkeypatch.setattr("pan.frontend.watcher.Observer", lambda: observer)
    inbox_dir = tmp_path / "inbox"

    timer_factory = FakeTimerFactory()
    terminal_host = FakeTerminalHost()
    watcher = WatchdogInboxWatcher(
        terminal_host, "pan-orchestrator", inbox_dir, timer_factory=timer_factory
    )
    watcher.start()

    # The inbox dir is created on demand, then one handler is scheduled on it
    # (non-recursive) and the observer is started.
    assert inbox_dir.is_dir()
    (handler, path), kwargs = observer.schedule.call_args
    assert isinstance(handler, _InboxEventHandler)
    assert path == str(inbox_dir)
    assert kwargs == {"recursive": False}
    observer.start.assert_called_once_with()
    # The handler feeds the debouncer, which wakes the orchestrator once after the quiet
    # period; nothing is sent while the timer is still pending.
    handler.on_any_event(
        FileMovedEvent(str(inbox_dir / ".Ev1.json.tmp"), str(inbox_dir / "Ev1.json"))
    )
    assert terminal_host.sent == []
    (timer,) = timer_factory.timers
    assert timer.interval == WATCHER_DEBOUNCE_SECONDS
    timer.fire()
    assert terminal_host.sent == [("pan-orchestrator", wake_instruction(orchestrating_skill()))]


_INBOX = "/inbox"


@pytest.mark.parametrize(
    "event, wakes",
    [
        # The atomic append: the temp file's rename onto `<id>.json` is the new entry.
        (FileMovedEvent(f"{_INBOX}/.Ev1.json.tmp", f"{_INBOX}/Ev1.json"), True),
        # Some backends report the entry as created rather than moved in.
        (FileCreatedEvent(f"{_INBOX}/Ev1.json"), True),
        # A drain un-claiming a valid entry puts it back, so it needs a drain again.
        (FileMovedEvent(f"{_INBOX}/Ev1.json.claimed", f"{_INBOX}/Ev1.json"), True),
        # Writing the temp file is not an entry yet.
        (FileCreatedEvent(f"{_INBOX}/.Ev1.json.tmp"), False),
        (FileModifiedEvent(f"{_INBOX}/.Ev1.json.tmp"), False),
        (FileClosedEvent(f"{_INBOX}/.Ev1.json.tmp"), False),
        # The orchestrator's own drain claims, quarantines and deletes entries.
        (FileMovedEvent(f"{_INBOX}/Ev1.json", f"{_INBOX}/Ev1.json.claimed"), False),
        (FileMovedEvent(f"{_INBOX}/Ev1.json.claimed", f"{_INBOX}/Ev1.json.corrupt"), False),
        (FileDeletedEvent(f"{_INBOX}/Ev1.json.claimed"), False),
        (FileDeletedEvent(f"{_INBOX}/Ev1.json"), False),
        (FileModifiedEvent(f"{_INBOX}/Ev1.json"), False),
        (DirModifiedEvent(_INBOX), False),
    ],
)
def test_inbox_event_handler_forwards_only_new_entries(event: FileSystemEvent, wakes: bool) -> None:
    received: list[bool] = []
    handler = _InboxEventHandler(lambda: received.append(True))

    handler.on_any_event(event)

    assert received == ([True] if wakes else [])
