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


def _inbox_thread(row: dict[str, Any], name: str) -> InboxThread:
    # A row's link is taken when it carries one (`url`); the documented listing does not, so
    # it is often None.
    url = row.get("url")
    return InboxThread(
        thread_id=row["thread_id"],
        name=name,
        url=url if isinstance(url, str) and url else None,
    )


def threads_in_listing(listing: Any) -> list[InboxThread]:
    # Every named thread in the listing, in its order (newest activity first).
    threads: list[InboxThread] = []
    for row in _thread_rows(listing):
        name = row.get("thread_name")
        if isinstance(name, str) and name:
            threads.append(_inbox_thread(row, name))
    return threads


def find_thread_in_listing(listing: Any, thread_name: str) -> InboxThread | None:
    # The first thread (newest activity first) whose name matches.
    wanted = _normalized_name(thread_name)
    for thread in threads_in_listing(listing):
        if _normalized_name(thread.name) == wanted:
            return thread
    return None


def _messages(value: Any, depth: int = 0) -> list[dict[str, Any]]:
    # Every object carrying an `author` object, wherever the full-format thread nests them
    # (today under thread.messages).
    if depth > _MAX_SEARCH_DEPTH:
        return []
    if isinstance(value, dict):
        if isinstance(value.get("author"), dict):
            return [value]
        return [message for nested in value.values() for message in _messages(nested, depth + 1)]
    if isinstance(value, list):
        return [message for nested in value for message in _messages(nested, depth + 1)]
    return []


def latest_agent_session_in(thread: Any) -> str | None:
    # The `author.session` of the newest agent message: the one with the latest `created_at`
    # (ISO 8601, so it sorts as text). The listing runs oldest first, so a later message wins
    # a tie, and a message with no `created_at` wins over every earlier one. A person's
    # message carries no session, so it never counts.
    latest_session: str | None = None
    latest_created_at = ""
    for message in _messages(thread):
        author = message["author"]
        session = author.get("session")
        if not isinstance(session, str) or not session or author.get("kind") == "person":
            continue
        created_at = message.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            latest_session = session
        elif latest_session is None or created_at >= latest_created_at:
            latest_session, latest_created_at = session, created_at
    return latest_session


class ShellPlannotatorInboxAdapter:
    # The InboxDirectory seam over `plannotator inbox call read_thread`: with no thread id it
    # lists a project's open threads (`asked_by: "anyone"`, so threads other sessions wrote
    # count too); with a thread id and `format: "full"` it reads one thread's messages.
    # `project_path` picks the project: a git worktree maps to its main repository's
    # project, any other folder to the project rooted at it (checked live), so the session's
    # own folder finds the threads it wrote. A session's thread exists only after its first
    # Inbox send, so a lookup right after typing a task into it usually finds nothing;
    # callers look again later (check). Read only: this adapter never sends. Best effort: any
    # failure (no Inbox, a refusal, a timeout, unexpected output) is logged and yields
    # nothing.
    def find_thread(self, project_path: Path, thread_name: str) -> InboxThread | None:
        listing = self._listing(project_path)
        if listing is None:
            return None
        found = find_thread_in_listing(listing, thread_name)
        logger.info(
            f"inbox lookup found={found is not None} threads_listed={len(_thread_rows(listing))}"
        )
        return found

    def list_threads(self, project_path: Path) -> list[InboxThread]:
        listing = self._listing(project_path)
        if listing is None:
            return []
        threads = threads_in_listing(listing)
        logger.info(f"inbox listing threads={len(threads)}")
        return threads

    def latest_agent_session(self, thread_id: str) -> str | None:
        # The full format returns every message of the thread, oldest first; `limit` is not
        # passed because the full read does not apply it (checked live).
        thread = _read_thread({"thread_id": thread_id, "format": "full"})
        if thread is None:
            return None
        session = latest_agent_session_in(thread)
        logger.info(f"inbox thread read thread_id={thread_id} session_found={session is not None}")
        return session

    def _listing(self, project_path: Path) -> Any | None:
        return _read_thread(
            {
                "asked_by": "anyone",
                "limit": INBOX_LOOKUP_THREAD_LIMIT,
                "project_path": str(project_path),
            }
        )


def _read_thread(arguments: dict[str, Any]) -> Any | None:
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
        logger.warning(f"inbox read could not run plannotator: {type(e).__name__}")
        return None
    if completed.returncode != 0:
        logger.warning(f"inbox read refused code={completed.returncode}")
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        logger.warning("inbox read produced non-JSON output")
        return None
