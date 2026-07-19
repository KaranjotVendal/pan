from __future__ import annotations

from pathlib import Path

import pytest

from pan.errors import TargetAmbiguousError, TargetNotFoundError
from pan.interface import relay_to_session, resolve_target
from pan.models import AgentStatus, LiveSession


def _live(
    workspace_name: str,
    workspace_id: str,
    pane_id: str,
    agent_status: AgentStatus = AgentStatus.WORKING,
) -> LiveSession:
    return LiveSession(
        workspace_name=workspace_name,
        workspace_id=workspace_id,
        pane_id=pane_id,
        cwd=Path("/tmp/worktree"),
        agent_status=agent_status,
    )


class RecordingHerdr:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.nudged: list[str] = []

    def create_workspace(self, label: str, cwd: Path) -> tuple[str, str]:  # pragma: no cover
        raise NotImplementedError

    def nudge(self, pane_id: str) -> None:
        self.nudged.append(pane_id)

    def send_text(self, pane_id: str, text: str) -> None:
        self.sent.append((pane_id, text))

    def kill_pane(self, pane_id: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def list_workspaces(self) -> list[LiveSession]:  # pragma: no cover
        raise NotImplementedError


# Canned live set: two sessions sharing the label "sra-codex" (distinct workspace_ids /
# pane_ids), plus a uniquely-labelled one. Exercises the precise-id precedence.
def _sessions() -> list[LiveSession]:
    return [
        _live("sra-codex", "wA", "wA:p1"),
        _live("sra-codex", "wB", "wB:p1"),
        _live("sra-claude", "wC", "wC:p1"),
    ]


@pytest.mark.parametrize(
    "selector, expected_workspace_id",
    [
        ("sra-claude", "wC"),  # unique label resolves
        ("wB", "wB"),  # precise workspace_id wins even when the label is duplicated
        ("wA:p1", "wA"),  # precise pane_id resolves
    ],
)
def test_resolve_target_resolves_by_precise_id_or_unique_label(
    selector: str, expected_workspace_id: str
) -> None:
    resolved = resolve_target(selector, _sessions())

    assert resolved.workspace_id == expected_workspace_id


def test_resolve_target_zero_matches_raises_not_found() -> None:
    with pytest.raises(TargetNotFoundError):
        resolve_target("does-not-exist", _sessions())


def test_resolve_target_duplicate_label_raises_ambiguous_with_candidates() -> None:
    with pytest.raises(TargetAmbiguousError) as excinfo:
        resolve_target("sra-codex", _sessions())

    error = excinfo.value
    assert error.selector == "sra-codex"
    assert {candidate.workspace_id for candidate in error.candidates} == {"wA", "wB"}
    # The rendered message is the user-facing re-target payload (SKILL.md exit-21 route):
    # it must name each candidate's precise ids so the user can re-target.
    rendered = str(error)
    assert "wA" in rendered and "wA:p1" in rendered
    assert "wB" in rendered and "wB:p1" in rendered


def test_relay_sends_into_resolved_pane_then_nudges_and_returns_target() -> None:
    herdr = RecordingHerdr()

    resolved = relay_to_session(herdr, "sra-claude", "re-run the tests", _sessions())

    assert resolved.workspace_id == "wC"
    assert herdr.sent == [("wC:p1", "re-run the tests")]
    assert herdr.nudged == ["wC:p1"]


def test_relay_preserves_flag_looking_message_verbatim() -> None:
    herdr = RecordingHerdr()

    relay_to_session(herdr, "sra-claude", "add the --json path", _sessions())

    assert herdr.sent == [("wC:p1", "add the --json path")]


@pytest.mark.parametrize("selector", ["does-not-exist", "sra-codex"])
def test_relay_does_not_send_when_target_unresolvable(selector: str) -> None:
    herdr = RecordingHerdr()

    with pytest.raises((TargetNotFoundError, TargetAmbiguousError)):
        relay_to_session(herdr, selector, "hi", _sessions())

    assert herdr.sent == []
    assert herdr.nudged == []
