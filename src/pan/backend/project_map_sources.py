from __future__ import annotations

from collections.abc import Callable

from pan.backend.project_map import BuiltinProjectMapSource
from pan.core.models import ProjectMapKind
from pan.core.seams import GitWorktreeAdapter, ProjectMapSource

# Every project map pan can read, by the config value `project_map` takes (ProjectMapKind).
# This is the one place a source is chosen: frontend.wiring builds the configured one and
# injects it. Adding a source is one adapter module (a herdrmap one shells `herdrmap`, so it
# goes in data/adapters/), one ProjectMapKind member and one entry here; see
# decisions/0004-project-routing.md. Each factory takes the git reader, which a source that
# reads branches itself ignores.
PROJECT_MAP_SOURCES: dict[ProjectMapKind, Callable[[GitWorktreeAdapter], ProjectMapSource]] = {
    ProjectMapKind.BUILTIN: BuiltinProjectMapSource,
}


def build_project_map_source(kind: ProjectMapKind, *, git: GitWorktreeAdapter) -> ProjectMapSource:
    return PROJECT_MAP_SOURCES[kind](git)
