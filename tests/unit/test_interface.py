from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from hc_fakes import FakeHc, make_terminal

from pan.errors import TargetAmbiguousError, TargetBlockedError, TargetNotFoundError
from pan.interface import (
    FULL_HISTORY_NOTICE,
    INCOMPLETE_READ_NOTICE,
    read_session,
    relay_to_session,
    resolve_target,
)
from pan.models import Terminal, TerminalRead, ThreadRecord

_NOW = datetime(2026, 7, 19, 10, 0, 0, tzinfo=UTC)


# Canned live set: two terminals in the same Herdr app space "web" (distinct tabs), one
# named terminal, and one whose tab label is unique. Exercises the precise-id precedence.
def _terminals() -> list[Terminal]:
    return [
        make_terminal("t3", where="web / codex"),
        make_terminal("t4", where="web / review"),
        make_terminal("t5", name="pan-backend"),
        make_terminal("t6", where="my-app / 1"),
    ]


def _record(address: str, transcript_path: Path | None) -> ThreadRecord:
    return ThreadRecord(
        thread_ts="t-1",
        terminal_name="pan-backend",
        terminal_address=address,
        channel="C1",
        worktree_path=Path("/wt/pan-backend"),
        transcript_path=transcript_path,
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.parametrize(
    "selector, expected_address",
    [
        ("t4", "t4"),  # precise address
        ("pan-backend", "t5"),  # terminal name
        ("web / codex", "t3"),  # full Herdr app label
        ("my-app", "t6"),  # unique space label
    ],
)
def test_resolve_target_by_address_name_or_unique_label(
    selector: str, expected_address: str
) -> None:
    assert resolve_target(selector, _terminals()).address == expected_address


def test_resolve_target_zero_matches_raises_not_found() -> None:
    with pytest.raises(TargetNotFoundError):
        resolve_target("does-not-exist", _terminals())


def test_resolve_target_shared_space_raises_ambiguous_with_candidates() -> None:
    with pytest.raises(TargetAmbiguousError) as excinfo:
        resolve_target("web", _terminals())

    error = excinfo.value
    assert error.selector == "web"
    assert {candidate.address for candidate in error.candidates} == {"t3", "t4"}
    # The rendered message is the user-facing re-target payload (SKILL.md exit-21 route).
    rendered = str(error)
    assert "t3 (name=-, where=web / codex)" in rendered
    assert "t4 (name=-, where=web / review)" in rendered


@pytest.mark.parametrize("message", ["re-run the tests", "add the --json path"])
def test_relay_sends_verbatim_into_the_resolved_terminal(message: str) -> None:
    hc = FakeHc()

    resolved = relay_to_session(hc, "pan-backend", message, _terminals())

    assert resolved.address == "t5"
    assert hc.sent == [("t5", message)]


@pytest.mark.parametrize("selector", ["does-not-exist", "web"])
def test_relay_does_not_send_when_target_unresolvable(selector: str) -> None:
    hc = FakeHc()

    with pytest.raises((TargetNotFoundError, TargetAmbiguousError)):
        relay_to_session(hc, selector, "hi", _terminals())

    assert hc.timeline == []


def test_relay_into_a_blocked_target_raises_blocked() -> None:
    hc = FakeHc(errors={"send": TargetBlockedError("blocked")})

    with pytest.raises(TargetBlockedError):
        relay_to_session(hc, "t5", "yes", _terminals())


@pytest.mark.parametrize(
    "terminal_read, expected",
    [
        (TerminalRead(text="the recent text", complete=True), "the recent text"),
        (
            TerminalRead(text="just the screen", complete=False),
            f"{INCOMPLETE_READ_NOTICE}\njust the screen",
        ),
    ],
)
def test_read_recent_reads_history(terminal_read: TerminalRead, expected: str) -> None:
    hc = FakeHc(read_result=terminal_read)

    content = read_session(
        hc, [], "pan-backend", _terminals(), full=False, lines=42, full_history_lines=900
    )

    assert content == expected
    assert hc.reads == [("t5", 42)]


def test_read_full_renders_the_recorded_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "s.jsonl"
    transcript.write_text('{"type": "user", "message": {"content": "fix it"}}\n')
    hc = FakeHc()

    content = read_session(
        hc,
        [_record("t5", transcript)],
        "pan-backend",
        _terminals(),
        full=True,
        lines=42,
        full_history_lines=900,
    )

    assert content == "user: fix it"
    assert hc.reads == []


@pytest.mark.parametrize(
    "records",
    [[], [_record("t5", None)], [_record("t5", Path("/nonexistent/s.jsonl"))]],
    ids=["external", "no-transcript-recorded", "transcript-unreadable"],
)
def test_read_full_falls_back_to_labelled_screen_history(records: list[ThreadRecord]) -> None:
    hc = FakeHc(read_result=TerminalRead(text="long history", complete=True))

    content = read_session(
        hc, records, "pan-backend", _terminals(), full=True, lines=42, full_history_lines=900
    )

    assert content == f"{FULL_HISTORY_NOTICE}\nlong history"
    assert hc.reads == [("t5", 900)]


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "selector, expected_error",
    [("does-not-exist", TargetNotFoundError), ("web", TargetAmbiguousError)],
)
def test_read_does_not_touch_hc_when_target_unresolvable(
    selector: str, expected_error: type[Exception], full: bool
) -> None:
    hc = FakeHc()

    with pytest.raises(expected_error):
        read_session(hc, [], selector, _terminals(), full=full, lines=10, full_history_lines=900)

    assert hc.reads == []
