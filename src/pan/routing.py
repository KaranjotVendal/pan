from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

from pan.config import CLAUDE_AGENT_NAMES, INBOX_ROUTE_MAX_PROJECTS
from pan.errors import (
    InboxThreadAmbiguousError,
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


def _project_folders(sessions: list[Terminal]) -> list[Path]:
    # The distinct folders the live Claude sessions run in, first seen first, capped. Each is
    # one Inbox listing; plannotator maps a worktree to its main repository's project.
    folders: list[Path] = []
    for session in sessions:
        folder = _resolved(session.cwd)
        if folder not in folders:
            folders.append(folder)
    if len(folders) > INBOX_ROUTE_MAX_PROJECTS:
        logger.info(
            f"inbox routing lists {INBOX_ROUTE_MAX_PROJECTS} of {len(folders)} session folders"
        )
    return folders[:INBOX_ROUTE_MAX_PROJECTS]


def _open_threads(inbox_directory: InboxDirectory, folders: list[Path]) -> list[InboxThread]:
    # Several folders can share a project, so the same thread can be listed more than once.
    threads: dict[str, InboxThread] = {}
    for folder in folders:
        for thread in inbox_directory.list_threads(folder):
            threads.setdefault(thread.thread_id, thread)
    return list(threads.values())


def route_by_inbox_thread(
    selector: str,
    terminals: list[Terminal],
    *,
    inbox_directory: InboxDirectory,
    session_folders: SessionFolders,
) -> RoutedSession | None:
    # The live Claude session that writes the Inbox thread the selector names: the thread's
    # newest agent message gives the session id, its transcript gives the folder, and the live
    # Claude terminal in that folder is the session. None when no thread matches or a step
    # cannot be read (best effort: the caller tries the next way). Raises when the selector
    # names several threads, when several sessions run in that folder, or when none does.
    sessions = _live_claude_sessions(terminals)
    threads = matching_threads(selector, _open_threads(inbox_directory, _project_folders(sessions)))
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
    # What `sessions` shows of a terminal: its name, Herdr label, folder name and title.
    parts = [terminal.name, terminal.where, terminal.cwd.name, terminal.title]
    return " ".join(part for part in parts if part)


def route_by_words(selector: str, terminals: list[Terminal]) -> Terminal | None:
    # The one live terminal whose name, Herdr label, folder name and title hold every
    # selector word. None when none does; raises when several do.
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
) -> RoutedSession:
    # `to <selector>:` resolution, first match wins: exact address, exact terminal name,
    # exact Herdr label or its space (resolve_target), then the Inbox thread the selector
    # names, then the selector's words in what `sessions` shows. Ambiguity at any step is
    # refused with the candidates, never guessed.
    try:
        return RoutedSession(terminal=resolve_target(selector, terminals))
    except TargetNotFoundError:
        pass
    routed = route_by_inbox_thread(
        selector, terminals, inbox_directory=inbox_directory, session_folders=session_folders
    )
    if routed is not None:
        logger.info(
            f"routed by inbox thread selector={selector} terminal={routed.terminal.address}"
        )
        return routed
    terminal = route_by_words(selector, terminals)
    if terminal is not None:
        logger.info(f"routed by words selector={selector} terminal={terminal.address}")
        return RoutedSession(terminal=terminal)
    raise TargetNotFoundError(f"no live session for selector '{selector}'")
