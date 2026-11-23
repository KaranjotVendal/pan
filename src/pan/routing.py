from __future__ import annotations

import re
from collections.abc import Callable, Hashable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path

from pydantic import BaseModel

from pan.config import (
    CLAUDE_AGENT_NAMES,
    INBOX_ROUTE_LISTING_BUDGET_SECONDS,
    INBOX_ROUTE_MAX_PROJECTS,
    INBOX_ROUTE_WORKERS,
    SESSION_ACTIVITY_BUDGET_SECONDS,
)
from pan.errors import (
    InboxThreadAmbiguousError,
    PanError,
    TargetAmbiguousError,
    TargetNotFoundError,
    ThreadSessionNotRunningError,
)
from pan.interface import resolve_target
from pan.logging import initialise_logger
from pan.models import InboxThread, Terminal, TerminalState
from pan.seams import InboxDirectory, SessionFolders

logger = initialise_logger(__name__)

# A word is a run of letters and digits; `-`, `_`, `/`, `:` and spaces all separate words.
_WORD = re.compile(r"[^\W_]+")


class RoutedSession(BaseModel, frozen=True):
    # The session a `to <selector>:` reaches, and the Inbox thread it was found by (None when
    # it was found any other way).
    terminal: Terminal
    inbox_thread: InboxThread | None = None


def words_of(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def words_match(selector: str, text: str) -> bool:
    # Every word of the selector starts a word of the text, ignoring case.
    selector_words = words_of(selector)
    text_words = words_of(text)
    return bool(selector_words) and all(
        any(text_word.startswith(selector_word) for text_word in text_words)
        for selector_word in selector_words
    )


def _same_name(first: str, second: str) -> bool:
    # Inbox thread names compare without case or spacing, as the Inbox compares them.
    return "".join(first.split()).casefold() == "".join(second.split()).casefold()


def matching_threads(selector: str, threads: list[InboxThread]) -> list[InboxThread]:
    # Threads named exactly the selector; else the threads whose name holds every selector word.
    exact = [thread for thread in threads if _same_name(thread.name, selector)]
    if exact:
        return exact
    return [thread for thread in threads if words_match(selector, thread.name)]


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _live_claude_sessions(terminals: list[Terminal]) -> list[Terminal]:
    return [
        terminal
        for terminal in terminals
        if terminal.state is TerminalState.LIVE and terminal.agent in CLAUDE_AGENT_NAMES
    ]


def _gather[Key: Hashable, Result](
    call: Callable[[Key], Result], keys: list[Key], budget_seconds: float, what: str
) -> dict[Key, Result]:
    # Run `call` on every key, INBOX_ROUTE_WORKERS at a time in key order, and keep what has
    # answered within one overall time budget. A key whose call is still queued or running
    # then, or failed, is skipped with a log line; a running call keeps going in the
    # background until its own timeout.
    if not keys:
        return {}
    executor = ThreadPoolExecutor(max_workers=INBOX_ROUTE_WORKERS)
    try:
        futures: dict[Key, Future[Result]] = {key: executor.submit(call, key) for key in keys}
        wait(futures.values(), timeout=budget_seconds)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    results: dict[Key, Result] = {}
    for key, future in futures.items():
        if future.cancelled() or not future.done():
            logger.warning(f"{what} skipped, no answer within {budget_seconds}s: {key}")
            continue
        if future.exception() is not None:
            logger.warning(f"{what} failed: {key}")
            continue
        results[key] = future.result()
    return results


def activity_ms(terminal: Terminal) -> int | None:
    # When the session was last active: its last status change, else when it was created.
    if terminal.status_changed_ms is not None:
        return terminal.status_changed_ms
    return terminal.created_ms


def _with_activity(
    sessions: list[Terminal], session_details: Callable[[str], Terminal] | None
) -> list[Terminal]:
    # The sessions with their activity times, fetched (by address) for those the listing gave
    # none; a session whose details do not come back keeps none.
    missing = [session.address for session in sessions if activity_ms(session) is None]
    if session_details is None or not missing:
        return sessions
    details = _gather(
        session_details, missing, SESSION_ACTIVITY_BUDGET_SECONDS, "session activity lookup"
    )
    return [
        session.model_copy(
            update={
                "status_changed_ms": details[session.address].status_changed_ms,
                "created_ms": details[session.address].created_ms,
            }
        )
        if session.address in details
        else session
        for session in sessions
    ]


def project_folders(sessions: list[Terminal]) -> list[Path]:
    # The distinct folders the live Claude sessions run in, most recently active first (a
    # folder is as active as its most active session; with no time, last, in listing order),
    # capped. Each is one Inbox listing; plannotator maps a worktree to its main repository's
    # project. Worktrees of one repository are not merged here: telling them apart would cost
    # a git call per folder, and a thread listed twice is kept once (_open_threads).
    folder_activity: dict[Path, int] = {}
    for session in sessions:
        folder = _resolved(session.cwd)
        session_activity = activity_ms(session)
        known = folder_activity.get(folder, -1)
        folder_activity[folder] = max(
            known, session_activity if session_activity is not None else -1
        )
    folders = sorted(folder_activity, key=lambda folder: folder_activity[folder], reverse=True)
    if len(folders) > INBOX_ROUTE_MAX_PROJECTS:
        logger.info(
            f"inbox routing lists {INBOX_ROUTE_MAX_PROJECTS} of {len(folders)} session folders"
        )
    return folders[:INBOX_ROUTE_MAX_PROJECTS]


def _open_threads(inbox_directory: InboxDirectory, folders: list[Path]) -> list[InboxThread]:
    # The folders' open threads, listed in folder order within one overall time budget, so a
    # budget that runs out skips the stalest folders. Several folders can share a project, so
    # a thread listed more than once is kept once, in folder order.
    listings = _gather(
        inbox_directory.list_threads, folders, INBOX_ROUTE_LISTING_BUDGET_SECONDS, "inbox listing"
    )
    threads: dict[str, InboxThread] = {}
    for folder in folders:
        for thread in listings.get(folder, []):
            threads.setdefault(thread.thread_id, thread)
    return list(threads.values())


def route_by_inbox_thread(
    selector: str,
    terminals: list[Terminal],
    *,
    inbox_directory: InboxDirectory,
    session_folders: SessionFolders,
    session_details: Callable[[str], Terminal] | None = None,
) -> RoutedSession | None:
    # The live Claude session that writes the Inbox thread the selector names: the thread's
    # newest agent message gives the session id, its transcript gives the folder, and the live
    # Claude terminal in that folder is the session. None when no thread matches or a step
    # cannot be read (best effort: the caller tries the next way). Raises when the selector
    # names several threads, when several sessions run in that folder, or when none does;
    # route_session tries the words before it reports any of these.
    sessions = _live_claude_sessions(terminals)
    folders = project_folders(_with_activity(sessions, session_details))
    threads = matching_threads(selector, _open_threads(inbox_directory, folders))
    if not threads:
        return None
    if len(threads) > 1:
        raise InboxThreadAmbiguousError(selector, threads)
    thread = threads[0]

    session_id = inbox_directory.latest_agent_session(thread.thread_id)
    if session_id is None:
        logger.info(f"inbox routing found no agent session thread_id={thread.thread_id}")
        return None
    folder = session_folders.folder_of(session_id)
    if folder is None:
        logger.info(f"inbox routing found no folder thread_id={thread.thread_id}")
        return None

    in_folder = [session for session in sessions if _resolved(session.cwd) == _resolved(folder)]
    if not in_folder:
        raise ThreadSessionNotRunningError(thread.name)
    if len(in_folder) > 1:
        raise TargetAmbiguousError(selector, in_folder)
    return RoutedSession(terminal=in_folder[0], inbox_thread=thread)


def session_words(terminal: Terminal) -> str:
    # What `sessions` shows of a terminal: its name, Herdr label and folder name.
    parts = [terminal.name, terminal.where, terminal.cwd.name]
    return " ".join(part for part in parts if part)


def route_by_words(selector: str, terminals: list[Terminal]) -> Terminal | None:
    # The one live terminal whose name, Herdr label and folder name hold every selector word.
    # None when none does; raises when several do.
    matches = [
        terminal
        for terminal in terminals
        if terminal.state is TerminalState.LIVE and words_match(selector, session_words(terminal))
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise TargetAmbiguousError(selector, matches)
    return matches[0]


def route_session(
    selector: str,
    terminals: list[Terminal],
    *,
    inbox_directory: InboxDirectory,
    session_folders: SessionFolders,
    session_details: Callable[[str], Terminal] | None = None,
) -> RoutedSession:
    # `to <selector>:` resolution, first match wins: exact address, exact terminal name,
    # exact Herdr label or its space (resolve_target), then the Inbox thread the selector
    # names, then the selector's words in what `sessions` shows. When the thread step finds a
    # thread but cannot reach one live session for it, the words are still tried: one match
    # wins, several get their pick list, and only with none is the thread step's refusal
    # raised (it says more than a plain not-found). Ambiguity is refused, never guessed.
    # `session_details` (the terminal host's `get`) gives each session's activity time, which
    # orders the Inbox listings most recently active first.
    try:
        return RoutedSession(terminal=resolve_target(selector, terminals))
    except TargetNotFoundError:
        pass
    thread_refusal: PanError | None = None
    try:
        routed = route_by_inbox_thread(
            selector,
            terminals,
            inbox_directory=inbox_directory,
            session_folders=session_folders,
            session_details=session_details,
        )
    except (ThreadSessionNotRunningError, InboxThreadAmbiguousError, TargetAmbiguousError) as e:
        logger.info(f"inbox routing refused, trying words selector={selector}: {e}")
        routed, thread_refusal = None, e
    if routed is not None:
        logger.info(
            f"routed by inbox thread selector={selector} terminal={routed.terminal.address}"
        )
        return routed
    terminal = route_by_words(selector, terminals)
    if terminal is not None:
        logger.info(f"routed by words selector={selector} terminal={terminal.address}")
        return RoutedSession(terminal=terminal)
    if thread_refusal is not None:
        raise thread_refusal
    raise TargetNotFoundError(f"no live session for selector '{selector}'")
