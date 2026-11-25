from __future__ import annotations

from collections.abc import Callable
from typing import NoReturn

from pan.backend.directive import asks_to_discard, names_selector
from pan.core.errors import (
    CredentialsError,
    DiscardNotRequestedError,
    NoExactMatchError,
    PanError,
    SlackPostError,
    XSourceRefusedError,
)
from pan.core.models import InboxItem, ThreadRecord, ThreadSource
from pan.core.seams import DrainedLog, ThreadMap
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# Posted when an action is refused because the message it rests on (--item) cannot be used:
# unknown, not the sender's newest in the thread, or not from the requester. No session names
# or paths, so it is safe in any thread.
UNUSABLE_ITEM_REPLY = "I couldn't act on that message; please send the request again."

# Text from X is someone else's text, so it must never steer a session that already exists:
# until the X safety gate (MOB-87) is in place, X may only start new sessions.
X_TO_ROUTE_NOTICE = "messages from X cannot use to <name>: yet; they can only start new sessions"

# Posts one line in the thread the action was asked in. It may raise CredentialsError or
# SlackPostError; a gate only logs those, since its refusal stands either way.
ThreadReply = Callable[[str], None]


def newest_drained_item(
    drained_log: DrainedLog, item_id: str | None, *, thread_ts: str, channel: str
) -> InboxItem | None:
    """The item as pan drained it, only when it was sent in this thread and channel and is the
    newest message pan drained there from the same sender: an older message cannot authorize
    what the sender's later one may have changed, while someone else's "+1" changes nothing."""
    if item_id is None:
        return None
    item = drained_log.get(item_id)
    if item is None or item.thread_ts != thread_ts or item.channel != channel:
        logger.info(f"no drained item id={item_id} in thread={thread_ts}")
        return None
    newest = drained_log.newest_in(thread_ts, channel, item.slack_user)
    if newest is None or newest.id != item.id:
        logger.info(f"drained item id={item_id} is not its sender's newest in thread={thread_ts}")
        return None
    return item


def refuse_x_source(thread_map: ThreadMap, thread_ts: str, item: InboxItem | None) -> None:
    """Raise XSourceRefusedError (exit 35) when the thread or the item came from X."""
    # TODO(MOB-87): replace with the X safety gate; until then X may only start new sessions.
    record = thread_map.get(thread_ts)
    from_x = (item is not None and item.source is ThreadSource.X) or (
        record is not None and record.source is ThreadSource.X
    )
    if from_x:
        logger.info(f"refused a thread from X thread={thread_ts}")
        raise XSourceRefusedError(X_TO_ROUTE_NOTICE)


def require_named_target(
    item: InboxItem | None,
    *,
    action: str,
    item_id: str | None,
    target: str,
    requester: str | None,
    thread_ts: str,
    reply: ThreadReply,
) -> InboxItem:
    """The item a send --to or relay (`action`) rests on, when it is usable (from `requester`, if given)
    and names `target` as a whole word or phrase; otherwise NoExactMatchError (exit 28). An
    unusable item also gets UNUSABLE_ITEM_REPLY in the thread."""
    if item is None or (requester is not None and item.slack_user != requester):
        _refuse_unusable_item(reply, thread_ts, NoExactMatchError(f"item {item_id} cannot be used"))
    if not names_selector(item.raw_text, target):
        logger.info(
            f"{action} target not in the person's drained message thread={thread_ts} item={item_id}"
        )
        raise NoExactMatchError(f"'{target}' is not named in the person's message")
    return item


def require_requesters_message(
    item: InboxItem | None, *, requester: str, thread_ts: str, reply: ThreadReply
) -> InboxItem:
    """The requester's own item a propose rests on; otherwise NoExactMatchError (exit 28) after
    UNUSABLE_ITEM_REPLY in the thread."""
    if item is None or item.slack_user != requester:
        _refuse_unusable_item(
            reply,
            thread_ts,
            NoExactMatchError(f"no message from {requester} to propose for in thread {thread_ts}"),
        )
    return item


def require_discard_request(
    item: InboxItem | None, record: ThreadRecord | None, *, thread_ts: str, reply: ThreadReply
) -> None:
    """Allow done --discard only on the thread requester's own message that is exactly
    done --discard; otherwise DiscardNotRequestedError (exit 33). An unusable item (missing, or
    not from the thread's requester) also gets UNUSABLE_ITEM_REPLY in the thread."""
    refusal = DiscardNotRequestedError(
        f"--discard refused for thread {thread_ts}: its requester did not send done --discard"
    )
    requester = record.requester if record is not None else None
    if item is None or requester is None or item.slack_user != requester:
        _refuse_unusable_item(reply, thread_ts, refusal)
    if not asks_to_discard(item.raw_text):
        raise refusal


def _refuse_unusable_item(reply: ThreadReply, thread_ts: str, error: PanError) -> NoReturn:
    # Says so in the thread, so a refusal is never silent, then raises `error`. A failed post
    # is only logged: the refusal and its exit code stand either way.
    try:
        reply(UNUSABLE_ITEM_REPLY)
    except (CredentialsError, SlackPostError) as e:
        logger.warning(f"could not post the refusal thread={thread_ts}: {type(e).__name__}")
    raise error
