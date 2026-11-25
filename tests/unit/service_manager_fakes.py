from __future__ import annotations

from pathlib import Path

from pan.core.errors import ServiceError
from pan.core.models import LaunchdJobState


class FakeServiceManager:
    # In-memory launchd: plists by label, loaded jobs by label, and every call in order.
    def __init__(self, launch_agents_dir: Path) -> None:
        self.launch_agents_dir = launch_agents_dir
        self.plists: dict[str, str] = {}
        self.loaded: dict[str, LaunchdJobState] = {}
        self.directories: list[Path] = []
        self.calls: list[tuple[str, str]] = []
        self.fail_bootstrap_for: str | None = None

    def plist_installed(self, label: str) -> bool:
        return label in self.plists

    def write_plist(self, label: str, content: str) -> Path:
        self.calls.append(("write", label))
        self.plists[label] = content
        return self.launch_agents_dir / f"{label}.plist"

    def remove_plist(self, label: str) -> bool:
        self.calls.append(("remove", label))
        return self.plists.pop(label, None) is not None

    def ensure_directory(self, path: Path) -> None:
        self.directories.append(path)

    def job_state(self, label: str) -> LaunchdJobState | None:
        return self.loaded.get(label)

    def bootstrap(self, plist_path: Path) -> None:
        label = plist_path.name.removesuffix(".plist")
        self.calls.append(("bootstrap", label))
        if label == self.fail_bootstrap_for:
            raise ServiceError(f"launchctl bootstrap failed for {label}")
        self.loaded[label] = LaunchdJobState(pid=100, state="running")

    def bootout(self, label: str) -> None:
        self.calls.append(("bootout", label))
        self.loaded.pop(label, None)
