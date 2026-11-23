from __future__ import annotations

from pathlib import Path

from pan.models import InboxThread


class FakeInboxDirectory:
    # Finds the threads it was given, by (project folder, thread name); records each lookup.
    # `listings` are each project folder's open threads; `sessions` map a thread id to the
    # Claude session id of its newest agent message.
    def __init__(
        self,
        threads: dict[tuple[Path, str], InboxThread] | None = None,
        *,
        listings: dict[Path, list[InboxThread]] | None = None,
        sessions: dict[str, str] | None = None,
    ) -> None:
        self._threads = threads or {}
        self._listings = listings or {}
        self._sessions = sessions or {}
        self.lookups: list[tuple[Path, str]] = []
        self.listed: list[Path] = []

    def find_thread(self, project_path: Path, thread_name: str) -> InboxThread | None:
        self.lookups.append((project_path, thread_name))
        return self._threads.get((project_path, thread_name))

    def list_threads(self, project_path: Path) -> list[InboxThread]:
        self.listed.append(project_path)
        return self._listings.get(project_path, [])

    def latest_agent_session(self, thread_id: str) -> str | None:
        return self._sessions.get(thread_id)


class FakeSessionFolders:
    def __init__(self, folders: dict[str, Path] | None = None) -> None:
        self._folders = folders or {}

    def folder_of(self, session_id: str) -> Path | None:
        return self._folders.get(session_id)
