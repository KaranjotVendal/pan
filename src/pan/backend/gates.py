from __future__ import annotations

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
from pan.core.events import ThreadNotice, ThreadNoticeReason
from pan.core.models import Conversation, InboxItem, ThreadRecord, ThreadSource
from pan.core.seams import DrainedLog, OpenReporter, ThreadMap
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# Text from X is someone else's text, so it must never steer a session that already exists:
# until the X safety gate (MOB-87) is in place, X may only start new sessions.
X_TO_ROUTE_NOTICE = "messages from X cannot use to <name>: yet; they can only start new sessions"


def newest_drained_item(
    drained_log: DrainedLog, item_id: str | None, conversation: Conversation
) -> InboxItem | None:
    """The item as pan drained it, only when it was sent in this conversation (same source,
    id and place) and is the newest message pan drained there from the same sender: an older
    message cannot authorize what the sender's later one may have changed, while someone
    else's "+1" changes nothing."""
    if item_id is None:
        return None
    item = drained_log.get(item_id)
    if item is None or item.conversation != conversation:
        logger.info(f"no drained item id={item_id} in conversation={conversation.key}")
        return None
    newest = drained_log.newest_in(conversation, item.author)
    if newest is None or newest.id != item.id:
        logger.info(
            f"drained item id={item_id} is not its sender's newest "
            f"in conversation={conversation.key}"
        )
        return None
    return item


def refuse_x_source(
    thread_map: ThreadMap, drained_log: DrainedLog, item_id: str | None, conversation_id: str
) -> None:
    """Raise XSourceRefusedError (exit 35) when the item pan drained as `item_id`, or a
    conversation with this id, came from X."""
    # TODO(MOB-87): replace with the X safety gate; until then X may only start new sessions.
    # Today only the --item check can fire: spawn always stores a Slack conversation, so no X
    # record exists yet. The X safety gate (MOB-87) will make spawn store the item's source.
    # The commands name a conversation by its id alone, so an X record with that id refuses:
    # the gate errs on the side of refusing. The item is read whatever conversation it is in,
    # since an X item can never pass the conversation check of a Slack command.
    item = drained_log.get(item_id) if item_id is not None else None
    x_record = thread_map.get(Conversation(source=ThreadSource.X, id=conversation_id))
    if (item is not None and item.conversation.source is ThreadSource.X) or x_record is not None:
        logger.info(f"refused a conversation from X id={conversation_id}")
        raise XSourceRefusedError(X_TO_ROUTE_NOTICE)


def require_named_target(
    item: InboxItem | None,
    *,
    action: str,
    item_id: str | None,
    target: str,
    requester: str | None,
    conversation: Conversation,
    open_reporter: OpenReporter,
) -> InboxItem:
    """The item a send --to or relay (`action`) rests on, when it is usable (from `requester`, if given)
    and names `target` as a whole word or phrase; otherwise NoExactMatchError (exit 28). An
    unusable item also gets a MESSAGE_UNUSABLE notice in the thread."""
    if item is None or (requester is not None and item.author != requester):
        _refuse_unusable_item(
            open_reporter, conversation, NoExactMatchError(f"item {item_id} cannot be used")
        )
    if not names_selector(item.raw_text, target):
        logger.info(
            f"{action} target not in the person's drained message conversation={conversation.key} item={item_id}"
        )
        raise NoExactMatchError(f"'{target}' is not named in the person's message")
    return item


def require_requesters_message(
    item: InboxItem | None,
    *,
    requester: str,
    conversation: Conversation,
    open_reporter: OpenReporter,
) -> InboxItem:
    """The requester's own item a propose rests on; otherwise NoExactMatchError (exit 28) after
    a MESSAGE_UNUSABLE notice in the thread."""
    if item is None or item.author != requester:
        _refuse_unusable_item(
            open_reporter,
            conversation,
            NoExactMatchError(
                f"no message from {requester} to propose for in thread {conversation.id}"
            ),
        )
    return item


def require_discard_request(
    item: InboxItem | None,
    record: ThreadRecord | None,
    *,
    conversation: Conversation,
    open_reporter: OpenReporter,
) -> None:
    """Allow done --discard only on the thread requester's own message that is exactly
    done --discard; otherwise DiscardNotRequestedError (exit 33). An unusable item (missing, or
    not from the thread's requester) also gets a MESSAGE_UNUSABLE notice in the thread."""
    refusal = DiscardNotRequestedError(
        f"--discard refused for thread {conversation.id}: its requester did not send done --discard"
    )
    requester = record.requester if record is not None else None
    if item is None or requester is None or item.author != requester:
        _refuse_unusable_item(open_reporter, conversation, refusal)
    if not asks_to_discard(item.raw_text):
        raise refusal


def _refuse_unusable_item(
    open_reporter: OpenReporter, conversation: Conversation, error: PanError
) -> NoReturn:
    # Says so in the thread (MESSAGE_UNUSABLE names no session or path, so it is safe in any
    # thread), so a refusal is never silent, then raises `error`. A reporter that cannot be
    # built (CredentialsError) or a failed post is only logged: the refusal and its exit code
    # stand either way.
    try:
        open_reporter().report(
            conversation, ThreadNotice(reason=ThreadNoticeReason.MESSAGE_UNUSABLE)
        )
    except (CredentialsError, SlackPostError) as e:
        logger.warning(
            f"could not post the refusal conversation={conversation.key}: {type(e).__name__}"
        )
    raise error
