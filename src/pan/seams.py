from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pan.models import InboxItem

# The remaining seam Protocols (SlackAdapter, ThreadMap, HerdrAdapter, GitWorktreeAdapter,
# MorcliAdapter, AgentLauncher, InboxWatcher) land here as their implementing tasks arrive,
# so every seam has a single import point.


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGen(Protocol):
    def new_id(self) -> str: ...


class InboxStore(Protocol):
    def append(self, item: InboxItem) -> None: ...

    def drain(self) -> list[InboxItem]: ...
