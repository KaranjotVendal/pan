from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from pan.config import INBOX_LOOKUP_THREAD_LIMIT, INBOX_LOOKUP_TIMEOUT_SECONDS
from pan.logging import initialise_logger
from pan.models import InboxThread

logger = initialise_logger(__name__)

_PLANNOTATOR = "plannotator"

# Bounds the walk over the listing, as in the inbox-link hook.
_MAX_SEARCH_DEPTH = 6


def _normalized_name(name: str) -> str:
    # The Inbox compares thread names without case or spacing.
    return "".join(name.split()).casefold()


def _thread_rows(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    # Every object carrying a string `thread_id`, wherever the listing nests them.
    if depth > _MAX_SEARCH_DEPTH:
        return []
    if isinstance(value, dict):
        if isinstance(value.get("thread_id"), str):
            return [value]
        return [row for nested in value.values() for row in _thread_rows(nested, depth + 1)]
    if isinstance(value, list):
        return [row for nested in value for row in _thread_rows(nested, depth + 1)]
    return []


def find_thread_in_listing(listing: Any, thread_name: str) -> InboxThread | None:
    # The first row (newest activity first) whose `thread_name` matches. A row's link is
    # taken when it carries one (`url`); the documented listing does not, so it is often None.
    wanted = _normalized_name(thread_name)
    for row in _thread_rows(listing):
        name = row.get("thread_name")
        if not isinstance(name, str) or _normalized_name(name) != wanted:
            continue
        url = row.get("url")
        return InboxThread(
            thread_id=row["thread_id"],
            name=name,
            url=url if isinstance(url, str) and url else None,
        )
    return None


class ShellPlannotatorInboxAdapter:
    # The InboxDirectory seam over `plannotator inbox call read_thread`: with no thread id it
    # lists a project's open threads (`asked_by: "anyone"`, so threads other sessions wrote
    # count too). Read only: this adapter never sends. Best effort: any failure (no Inbox,
    # a refusal, a timeout, unexpected output) is logged and returns None.
    def find_thread(self, project_path: Path, thread_name: str) -> InboxThread | None:
        arguments = {
            "asked_by": "anyone",
            "limit": INBOX_LOOKUP_THREAD_LIMIT,
            "project_path": str(project_path),
        }
        command = [_PLANNOTATOR, "inbox", "call", "read_thread", json.dumps(arguments)]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=INBOX_LOOKUP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning(f"inbox lookup could not run plannotator: {type(e).__name__}")
            return None
        if completed.returncode != 0:
            logger.warning(f"inbox lookup refused code={completed.returncode}")
            return None
        try:
            listing = json.loads(completed.stdout)
        except json.JSONDecodeError:
            logger.warning("inbox lookup produced non-JSON output")
            return None
        found = find_thread_in_listing(listing, thread_name)
        logger.info(f"inbox lookup found={found is not None}")
        return found
