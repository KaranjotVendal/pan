from __future__ import annotations

import fcntl
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from pan.core.models import Conversation, InboxItem
from pan.utils.config import DRAINED_LOG_MAX_ITEMS, DRAINED_LOG_RETENTION
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)


def drained_log_path(threads_path: Path) -> Path:
    # Kept next to the thread map.
    return threads_path.with_name("drained.jsonl")


class FileDrainedLog:
    # The items `pan inbox drain` handed the orchestrator, one JSON line each, so a gate can
    # read what the person typed from pan's own record rather than from text the orchestrator
    # passes. Reads never fail: an unreadable file or line counts as no items. Lines in the
    # flat shape from before Conversation existed load (InboxItem) and are rewritten in the new
    # shape by the next record.
    def __init__(self, path: Path) -> None:
        self._path = path

    def _read_all(self) -> list[InboxItem]:
        try:
            lines = self._path.read_text().splitlines()
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"drained log unreadable, treating as empty: {type(e).__name__}")
            return []
        items: list[InboxItem] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                items.append(InboxItem.model_validate_json(line))
            except ValidationError:
                logger.warning("drained log skipping an unparseable line")
        return items

    def record(self, items: list[InboxItem], now: datetime) -> None:
        # Appends the items and drops those past DRAINED_LOG_RETENTION, keeping at most
        # DRAINED_LOG_MAX_ITEMS (the newest). Raises OSError when the file cannot be written.
        if not items:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(self._path.name + ".lock")
        # An exclusive lock around the read-modify-replace, so two drains never drop each
        # other's items.
        with lock_path.open("a") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                oldest_kept = now - DRAINED_LOG_RETENTION
                kept = [item for item in self._read_all() if item.received_at >= oldest_kept]
                kept = [*kept, *items][-DRAINED_LOG_MAX_ITEMS:]
                temp_path = self._path.with_name(self._path.name + ".tmp")
                temp_path.write_text("".join(f"{item.model_dump_json()}\n" for item in kept))
                temp_path.replace(self._path)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        logger.info(f"drained log recorded count={len(items)} kept={len(kept)}")

    def get(self, item_id: str) -> InboxItem | None:
        # The newest record of the item, or None.
        for item in reversed(self._read_all()):
            if item.id == item_id:
                return item
        return None

    def newest_in(self, conversation: Conversation, author: str) -> InboxItem | None:
        # The item the author sent last in the conversation (same source, id and place), of
        # those drained, or None.
        for item in reversed(self._read_all()):
            if item.conversation == conversation and item.author == author:
                return item
        return None
