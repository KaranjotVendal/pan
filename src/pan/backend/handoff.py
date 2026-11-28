from __future__ import annotations

from pan.backend.session_control import send_to_session
from pan.backend.status_message import publish_status, send_private_details
from pan.core.errors import (
    HandoffTargetNotFoundError,
    NotAwaitingHandoffError,
    TargetInOtherProjectError,
)
from pan.core.models import (
    Conversation,
    ProjectHandoff,
    ProjectMap,
    ReplyMode,
    StatusState,
    Terminal,
    TerminalState,
    ThreadRecord,
)
from pan.core.seams import (
    Clock,
    InboxDirectory,
    ProjectMapSource,
    Reporter,
    TerminalHost,
    ThreadMap,
)
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

# What `pan handoff` adds under the task it types into the stream: the orchestrator's own
# instructions (--note), then the thread's link.
HANDOFF_NOTE_SECTION = "\n\nFrom the {project} orchestrator: {note}"
HANDED_THREAD_LINK_LINE = "\n\nSlack thread: {thread_link}"


def handed_task_text(handoff: ProjectHandoff, note: str | None) -> str:
    # The task the person asked for, the orchestrator's note and the thread's link: the body
    # `pan handoff` sends to the stream, under the usual "from Slack" line (send_to_session).
    text = handoff.task or ""
    if note:
        text += HANDOFF_NOTE_SECTION.format(project=handoff.project, note=note)
    if handoff.thread_link:
        text += HANDED_THREAD_LINK_LINE.format(thread_link=handoff.thread_link)
    return text


def _handoff_target(
    selector: str, terminals: list[Terminal], project_map: ProjectMap, handoff: ProjectHandoff
) -> Terminal:
    # The live agent session at exactly this address or with exactly this terminal name (no
    # labels or spaces), in the thread's project: one of its streams, or its orchestrator
    # itself, even when no stream of it is left.
    wanted = selector.strip()
    target = next(
        (
            terminal
            for terminal in terminals
            if terminal.state is TerminalState.LIVE and wanted in (terminal.address, terminal.name)
        ),
        None,
    )
    if target is None or project_map.agent_session_at(target.address) is None:
        raise HandoffTargetNotFoundError(f"no live agent session at or named '{selector}'")
    if target.address == handoff.project_root:
        return target
    project = project_map.project_of(target.address)
    if project is None or project.root != handoff.project_root:
        raise TargetInOtherProjectError(f"'{selector}' is not in the {handoff.project} project")
    return target


def _keep_it_yourself(
    *,
    awaiting: ThreadRecord,
    handoff: ProjectHandoff,
    target: Terminal,
    thread_map: ThreadMap,
    reporter: Reporter,
    clock: Clock,
) -> ThreadRecord:
    # The orchestrator keeps the work: it already has the task, so nothing is typed. Its
    # waiting record (the thread's record of it) stops waiting and is tied as its own.
    record = awaiting.model_copy(
        update={
            "terminal_address": target.address,
            "handoff": ProjectHandoff(
                project=handoff.project, project_root=handoff.project_root, awaiting=False
            ),
            "updated_at": clock.now(),
        }
    )
    thread_map.tie(record)
    record = publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=record,
        state=StatusState.WORKING,
        clock=clock,
    )
    send_private_details(reporter=reporter, record=record)
    return record


def handoff_thread(
    *,
    conversation: Conversation,
    selector: str,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    inbox_directory: InboxDirectory,
    reporter: Reporter,
    clock: Clock,
    project_map_source: ProjectMapSource,
    note: str | None = None,
) -> ThreadRecord:
    # `pan handoff --to <selector>`: the project's orchestrator picked the stream for the
    # thread waiting for it. The stream gets the task exactly as a `to <name>` send gives it
    # (send_to_session: the "from Slack" line, the busy and prompt refusals, the tie, the
    # status message and the private details), with `note` and the thread's link under the
    # task; its status message then says where the work went. When the orchestrator keeps the
    # work itself nothing is typed. Refused, with nothing typed, tied or posted, when the thread
    # is not waiting (NotAwaitingHandoffError), names no live agent session
    # (HandoffTargetNotFoundError) or one in another project (TargetInOtherProjectError). A
    # busy or blocked stream is refused as a send is, and the thread keeps waiting.
    awaiting = thread_map.get(conversation)
    handoff = awaiting.handoff if awaiting is not None and awaiting.tied else None
    if awaiting is None or handoff is None or not handoff.awaiting:
        raise NotAwaitingHandoffError(f"thread {conversation.id} is not awaiting a handoff")
    terminals = terminal_host.list_terminals()
    target = _handoff_target(
        selector, terminals, project_map_source.project_map(terminals), handoff
    )
    if target.address == handoff.project_root:
        record = _keep_it_yourself(
            awaiting=awaiting,
            handoff=handoff,
            target=target,
            thread_map=thread_map,
            reporter=reporter,
            clock=clock,
        )
    else:
        record = send_to_session(
            conversation=conversation,
            selector=target.address,
            text=handed_task_text(handoff, note),
            requester=awaiting.requester,
            reply_in_slack=awaiting.reply_mode is ReplyMode.SLACK,
            terminal_host=terminal_host,
            thread_map=thread_map,
            inbox_directory=inbox_directory,
            reporter=reporter,
            clock=clock,
            label=awaiting.status_label,
            handoff=ProjectHandoff(
                project=handoff.project, project_root=handoff.project_root, awaiting=False
            ),
        )
    logger.info(
        f"handed off conversation={conversation.key} terminal={target.address} "
        f"root={handoff.project_root} typed={target.address != handoff.project_root}"
    )
    return record
