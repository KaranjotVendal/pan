from __future__ import annotations

from pathlib import Path

from pan.core.models import InboxThread


class FakeInboxDirectory:
    # Finds the threads it was given, by (project folder, thread name); records each lookup.
    def __init__(self, threads: dict[tuple[Path, str], InboxThread] | None = None) -> None:
        self._threads = threads or {}
        self.lookups: list[tuple[Path, str]] = []

    def find_thread(self, project_path: Path, thread_name: str) -> InboxThread | None:
        self.lookups.append((project_path, thread_name))
        return self._threads.get((project_path, thread_name))
