from __future__ import annotations

from typing import TypeVar

from pan.core.errors import SlackPostError
from pan.core.events import Event, StatusChanged
from pan.core.models import Conversation, StatusState

EventType = TypeVar("EventType")


class RecordingReporter:
    # Records every event reported, in order, with its conversation. A StatusChanged naming no
    # posted status message gets a new id ("status-1", "status-2", ...), as a post would; one
    # naming a message is edited in place (None). Set `error` to make every report raise it.
    def __init__(self) -> None:
        self.reports: list[tuple[Conversation, Event]] = []
        self.error: SlackPostError | None = None
        self._next_id = 0

    def report(self, conversation: Conversation, event: Event) -> str | None:
        if self.error is not None:
            raise self.error
        self.reports.append((conversation, event))
        if not isinstance(event, StatusChanged):
            return None
        if event.status_message_id:
            return None
        self._next_id += 1
        return f"status-{self._next_id}"

    def events(self) -> list[Event]:
        return [event for _conversation, event in self.reports]

    def of_type(self, event_type: type[EventType]) -> list[EventType]:
        return [event for event in self.events() if isinstance(event, event_type)]

    def states(self) -> list[StatusState]:
        # The state of every status reported, in order.
        return [status.state for status in self.of_type(StatusChanged)]
