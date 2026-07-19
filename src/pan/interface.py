from __future__ import annotations

from pan.errors import TargetAmbiguousError, TargetNotFoundError
from pan.logging import initialise_logger
from pan.models import LiveSession
from pan.seams import HerdrAdapter

logger = initialise_logger(__name__)


def resolve_target(selector: str, sessions: list[LiveSession]) -> LiveSession:
    # Exact-match targeting with precise-id precedence, so an ambiguous label is
    # re-targetable by a precise id (INV-3, no fuzzy judgment). A workspace_id match wins,
    # then a pane_id match; both are precise ids and resolve to a single session. Only the
    # label axis carries an ambiguity: zero label matches is not-found, one resolves, and
    # more than one (duplicate labels / multiple panes under one label) is refused.
    for session in sessions:
        if session.workspace_id == selector:
            return session
    for session in sessions:
        if session.pane_id == selector:
            return session

    label_matches = [session for session in sessions if session.workspace_name == selector]
    if not label_matches:
        raise TargetNotFoundError(f"no live session for selector '{selector}'")
    if len(label_matches) == 1:
        return label_matches[0]
    raise TargetAmbiguousError(selector, label_matches)


def relay_to_session(
    herdr: HerdrAdapter,
    selector: str,
    message: str,
    sessions: list[LiveSession],
) -> LiveSession:
    # Resolve the target, send the message into its pane, then nudge it (a content-free
    # Enter) so the agent picks the message up. Returns the resolved session so the caller
    # can build a precise ack. TargetNotFoundError / TargetAmbiguousError propagate before
    # any send, so an unresolvable selector never drives a pane.
    target = resolve_target(selector, sessions)
    herdr.send_text(target.pane_id, message)
    herdr.nudge(target.pane_id)
    logger.info(f"relay pane={target.pane_id} selector={selector} len={len(message)}")
    return target
