from __future__ import annotations

from datetime import datetime
from typing import Protocol

# The remaining seam Protocols (SlackAdapter, InboxStore, ThreadMap, HerdrAdapter,
# GitWorktreeAdapter, MorcliAdapter, AgentLauncher, InboxWatcher) are added here in
# Task 2 once `pan.models` exists, so every seam has a single import point.


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGen(Protocol):
    def new_id(self) -> str: ...
