from __future__ import annotations

import json
import re
from pathlib import Path

from pan.config import CLAUDE_PROJECTS_DIR, SESSION_TRANSCRIPT_CWD_MAX_LINES
from pan.logging import initialise_logger

logger = initialise_logger(__name__)

# A Claude session id is a UUID. Anything else is refused before it reaches the glob, so a
# session id read from the Inbox can never widen the search or leave the projects folder.
_SESSION_ID = re.compile(r"[A-Za-z0-9-]+")


def cwd_in_transcript(transcript_path: Path, max_lines: int) -> Path | None:
    # The first `cwd` string among the transcript's first `max_lines` JSON lines. Stops at the
    # first one found; lines that are not JSON objects are skipped.
    with transcript_path.open(encoding="utf-8", errors="replace") as transcript:
        for line_number, line in enumerate(transcript):
            if line_number >= max_lines:
                return None
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                cwd = entry.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return Path(cwd)
    return None


class ClaudeTranscriptFolders:
    # The SessionFolders seam over Claude Code's transcripts: <projects root>/<project>/
    # <session id>.jsonl, whose records carry the session's `cwd`. Reads only the one
    # transcript of the asked session, and only its first lines. Best effort: a missing or
    # unreadable transcript, or one naming no folder, is logged and returns None.
    def __init__(
        self,
        projects_root: Path = CLAUDE_PROJECTS_DIR,
        max_lines: int = SESSION_TRANSCRIPT_CWD_MAX_LINES,
    ) -> None:
        self._projects_root = projects_root
        self._max_lines = max_lines

    def folder_of(self, session_id: str) -> Path | None:
        if not _SESSION_ID.fullmatch(session_id):
            logger.warning("session folder lookup refused a malformed session id")
            return None
        try:
            transcripts = sorted(
                self._projects_root.glob(f"*/{session_id}.jsonl"),
                key=lambda transcript: transcript.stat().st_mtime,
                reverse=True,
            )
        except OSError as e:
            logger.warning(f"session transcripts unreadable: {type(e).__name__}")
            return None
        if not transcripts:
            logger.info(f"session folder lookup found no transcript session={session_id}")
            return None
        if len(transcripts) > 1:
            # The same session id under several projects: the most recently written one wins.
            logger.info(
                f"session folder lookup found {len(transcripts)} transcripts session={session_id}; "
                "using the newest"
            )
        try:
            folder = cwd_in_transcript(transcripts[0], self._max_lines)
        except OSError as e:
            logger.warning(f"session transcript unreadable: {type(e).__name__}")
            return None
        logger.info(f"session folder lookup session={session_id} found={folder is not None}")
        return folder
