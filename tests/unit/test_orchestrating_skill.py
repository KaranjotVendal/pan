from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

import pan
from pan.orchestrating_skill import (
    ORCHESTRATING_SKILL_PATH,
    SkillVersion,
    orchestrating_skill,
    orchestrator_boot_brief,
    wake_instruction,
)


def test_orchestrating_skill_is_the_packaged_file_with_a_stable_version() -> None:
    skill = orchestrating_skill()

    package_dir = Path(pan.__file__).resolve().parent
    assert skill.path == package_dir / "skills" / "orchestrating" / "SKILL.md"
    assert skill.path == ORCHESTRATING_SKILL_PATH
    assert skill.path.is_absolute()
    assert skill.path.read_text().startswith("---\nname: orchestrating\n")
    assert re.fullmatch(r"[0-9a-f]{12}", skill.version)
    assert orchestrating_skill() == skill


def test_orchestrating_skill_version_changes_with_the_file_content(tmp_path: Path) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("first instructions\n")
    first = orchestrating_skill(skill_file)

    skill_file.write_text("second instructions\n")
    second = orchestrating_skill(skill_file)

    assert first.path == second.path == skill_file
    assert first.version != second.version


@pytest.mark.parametrize("build_text", [orchestrator_boot_brief, wake_instruction])
def test_orchestrator_text_names_the_skill_path_and_version(
    build_text: Callable[[SkillVersion], str],
) -> None:
    skill = SkillVersion(
        path=Path("/opt/pan/skills/orchestrating/SKILL.md"), version="0123456789ab"
    )

    text = build_text(skill)

    assert "/opt/pan/skills/orchestrating/SKILL.md" in text
    assert "0123456789ab" in text


def test_wake_instruction_asks_for_a_re_read_when_the_version_changed() -> None:
    skill = SkillVersion(path=Path("/opt/pan/SKILL.md"), version="0123456789ab")

    text = wake_instruction(skill)

    assert "pan inbox drain --json" in text
    assert "if that is not the version you last read, read the file again before draining" in text


def test_orchestrating_skill_gives_no_version_for_an_unreadable_file(tmp_path: Path) -> None:
    missing_skill = tmp_path / "SKILL.md"

    assert orchestrating_skill(missing_skill) == SkillVersion(path=missing_skill, version=None)


@pytest.mark.parametrize(
    "build_text, expected_phrase",
    [
        (
            orchestrator_boot_brief,
            "Read the orchestrating skill at /opt/pan/SKILL.md and follow it",
        ),
        (wake_instruction, "pan could not read its version, so read the file again"),
    ],
)
def test_orchestrator_text_without_a_version_still_names_the_path(
    build_text: Callable[[SkillVersion], str], expected_phrase: str
) -> None:
    text = build_text(SkillVersion(path=Path("/opt/pan/SKILL.md"), version=None))

    assert expected_phrase in text
    assert "version None" not in text
