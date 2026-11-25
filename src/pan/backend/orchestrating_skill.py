from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel

from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# The orchestrating skill installed with the running pan code, so after
# `uv tool install <checkout> --force` the orchestrator is pointed at that checkout's copy,
# whatever folder it runs in.
ORCHESTRATING_SKILL_PATH = (
    Path(__file__).resolve().parent.parent / "skills" / "orchestrating" / "SKILL.md"
)

_VERSION_LENGTH = 12


class SkillVersion(BaseModel, frozen=True):
    path: Path
    # None when the file could not be read; the texts below then ask for a re-read.
    version: str | None


def orchestrating_skill(path: Path = ORCHESTRATING_SKILL_PATH) -> SkillVersion:
    # The version is the first 12 hex characters of the file's sha256, so any edit to the
    # skill changes it and the orchestrator knows to read the file again. An unreadable file
    # is logged and gives no version rather than raising, so a wake or boot still goes out.
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as error:
        logger.warning(
            f"could not read orchestrating skill path={path} error={type(error).__name__}"
        )
        return SkillVersion(path=path, version=None)
    return SkillVersion(path=path, version=digest[:_VERSION_LENGTH])


def orchestrator_boot_brief(skill: SkillVersion) -> str:
    # The first task given to a freshly started orchestrator. It is stateless by design (all
    # state is in the inbox and the thread map), so a fresh session after a reboot is correct.
    # It drains once on boot because a send to a booting agent can be dropped, and anything
    # that arrived while pan was down is already waiting in the inbox.
    version = f" (version {skill.version})" if skill.version is not None else ""
    return (
        f"You are the persistent pan orchestrator. Read the orchestrating skill at {skill.path}"
        f"{version} and follow it: drain the pan inbox once now, decide what each person means "
        "and act through `pan` subcommands, then do the same each time you are woken with a "
        "new-inbox-item message."
    )


def wake_instruction(skill: SkillVersion) -> str:
    # INV-2: fixed text plus the skill's path and version; never any inbox content.
    if skill.version is not None:
        reload = (
            f"Your instructions are {skill.path}, version {skill.version}; if that is not the "
            "version you last read, read the file again before draining."
        )
    else:
        reload = (
            f"Your instructions are {skill.path}; pan could not read its version, so read the "
            "file again before draining."
        )
    return (
        "A new item is in the pan inbox. Run your orchestrating loop now: shell "
        "`pan inbox drain --json`, then decide what each person means and act through `pan` "
        f"subcommands per the orchestrating skill. {reload}"
    )
