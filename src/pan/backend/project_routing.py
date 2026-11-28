from __future__ import annotations

from collections.abc import Callable

from pan.backend.delivery import deliver_when_ready, session_name
from pan.backend.sessions import joins_terminal
from pan.backend.spawn import upgraded_reply_mode
from pan.backend.status_message import publish_status
from pan.core.errors import (
    CredentialsError,
    NoExactMatchError,
    NotAwaitingHandoffError,
    SlackPostError,
    TerminalGoneError,
)
from pan.core.events import SessionNotice, SessionNoticeReason, ThreadNotice, ThreadNoticeReason
from pan.core.models import (
    AgentStatus,
    Conversation,
    MappedProject,
    ProjectHandoff,
    ProjectMap,
    ReplyMode,
    StatusState,
    Terminal,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
)
from pan.core.seams import (
    Clock,
    OpenReporter,
    ProjectMapSource,
    Reporter,
    TerminalHost,
    ThreadMap,
)
from pan.utils.config import WORKER_BRIEF_TASK_MARKER
from pan.utils.logging import initialise_logger
from pan.utils.text import status_label

logger = initialise_logger(__name__)

# What pan types into a project's orchestrator: the task, the thread's link and the command
# that ties the thread to the stream the orchestrator picks, filled in but for the stream. Kept
# short and plain, since the orchestrator may be any agent (Claude, pi).
HANDOFF_HEADER = (
    "From Slack via pan, for the {project} project. Pick the stream that should do this task "
    "(or start one), then run: pan handoff --thread {thread} --channel {channel} --to <stream> "
    "(<stream> is that stream's name or address; add --note '<text>' for instructions of your "
    "own). pan passes the task and the thread to that stream. Don't do the task yourself "
    "unless you hand it to yourself (--to <your own name>). Until then nothing you write "
    "reaches Slack."
)
HANDOFF_THREAD_LINK_LINE = "\nSlack thread: {thread_link}"
# Looks up the thread's Slack link only once a project is found (one Slack call).
ThreadLinkLookup = Callable[[], str | None]
# hc reports these for an agent whose turn has ended.
_TURN_ENDED_STATUSES = frozenset({AgentStatus.IDLE, AgentStatus.DONE})


def handoff_text(
    conversation: Conversation, project: str, task: str, thread_link: str | None
) -> str:
    header = HANDOFF_HEADER.format(
        project=project, thread=conversation.id, channel=conversation.slack_channel()
    )
    if thread_link:
        header += HANDOFF_THREAD_LINK_LINE.format(thread_link=thread_link)
    return f"{header}{WORKER_BRIEF_TASK_MARKER}{task}"


def find_project_orchestrator(
    selector: str, terminals: list[Terminal], project_map: ProjectMap
) -> tuple[Terminal, MappedProject]:
    # The live orchestrator of the one project the selector names exactly: the project's label
    # (any letter case, as names_selector checks it) or its orchestrator's address or name.
    # None, or several projects sharing the label, is NoExactMatchError: pan proposes instead.
    by_address = {terminal.address: terminal for terminal in terminals}
    wanted = selector.strip()
    matches = [
        (by_address[project.root], project)
        for project in project_map.projects
        if project.root in by_address
        and (
            project.label.casefold() == wanted.casefold()
            or wanted in (project.root, by_address[project.root].name)
        )
    ]
    if len(matches) != 1:
        logger.info(f"no exact project match selector={selector} matches={len(matches)}")
        raise NoExactMatchError(f"no project matches '{selector}' exactly")
    return matches[0]


def hand_to_project(
    *,
    conversation: Conversation,
    selector: str,
    text: str,
    requester: str | None,
    reply_in_slack: bool,
    thread_link: ThreadLinkLookup,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    reporter: Reporter,
    clock: Clock,
    project_map_source: ProjectMapSource,
    retry_choice: str | None = None,
) -> ThreadRecord:
    # `pan send --project <selector>`: type the task, with the filled-in handoff command, into
    # the orchestrator of the project the selector names exactly, and tie the thread to the
    # project (not to that session) until the orchestrator runs `pan handoff`. Busy and
    # prompt refusals are as for any send. The thread keeps its one status message, which now
    # says it was handed to the project. The thread's link is looked up only once the project
    # is found. A record the thread already keeps for that orchestrator (pan's own worker, say)
    # is kept, with its hooks' fields, and only marked as waiting.
    terminals = terminal_host.list_terminals()
    orchestrator, project = find_project_orchestrator(
        selector, terminals, project_map_source.project_map(terminals)
    )
    link = thread_link()
    handoff = ProjectHandoff(
        project=project.label,
        project_root=project.root,
        thread_link=link,
        orchestrator_turn=orchestrator.turn,
        task=text,
    )
    deliver_when_ready(
        terminal_host,
        orchestrator,
        handoff_text(conversation, project.label, text, link),
        reporter=reporter,
        conversation=conversation,
        retry_choice=retry_choice,
    )
    now = clock.now()
    current = thread_map.get(conversation)
    base = thread_map.get(conversation, session_name=session_name(orchestrator)) or ThreadRecord(
        conversation=conversation,
        terminal_name=session_name(orchestrator),
        worktree_path=orchestrator.cwd,
        status=WorkerStatus.RUNNING,
        created_at=now,
        updated_at=now,
        started_by_pan=False,
    )
    record = base.model_copy(
        update={
            "terminal_address": orchestrator.address,
            "updated_at": now,
            "requester": requester,
            "reply_mode": ReplyMode.SLACK if reply_in_slack else ReplyMode.ON_DEMAND,
            "status_label": status_label(text),
            "task_started_at": now,
            "status_message_ts": current.status_message_ts if current is not None else None,
            "handoff": handoff,
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
    logger.info(
        f"handed to project root={project.root} conversation={conversation.key} "
        f"turn={orchestrator.turn} len={len(text)}"
    )
    return record


def waiting_orchestrator(record: ThreadRecord, terminals: list[Terminal]) -> Terminal | None:
    # The live orchestrator the thread was handed to, found by its address or name
    # (joins_terminal). It need not root a project any more: its last stream may have ended
    # while the thread waits.
    return next(
        (
            terminal
            for terminal in terminals
            if terminal.state is TerminalState.LIVE and joins_terminal(record, terminal)
        ),
        None,
    )


def end_wait_for_ended_orchestrator(
    *, record: ThreadRecord, thread_map: ThreadMap, reporter: Reporter, clock: Clock
) -> bool:
    # The orchestrator a waiting thread was handed to has ended (gone or finished): untie the
    # thread so it waits no longer, mark its status message Stopped and say once what to do
    # next. Only the caller that ends the wait posts (FileThreadMap.end_handoff_wait), so a
    # reply and the watcher's sweep never both do. True when this call ended it.
    handoff = record.handoff
    if handoff is None or not thread_map.end_handoff_wait(
        record.conversation, session_name=record.terminal_name
    ):
        return False
    publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=record,
        state=StatusState.STOPPED,
        clock=clock,
    )
    reporter.report(
        record.conversation,
        SessionNotice(
            reason=SessionNoticeReason.PROJECT_ORCHESTRATOR_ENDED, session_name=handoff.project
        ),
    )
    logger.info(f"handoff wait ended, orchestrator gone conversation={record.conversation.key}")
    return True


def check_waiting_thread(
    *,
    record: ThreadRecord,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    reporter: Reporter,
    clock: Clock,
) -> None:
    # `check` on a thread waiting for its project's orchestrator: nothing the orchestrator says
    # belongs in the thread, so only the status message is refreshed (Working, handed to the
    # project). When the orchestrator has ended, the wait ends instead.
    if waiting_orchestrator(record, terminal_host.list_terminals()) is None:
        end_wait_for_ended_orchestrator(
            record=record, thread_map=thread_map, reporter=reporter, clock=clock
        )
        return
    publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=record,
        state=StatusState.WORKING,
        clock=clock,
    )
    logger.info(f"check on a thread awaiting handoff conversation={record.conversation.key}")


def forward_to_project(
    *,
    record: ThreadRecord,
    text: str,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    reporter: Reporter,
    clock: Clock,
    reply_in_slack: bool = False,
) -> ThreadRecord:
    # A follow-up in a thread still waiting for its project's orchestrator goes to that
    # orchestrator again, with the same handoff command, and pan watches for its turn end
    # anew. An orchestrator that has ended ends the wait (end_wait_for_ended_orchestrator) and
    # raises TerminalGoneError, with nothing typed.
    handoff = record.handoff
    if handoff is None or not handoff.awaiting:
        raise NotAwaitingHandoffError(f"thread {record.conversation.id} is not awaiting a handoff")
    conversation = record.conversation
    orchestrator = waiting_orchestrator(record, terminal_host.list_terminals())
    if orchestrator is None:
        end_wait_for_ended_orchestrator(
            record=record, thread_map=thread_map, reporter=reporter, clock=clock
        )
        raise TerminalGoneError(f"the orchestrator {record.terminal_name} has ended")
    reply_mode = upgraded_reply_mode(record, reply_in_slack=reply_in_slack)
    deliver_when_ready(
        terminal_host,
        orchestrator,
        handoff_text(conversation, handoff.project, text, handoff.thread_link),
        reporter=reporter,
        conversation=conversation,
    )
    now = clock.now()
    record = record.model_copy(
        update={
            "handoff": handoff.model_copy(
                update={
                    "orchestrator_turn": orchestrator.turn,
                    "nudged": False,
                    "task": waiting_task(handoff, text),
                }
            ),
            "reply_mode": reply_mode,
            "status_label": status_label(text),
            "task_started_at": now,
            "updated_at": now,
        }
    )
    thread_map.put(record)
    record = publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=record,
        state=StatusState.WORKING,
        clock=clock,
    )
    logger.info(
        f"follow-up forwarded to project root={handoff.project_root} "
        f"conversation={conversation.key} len={len(text)}"
    )
    return record


def waiting_task(handoff: ProjectHandoff, follow_up: str) -> str:
    # The task the stream will get: what the person first asked, then each follow-up sent
    # while the thread waited, in order.
    if not handoff.task:
        return follow_up
    return f"{handoff.task}\n\n{follow_up}"


def orchestrator_turn_ended(handoff: ProjectHandoff, orchestrator: Terminal) -> bool:
    # The live orchestrator's turn on the handed task is over: hc counts a turn finished since
    # the task was typed and it is idle again. Without turn counts from the host this cannot
    # be told, so it is never assumed.
    if handoff.orchestrator_turn is None or orchestrator.turn is None:
        return False
    return (
        orchestrator.turn > handoff.orchestrator_turn
        and orchestrator.agent_status in _TURN_ENDED_STATUSES
    )


def nudge_unhanded_threads(
    *,
    thread_map: ThreadMap,
    terminal_host: TerminalHost,
    open_reporter: OpenReporter,
    clock: Clock,
) -> list[Conversation]:
    # Run on a timer by the watcher. Each thread still waiting for a project's orchestrator
    # whose turn on it has ended gets one line saying no stream was picked yet, once per
    # handed task (a forwarded follow-up arms it again). The thread is marked before the post,
    # so two sweeps never both post, and the mark is cleared when the post fails, so the next
    # sweep tries again. A thread whose orchestrator has ended stops waiting instead
    # (end_wait_for_ended_orchestrator). Returns the threads nudged.
    waiting = [
        record
        for record in thread_map.records()
        if record.tied
        and record.handoff is not None
        and record.handoff.awaiting
        and not record.handoff.nudged
    ]
    if not waiting:
        return []
    terminals = terminal_host.list_terminals()
    nudged: list[Conversation] = []
    for record in waiting:
        handoff = record.handoff
        if handoff is None:
            continue
        orchestrator = waiting_orchestrator(record, terminals)
        try:
            if orchestrator is None:
                end_wait_for_ended_orchestrator(
                    record=record, thread_map=thread_map, reporter=open_reporter(), clock=clock
                )
                continue
            if not orchestrator_turn_ended(handoff, orchestrator):
                continue
            if not thread_map.mark_handoff_nudged(
                record.conversation, session_name=record.terminal_name
            ):
                continue
            _post_no_stream_notice(record, thread_map, open_reporter)
        except (CredentialsError, SlackPostError) as e:
            logger.warning(
                f"could not post to a waiting thread conversation={record.conversation.key}: "
                f"{type(e).__name__}"
            )
            continue
        nudged.append(record.conversation)
        logger.info(f"no stream picked, nudged conversation={record.conversation.key}")
    return nudged


def _post_no_stream_notice(
    record: ThreadRecord, thread_map: ThreadMap, open_reporter: OpenReporter
) -> None:
    # Posted after the mark; a failed post clears the mark again and re-raises.
    try:
        open_reporter().report(
            record.conversation, ThreadNotice(reason=ThreadNoticeReason.NO_STREAM_PICKED)
        )
    except CredentialsError, SlackPostError:
        thread_map.mark_handoff_nudged(
            record.conversation, session_name=record.terminal_name, nudged=False
        )
        raise
