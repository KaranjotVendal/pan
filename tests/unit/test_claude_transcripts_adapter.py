from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pan.adapters.claude_transcripts import ClaudeTranscriptFolders

_SESSION_ID = "0b6f3c1e-1111-4222-8333-444455556666"


def _write_transcript(
    projects_root: Path, session_id: str, lines: list[str], project_name: str = "-tmp-repo"
) -> Path:
    project = projects_root / project_name
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text("".join(f"{line}\n" for line in lines))
    return transcript


@pytest.mark.parametrize(
    "lines, expected",
    [
        (
            [
                json.dumps({"type": "summary"}),
                "not json",
                json.dumps({"type": "user", "cwd": "/tmp/repo"}),
                json.dumps({"type": "user", "cwd": "/tmp/elsewhere"}),
            ],
            Path("/tmp/repo"),
        ),
        ([json.dumps({"type": "summary"}), json.dumps(["a", "list"])], None),
        ([json.dumps({"type": "summary"})] * 3 + [json.dumps({"cwd": "/tmp/too-late"})], None),
    ],
    ids=["first-cwd", "no-cwd", "cwd-past-the-line-limit"],
)
def test_folder_of_reads_the_first_cwd(
    tmp_path: Path, lines: list[str], expected: Path | None
) -> None:
    _write_transcript(tmp_path, _SESSION_ID, lines)

    assert ClaudeTranscriptFolders(tmp_path, max_lines=3).folder_of(_SESSION_ID) == expected


@pytest.mark.parametrize(
    "session_id",
    ["11111111-2222-4333-8444-555566667777", "*", "../escape", ""],
    ids=["no-transcript", "glob-pattern", "path-escape", "empty"],
)
def test_folder_of_an_unknown_or_malformed_session_is_none(tmp_path: Path, session_id: str) -> None:
    _write_transcript(tmp_path, _SESSION_ID, [json.dumps({"cwd": "/tmp/repo"})])

    assert ClaudeTranscriptFolders(tmp_path).folder_of(session_id) is None


@pytest.mark.parametrize(
    "newer_project, expected",
    [("-tmp-first", Path("/tmp/first")), ("-tmp-second", Path("/tmp/second"))],
    ids=["first-is-newer", "second-is-newer"],
)
def test_folder_of_prefers_the_most_recently_written_transcript(
    tmp_path: Path, newer_project: str, expected: Path
) -> None:
    for project_name, folder in (("-tmp-first", "/tmp/first"), ("-tmp-second", "/tmp/second")):
        transcript = _write_transcript(
            tmp_path, _SESSION_ID, [json.dumps({"cwd": folder})], project_name
        )
        modified_at = 2_000_000_000 if project_name == newer_project else 1_000_000_000
        os.utime(transcript, (modified_at, modified_at))

    assert ClaudeTranscriptFolders(tmp_path).folder_of(_SESSION_ID) == expected
