from __future__ import annotations

from pan.backend.sessions import joins_by_address
from pan.core.errors import TargetAmbiguousError, TargetNotFoundError
from pan.core.models import Terminal, ThreadRecord
from pan.core.seams import TerminalHost
from pan.data.transcript import render_transcript
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# The Herdr app's `where` label is "<space> / <tab>"; a selector may name just the space.
_WHERE_SEPARATOR = " / "
FULL_HISTORY_NOTICE = "(no transcript recorded for this terminal; showing its screen history)"
INCOMPLETE_READ_NOTICE = (
    "(incomplete: only the visible screen; the session is busy or waiting for a person)"
)


def _space_of(where: str | None) -> str | None:
    return where.split(_WHERE_SEPARATOR, 1)[0] if where is not None else None


def resolve_target(selector: str, terminals: list[Terminal]) -> Terminal:
    # Exact-match targeting with precise-id precedence, so an ambiguous label is
    # re-targetable by a precise one (INV-3, no fuzzy judgment). A host address wins, then
    # a terminal name (unique among live terminals); both resolve to one terminal.
    # Only the Herdr app label axis (`where`, or just its space) can be ambiguous: zero
    # matches is not-found, one resolves, and more than one is refused with the candidates.
    for terminal in terminals:
        if terminal.address == selector:
            return terminal
    for terminal in terminals:
        if terminal.name == selector:
            return terminal

    label_matches = [
        terminal
        for terminal in terminals
        if selector in (terminal.where, _space_of(terminal.where))
    ]
    if not label_matches:
        raise TargetNotFoundError(f"no live session for selector '{selector}'")
    if len(label_matches) == 1:
        return label_matches[0]
    raise TargetAmbiguousError(selector, label_matches)


def relay_to_session(
    terminal_host: TerminalHost,
    selector: str,
    message: str,
    terminals: list[Terminal],
) -> Terminal:
    # Resolve the target, then paste the message and press Enter in one terminal host send. Returns
    # the resolved terminal so the caller can build a precise ack. TargetNotFoundError /
    # TargetAmbiguousError propagate before any send; a target waiting on a person raises
    # TargetBlockedError from the host without anything being typed.
    target = resolve_target(selector, terminals)
    terminal_host.send(target.address, message)
    logger.info(f"relay terminal={target.address} selector={selector} len={len(message)}")
    return target


def _transcript_for(target: Terminal, records: list[ThreadRecord]) -> str | None:
    # The transcript of the record joined to the target by address (names agreeing).
    for record in records:
        if joins_by_address(record, target) and record.transcript_path is not None:
            try:
                return render_transcript(record.transcript_path)
            except OSError:
                logger.warning(f"transcript unreadable conversation={record.conversation.key}")
                return None
    return None


def read_session(
    terminal_host: TerminalHost,
    records: list[ThreadRecord],
    selector: str,
    terminals: list[Terminal],
    *,
    full: bool,
    lines: int,
    full_history_lines: int,
) -> str:
    # Resolve the target and return its RAW output — recent terminal history by default,
    # or with full=True the Claude transcript recorded by a pan worker's SessionStart hook
    # (falling back to long screen history, labelled as such, for any other terminal). The
    # caller (the orchestrator) summarizes; the CLI stays deterministic and returns raw
    # content. Resolution errors propagate before the terminal host is touched.
    target = resolve_target(selector, terminals)
    if full:
        transcript = _transcript_for(target, records)
        if transcript is not None:
            content = transcript
        else:
            history = terminal_host.read(target.address, full_history_lines)
            content = f"{FULL_HISTORY_NOTICE}\n{history.text}"
    else:
        recent = terminal_host.read(target.address, lines)
        content = recent.text if recent.complete else f"{INCOMPLETE_READ_NOTICE}\n{recent.text}"
    logger.info(
        f"read terminal={target.address} selector={selector} full={full} out_len={len(content)}"
    )
    return content
