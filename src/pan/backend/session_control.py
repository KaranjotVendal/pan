from __future__ import annotations

import re
from pathlib import Path

from pan.backend.delivery import deliver_when_ready, session_name
from pan.backend.interface import INCOMPLETE_READ_NOTICE, read_session, resolve_target
from pan.backend.project_routing import check_waiting_thread, forward_to_project
from pan.backend.sessions import (
    inbox_thread_known,
    joins_by_address,
    joins_terminal,
    session_record_for,
)
from pan.backend.spawn import (
    RELAYED_FOLLOW_UP_SENTENCE,
    inbox_thread_name,
    slack_reply_sentence,
    upgraded_reply_mode,
)
from pan.backend.status_message import (
    check_state,
    post_answer,
    publish_status,
    send_private_details,
)
from pan.core.errors import (
    CredentialsError,
    GitWorktreeError,
    NoExactMatchError,
    NotPanSessionError,
    SessionBusyError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
    TerminalGoneError,
    TerminalHostError,
    ThreadNotFoundError,
    UnsafeWorktreeError,
    UnsavedWorkError,
)
from pan.core.events import (
    CleanupFailed,
    ScreenCaptured,
    SessionClosed,
    SessionNotice,
    SessionNoticeReason,
    ThreadNotice,
    ThreadNoticeReason,
    WorkKept,
)
from pan.core.models import (
    AgentStatus,
    BranchStatus,
    Conversation,
    InboxThread,
    ProjectHandoff,
    ReplyMode,
    StatusState,
    Terminal,
    TerminalState,
    ThreadRecord,
    WorkerStatus,
    WorktreeStatus,
)
from pan.core.seams import (
    Clock,
    GitWorktreeAdapter,
    InboxDirectory,
    OpenReporter,
    Reporter,
    TerminalHost,
    ThreadMap,
)
from pan.data.transcript import last_assistant_text
from pan.utils.config import (
    CHECK_SCREEN_MAX_LINES,
    WORKER_BRIEF_TASK_MARKER,
    WORKER_SETTINGS_RELATIVE_PATH,
)
from pan.utils.logging import initialise_logger
from pan.utils.text import status_label

logger = initialise_logger(__name__)

# The line on top of text typed into a session from Slack (see from_slack_text), when pan
# knows the session's Inbox thread.
FROM_SLACK_HEADER = (
    "From Slack via pan. Put questions and progress in the Plannotator Inbox thread named "
    '"{inbox_thread_name}". {slack_reply_sentence} {relayed_follow_up_sentence}'
)
# The same line for a session pan did not start and whose Inbox thread pan does not know yet:
# it may already report this work in a thread of its own.
FROM_SLACK_EXISTING_OR_NEW_THREAD_HEADER = (
    "From Slack via pan. Put questions and progress in your existing Plannotator Inbox thread "
    'for this work if you already have one; otherwise start one named "{inbox_thread_name}". '
    "{slack_reply_sentence} {relayed_follow_up_sentence}"
)
# A screen line made only of box-drawing characters (Claude Code's dividers and banner frame),
# ASCII rule characters and whitespace carries nothing worth posting.
_DIVIDER_LINE = re.compile(r"[\s\u2500-\u259f\-=_]*")


def from_slack_text(
    inbox_thread: str, text: str, reply_mode: ReplyMode, *, thread_known: bool = True
) -> str:
    # The text typed into a session, under one line saying where it came from, which Inbox
    # thread to report in and who replies in Slack. When pan does not know the session's
    # thread, the line says to keep using its existing one, else to start `inbox_thread`. The
    # brief's task marker separates them, so `pan sessions` shows only the task as the
    # session's last ask.
    template = FROM_SLACK_HEADER if thread_known else FROM_SLACK_EXISTING_OR_NEW_THREAD_HEADER
    header = template.format(
        inbox_thread_name=inbox_thread,
        slack_reply_sentence=slack_reply_sentence(reply_mode),
        relayed_follow_up_sentence=RELAYED_FOLLOW_UP_SENTENCE,
    )
    return f"{header}{WORKER_BRIEF_TASK_MARKER}{text}"


def _with_inbox_link(
    record: ThreadRecord, inbox_directory: InboxDirectory, thread_map: ThreadMap
) -> ThreadRecord:
    # A session pan has no hooks in (a desk session, or a worker whose hook missed the link)
    # gets its Inbox thread looked up by the name pan gave it, in the session's folder, each
    # time until it is found (the session creates the thread only on its first Inbox send,
    # so the lookup at send time usually misses and `check` finds it later). The id and any
    # link found are stored; with nothing found the name alone is shown.
    if record.inbox_thread_name is None or record.inbox_thread_id is not None:
        return record
    found = inbox_directory.find_thread(record.worktree_path, record.inbox_thread_name)
    if found is None:
        return record
    thread_map.record_inbox_thread(
        record.conversation, found.thread_id, found.url, session_name=record.terminal_name
    )
    return record.model_copy(
        update={
            "inbox_thread_id": found.thread_id,
            "inbox_thread_url": found.url or record.inbox_thread_url,
        }
    )


def _with_found_thread(record: ThreadRecord, found_thread: InboxThread) -> ThreadRecord:
    # The Inbox thread found under the name pan suggested is the one the session reports in:
    # record its name and id, keeping a known link only when it is the same thread.
    same_thread = record.inbox_thread_id == found_thread.thread_id
    return record.model_copy(
        update={
            "inbox_thread_name": found_thread.name,
            "inbox_thread_id": found_thread.thread_id,
            "inbox_thread_url": found_thread.url
            or (record.inbox_thread_url if same_thread else None),
        }
    )


def send_to_session(
    *,
    conversation: Conversation,
    selector: str,
    text: str,
    requester: str | None,
    reply_in_slack: bool,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    inbox_directory: InboxDirectory,
    reporter: Reporter,
    clock: Clock,
    retry_choice: str | None = None,
    label: str | None = None,
    handoff: ProjectHandoff | None = None,
) -> ThreadRecord:
    # `pan send --to <selector>`: send the task to the live session the selector names exactly
    # (address, name, or Herdr label or space; see resolve_target), pan's own or a desk
    # session, and tie the thread to it (the session's previous thread is untied). The task
    # goes under the "from Slack" line naming its Inbox thread. With no exact match, or a label
    # several sessions share, nothing is sent or posted: NoExactMatchError tells the
    # orchestrator to propose a session for the person to confirm (`pan propose`). A busy
    # session or one waiting on a prompt is reported as not delivered and raises, with nothing
    # sent.
    # On success the thread's status message says Working, and the session's Inbox thread and
    # terminal go to the requester only. `label` is the status message's task when it is not
    # `text` itself, and `handoff` the project that handed the session this work (`pan
    # handoff`); a plain send by name ties the session with no project.
    terminals = terminal_host.list_terminals()
    try:
        target = resolve_target(selector, terminals)
    except (TargetNotFoundError, TargetAmbiguousError) as error:
        logger.info(f"no exact match for selector={selector} conversation={conversation.key}")
        raise NoExactMatchError(f"no session matches '{selector}' exactly") from error

    name = session_name(target)
    # The thread's current record: its status message is the one to edit, wherever the work
    # goes. When it waits for a project's orchestrator and the person names that orchestrator,
    # the waiting record becomes this session's record (its wait cleared below), so it is not
    # replaced by a second record under the same key.
    current = thread_map.get(conversation)
    session_record = session_record_for(target, thread_map.records())
    if (
        session_record is None
        and current is not None
        and current.awaiting_handoff
        and joins_terminal(current, target)
    ):
        session_record = current
    found_thread: InboxThread | None = None
    looked_up_name: str | None = None
    if (
        session_record is not None
        and session_record.inbox_thread_name
        and not inbox_thread_known(session_record)
    ):
        # A name pan suggested earlier: if the session created that thread, it is now known.
        looked_up_name = session_record.inbox_thread_name
        found_thread = inbox_directory.find_thread(target.cwd, looked_up_name)
    if found_thread is not None:
        inbox_thread, thread_known = found_thread.name, True
    elif session_record is not None and session_record.inbox_thread_name:
        inbox_thread = session_record.inbox_thread_name
        thread_known = inbox_thread_known(session_record)
    else:
        inbox_thread = inbox_thread_name(name, text)
        thread_known = session_record is not None and inbox_thread_known(session_record)
    reply_mode = ReplyMode.SLACK if reply_in_slack else ReplyMode.ON_DEMAND
    deliver_when_ready(
        terminal_host,
        target,
        from_slack_text(inbox_thread, text, reply_mode, thread_known=thread_known),
        reporter=reporter,
        conversation=conversation,
        retry_choice=retry_choice,
    )

    now = clock.now()
    if session_record is None:
        # A desk session: pan did not start it, so its record holds its own folder and pan
        # never stops, removes or reopens it.
        tied_record = ThreadRecord(
            conversation=conversation,
            terminal_name=name,
            terminal_address=target.address,
            worktree_path=target.cwd,
            status=WorkerStatus.RUNNING,
            created_at=now,
            updated_at=now,
            started_by_pan=False,
            requester=requester,
            reply_mode=reply_mode,
            inbox_thread_name=inbox_thread,
            status_label=label if label is not None else status_label(text),
            task_started_at=now,
            status_message_ts=current.status_message_ts if current is not None else None,
            handoff=handoff,
        )
    else:
        same_thread = session_record.conversation.key == conversation.key
        updates: dict[str, object] = {
            "conversation": conversation,
            "terminal_address": target.address,
            "requester": requester,
            "reply_mode": reply_mode,
            "inbox_thread_name": inbox_thread,
            "created_at": session_record.created_at if same_thread else now,
            "updated_at": now,
            "status_label": label if label is not None else status_label(text),
            "task_started_at": now,
            # The thread's one status message, edited in place; a session moving here from
            # another thread leaves that thread's message behind.
            "status_message_ts": (
                current.status_message_ts
                if current is not None
                else session_record.status_message_ts
                if same_thread
                else None
            ),
            # Sent by name: whatever project handed it work before, this tie is not that.
            "handoff": handoff,
        }
        if not session_record.started_by_pan:
            # No hooks report a desk session's turns; it was just given work.
            updates["status"] = WorkerStatus.RUNNING
            updates["worktree_path"] = target.cwd
        tied_record = session_record.model_copy(update=updates)
    if found_thread is not None:
        tied_record = _with_found_thread(tied_record, found_thread)
    thread_map.tie(tied_record)
    if looked_up_name is None or tied_record.inbox_thread_name != looked_up_name:
        # Not looked up yet in this send (a lookup that just missed would miss again).
        tied_record = _with_inbox_link(tied_record, inbox_directory, thread_map)
    tied_record = publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=tied_record,
        state=StatusState.WORKING,
        clock=clock,
    )
    send_private_details(reporter=reporter, record=tied_record)
    logger.info(
        f"sent to session terminal={target.address} conversation={conversation.key} "
        f"started_by_pan={tied_record.started_by_pan} len={len(text)}"
    )
    return tied_record


def follow_up_to_session(
    *,
    conversation: Conversation,
    text: str,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    reporter: Reporter,
    clock: Clock,
    reply_in_slack: bool = False,
) -> ThreadRecord:
    # A follow-up in a tied thread goes to the tied session, with the same busy and blocked
    # refusals as a send to a named session. A desk session gets the "from Slack" line;
    # pan's own sessions already know their Inbox thread from their brief. When the session
    # has ended: for pan's own, nothing is posted and TerminalGoneError tells the caller to
    # reopen it; a desk session is never reopened, so pan says it ended.
    # `reply_in_slack` switches the thread to the reply-in-Slack mode, saved right after the
    # text is typed, so the Stop hook posts this answer; a busy or blocked refusal leaves the
    # record unchanged. Without it the mode is left as it is (never back to on demand). Once
    # typed, the follow-up is the thread's task and its status message says Working. A thread
    # still waiting for a project's orchestrator to pick a stream forwards it to that
    # orchestrator again (project_routing.forward_to_project).
    record = thread_map.get(conversation)
    if record is None or not record.tied:
        reporter.report(conversation, ThreadNotice(reason=ThreadNoticeReason.THREAD_NOT_TIED))
        raise ThreadNotFoundError(f"no tied session for thread_ts={conversation.id}")
    if record.awaiting_handoff:
        # No stream is picked yet: the follow-up goes to the project's orchestrator again.
        return forward_to_project(
            record=record,
            text=text,
            terminal_host=terminal_host,
            thread_map=thread_map,
            reporter=reporter,
            clock=clock,
            reply_in_slack=reply_in_slack,
        )
    terminal = _live_terminal(terminal_host, record)
    if terminal is None:
        if not record.started_by_pan:
            reporter.report(
                conversation,
                SessionNotice(
                    reason=SessionNoticeReason.DESK_SESSION_ENDED,
                    session_name=record.terminal_name,
                ),
            )
        raise TerminalGoneError(f"the session {record.terminal_name} has ended")
    reply_mode = upgraded_reply_mode(record, reply_in_slack=reply_in_slack)

    typed = text
    if not record.started_by_pan:
        inbox_thread = record.inbox_thread_name or inbox_thread_name(record.terminal_name, text)
        typed = from_slack_text(
            inbox_thread, text, reply_mode, thread_known=inbox_thread_known(record)
        )
    deliver_when_ready(terminal_host, terminal, typed, reporter=reporter, conversation=conversation)
    if reply_mode is not record.reply_mode:
        thread_map.set_reply_mode(conversation, reply_mode, session_name=record.terminal_name)
        logger.info(f"reply mode set to {reply_mode} conversation={conversation.key}")
        record = record.model_copy(update={"reply_mode": reply_mode})
    if not record.started_by_pan:
        thread_map.update_status(
            conversation, WorkerStatus.RUNNING, session_name=record.terminal_name
        )
    label, started_at = status_label(text), clock.now()
    thread_map.record_task(conversation, label, started_at, session_name=record.terminal_name)
    record = publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=record.model_copy(update={"status_label": label, "task_started_at": started_at}),
        state=StatusState.WORKING,
        clock=clock,
    )
    logger.info(
        f"follow-up sent terminal={terminal.address} conversation={conversation.key} len={len(text)}"
    )
    return record


def forget_thread(*, conversation: Conversation, thread_map: ThreadMap, reporter: Reporter) -> None:
    # Untie the thread. The session is never stopped or touched.
    record = thread_map.get(conversation)
    if record is None or not record.tied:
        reporter.report(conversation, ThreadNotice(reason=ThreadNoticeReason.THREAD_NOT_TIED))
        return
    thread_map.untie(conversation)
    reporter.report(
        conversation,
        SessionNotice(reason=SessionNoticeReason.THREAD_UNTIED, session_name=record.terminal_name),
    )
    logger.info(f"forget conversation={conversation.key}")


def stop_thread(
    *,
    conversation: Conversation,
    thread_map: ThreadMap,
    terminal_host: TerminalHost,
    open_reporter: OpenReporter,
    clock: Clock,
) -> None:
    """Kill switch: remove the worker's terminal, mark its record FAILED and edit the thread's
    status message to Stopped. A session pan did not start is only untied, never stopped
    (NotPanSessionError, exit 25)."""
    record = thread_map.get(conversation)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={conversation.id}")
    if not record.started_by_pan:
        if record.tied:
            thread_map.untie(conversation)
        raise NotPanSessionError(
            f"{record.terminal_name} was not started by pan, so pan never stops it; "
            "this thread is untied from it"
        )
    if record.terminal_address:
        terminal_host.remove(record.terminal_address)
    thread_map.update_status(conversation, WorkerStatus.FAILED, session_name=record.terminal_name)
    if record.tied:
        _publish_stopped(open_reporter, thread_map, record, clock)


def _publish_stopped(
    open_reporter: OpenReporter, thread_map: ThreadMap, record: ThreadRecord, clock: Clock
) -> None:
    # Best effort: the kill switch has done its work whether or not Slack can be reached.
    try:
        reporter = open_reporter()
    except CredentialsError as e:
        logger.warning(f"stop could not update the status message: {type(e).__name__}")
        return
    publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=record,
        state=StatusState.STOPPED,
        clock=clock,
    )


def _session_terminal(terminal_host: TerminalHost, record: ThreadRecord) -> Terminal | None:
    # The record's terminal, live or finished, by its address (else its name). A terminal now
    # at that address under another name is a different session, so the record's one is gone.
    selector = record.terminal_address or record.terminal_name
    try:
        terminal = terminal_host.get(selector)
    except TerminalGoneError:
        return None
    if record.terminal_address and not joins_by_address(record, terminal):
        logger.info(
            f"terminal at address={record.terminal_address} is not {record.terminal_name}; "
            f"treating the session as gone conversation={record.conversation.key}"
        )
        return None
    return terminal


def _live_terminal(terminal_host: TerminalHost, record: ThreadRecord) -> Terminal | None:
    terminal = _session_terminal(terminal_host, record)
    return terminal if terminal is not None and terminal.state is TerminalState.LIVE else None


def clean_screen_text(screen: str, max_lines: int = CHECK_SCREEN_MAX_LINES) -> str:
    # Screen text for Slack: trailing whitespace stripped, divider lines dropped, runs of blank
    # lines collapsed to one, and only the last `max_lines` non-empty lines kept. A leading
    # "(incomplete: ...)" marker from read_session is kept on top.
    lines = screen.splitlines()
    marker = None
    if lines and lines[0] == INCOMPLETE_READ_NOTICE:
        marker, lines = lines[0], lines[1:]
    kept: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip()
        if not line:
            if kept and kept[-1]:
                kept.append(line)
        elif not _DIVIDER_LINE.fullmatch(line):
            kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    non_empty_seen = 0
    start = len(kept)
    while start > 0 and non_empty_seen < max_lines:
        start -= 1
        if kept[start]:
            non_empty_seen += 1
    tail = kept[start:]
    if marker is not None:
        tail.insert(0, marker)
    return "\n".join(tail)


def screen_answer(
    record: ThreadRecord,
    terminal: Terminal | None,
    terminal_host: TerminalHost,
    *,
    screen_lines: int,
) -> str | None:
    # For a session with no transcript (a desk session has no hooks): its recent screen lines,
    # cleaned. None when there is no live terminal or nothing to show.
    if terminal is None:
        return None
    try:
        screen = read_session(
            terminal_host,
            [],
            terminal.address,
            [terminal],
            full=False,
            lines=screen_lines,
            full_history_lines=screen_lines,
        )
    except TerminalHostError:
        logger.info(f"check could not read the screen conversation={record.conversation.key}")
        return None
    cleaned = clean_screen_text(screen)
    return cleaned or None


def check_thread(
    *,
    conversation: Conversation,
    thread_map: ThreadMap,
    terminal_host: TerminalHost,
    inbox_directory: InboxDirectory,
    reporter: Reporter,
    clock: Clock,
    screen_lines: int,
) -> None:
    # Bring the thread's status message up to date (edited in place, or posted anew when it is
    # missing), post the latest answer as its own clean message when there is one, and send
    # the session's Inbox thread and terminal to the requester only.
    record = thread_map.get(conversation)
    if record is None or not record.tied:
        reporter.report(conversation, ThreadNotice(reason=ThreadNoticeReason.THREAD_NOT_TIED))
        return
    if record.awaiting_handoff:
        # Nothing the project's orchestrator says belongs in the thread.
        check_waiting_thread(
            record=record,
            terminal_host=terminal_host,
            thread_map=thread_map,
            reporter=reporter,
            clock=clock,
        )
        return
    record = _with_inbox_link(record, inbox_directory, thread_map)
    terminal = _live_terminal(terminal_host, record)
    state = check_state(terminal, record.status)
    record = publish_status(
        reporter=reporter, thread_map=thread_map, record=record, state=state, clock=clock
    )
    # The last answer in the transcript, posted alone unless pan already posted that same
    # answer (the Stop hook, in the reply-in-Slack mode); without a transcript, screen text.
    answer = last_assistant_text(record.transcript_path) if record.transcript_path else None
    if answer is not None:
        post_answer(reporter=reporter, thread_map=thread_map, record=record, answer=answer)
    else:
        screen = screen_answer(record, terminal, terminal_host, screen_lines=screen_lines)
        if screen is not None:
            reporter.report(conversation, ScreenCaptured(text=screen))
    send_private_details(reporter=reporter, record=record)
    logger.info(f"check conversation={conversation.key} session={record.terminal_name}")


def uncommitted_paths(status: WorktreeStatus) -> list[str]:
    # The worktree's uncommitted work: every modified tracked file, and every untracked file
    # except the settings file pan wrote at spawn (the worker's hooks), which is pan's. A
    # tracked settings file the worker changed is the worker's work, so it counts.
    untracked = [path for path in status.untracked_paths if path != WORKER_SETTINGS_RELATIVE_PATH]
    return [*status.modified_paths, *untracked]


def unpushed_commit_count(branch: BranchStatus) -> int:
    # Commits that deleting the branch would lose: those its upstream lacks. An upstream that
    # is gone (its remote branch was deleted after a merge) counts as holding them all. With
    # no upstream: those merged into neither the repo's default branch nor the base branch.
    if branch.upstream is not None:
        return 0 if branch.upstream_gone else branch.commits_not_on_upstream
    return branch.commits_not_in_base


def force_branch_delete(branch: BranchStatus, *, discard: bool) -> bool:
    # `git branch -d` refuses a branch not merged into its upstream (or, with none, the main
    # checkout's branch). -D is used when that refusal would be wrong: with discard, when the
    # branch has no commits outside the base branches, or when its upstream is gone (merged
    # and deleted remotely, so -d would compare against nothing). Otherwise -d, which deletes
    # a branch whose commits are all on its upstream.
    return discard or branch.commits_not_in_base == 0 or branch.upstream_gone


def _is_pan_worktree(worktree_path: Path, worktree_base: Path) -> bool:
    resolved_path = worktree_path.resolve()
    resolved_base = worktree_base.resolve()
    return resolved_path != resolved_base and resolved_path.is_relative_to(resolved_base)


def _repo_without_worktree(
    record: ThreadRecord, fallback_repo: Path, git: GitWorktreeAdapter
) -> Path | None:
    # Where to finish the cleanup once the worktree folder is gone: the repository recorded at
    # spawn, else (a record from before pan stored it) the orchestrator's folder, but only
    # when that repository has the session's branch, so pan never touches a repository the
    # session did not come from.
    if record.repo_path is not None and record.repo_path.is_dir():
        return record.repo_path
    if not fallback_repo.is_dir():
        return None
    try:
        has_branch = git.branch_status(fallback_repo, record.terminal_name) is not None
    except GitWorktreeError:
        return None
    return fallback_repo if has_branch else None


def _refuse_closing_a_busy_session(
    terminal: Terminal | None,
    *,
    discard: bool,
    conversation: Conversation,
    name: str,
    reporter: Reporter,
) -> None:
    # Nothing is closed while its live terminal is working or waiting on a prompt, unless
    # `discard`.
    if terminal is None or terminal.state is not TerminalState.LIVE or discard:
        return
    if terminal.agent_status is AgentStatus.WORKING:
        reporter.report(
            conversation,
            SessionNotice(reason=SessionNoticeReason.NOT_CLOSED_WORKING, session_name=name),
        )
        raise SessionBusyError(f"{name} is still working; not closed")
    if terminal.agent_status is AgentStatus.BLOCKED:
        reporter.report(
            conversation,
            SessionNotice(
                reason=SessionNoticeReason.NOT_CLOSED_WAITING_ON_PROMPT, session_name=name
            ),
        )
        raise TargetBlockedError(f"{name} is waiting on a prompt; not closed")


def _read_worktree_and_branch(
    record: ThreadRecord,
    *,
    conversation: Conversation,
    worktree_base: Path,
    fallback_repo: Path,
    git: GitWorktreeAdapter,
    reporter: Reporter,
) -> tuple[WorktreeStatus | None, Path | None, BranchStatus | None]:
    # The session's worktree (None when its folder is gone), the repository it belongs to
    # (None when unknown) and the session's branch there (None when it has none). A folder
    # outside pan's worktrees, or a main checkout, is refused before anything is touched.
    name = record.terminal_name
    worktree_path = record.worktree_path
    worktree: WorktreeStatus | None = None
    branch: BranchStatus | None = None
    try:
        if worktree_path.exists():
            if not _is_pan_worktree(worktree_path, worktree_base):
                reporter.report(
                    conversation,
                    SessionNotice(
                        reason=SessionNoticeReason.FOLDER_OUTSIDE_WORKTREES, session_name=name
                    ),
                )
                raise UnsafeWorktreeError(f"{worktree_path} is not under {worktree_base}")
            worktree = git.worktree_status(worktree_path)
            if worktree.is_main_worktree:
                reporter.report(
                    conversation,
                    SessionNotice(
                        reason=SessionNoticeReason.FOLDER_IS_MAIN_REPOSITORY, session_name=name
                    ),
                )
                raise UnsafeWorktreeError(f"{worktree_path} is a main worktree")
            repo = worktree.main_repo
        else:
            repo = _repo_without_worktree(record, fallback_repo, git)
        if repo is not None:
            branch = git.branch_status(repo, name)
    except GitWorktreeError:
        reporter.report(
            conversation,
            SessionNotice(reason=SessionNoticeReason.WORKTREE_UNREADABLE, session_name=name),
        )
        raise
    return worktree, repo, branch


def _refuse_closing_unsaved_work(
    worktree: WorktreeStatus | None,
    branch: BranchStatus | None,
    *,
    discard: bool,
    conversation: Conversation,
    name: str,
    reporter: Reporter,
) -> None:
    # Nothing is closed while closing it would lose work (uncommitted files, or branch commits
    # not pushed or merged), unless `discard`.
    uncommitted_count = len(uncommitted_paths(worktree)) if worktree is not None else 0
    unpushed_count = unpushed_commit_count(branch) if branch is not None else 0
    if worktree is not None:
        unpushed_count += worktree.detached_commits
    if (uncommitted_count or unpushed_count) and not discard:
        reporter.report(
            conversation,
            WorkKept(
                session_name=name,
                uncommitted_files=uncommitted_count,
                unpushed_commits=unpushed_count,
            ),
        )
        raise UnsavedWorkError(
            f"{name} has {uncommitted_count} uncommitted file(s) and "
            f"{unpushed_count} unpushed commit(s); not closed"
        )


def _remove_worktree_and_branch(
    record: ThreadRecord,
    worktree: WorktreeStatus | None,
    repo: Path | None,
    branch: BranchStatus | None,
    *,
    discard: bool,
    conversation: Conversation,
    thread_map: ThreadMap,
    git: GitWorktreeAdapter,
    reporter: Reporter,
) -> tuple[bool, bool]:
    # Removes the worktree (or, when the folder is already gone, the repository's record of
    # it) and then the branch; returns whether the worktree was removed and the branch deleted.
    name = record.terminal_name
    worktree_removed = False
    branch_deleted = False
    try:
        if worktree is not None:
            # Without discard, any file left is pan's untracked settings file.
            has_files = bool(worktree.untracked_paths or worktree.modified_paths)
            git.remove_worktree(record.worktree_path, force=discard or has_files)
            worktree_removed = True
        elif repo is not None:
            git.prune_worktrees(repo)
        if repo is not None and branch is not None:
            git.delete_branch(repo, name, force=force_branch_delete(branch, discard=discard))
            branch_deleted = True
    except GitWorktreeError:
        # The terminal is gone but the cleanup is not finished: the record keeps its status
        # and its tie (so `@pan done` retries from where it stopped), and forgets the removed
        # terminal's address, so nothing is ever sent to a terminal now at that address.
        thread_map.put(record.model_copy(update={"terminal_address": ""}))
        reporter.report(
            conversation, CleanupFailed(session_name=name, worktree_path=record.worktree_path)
        )
        raise
    return worktree_removed, branch_deleted


def done_thread(
    *,
    conversation: Conversation,
    discard: bool,
    worktree_base: Path,
    fallback_repo: Path,
    thread_map: ThreadMap,
    terminal_host: TerminalHost,
    git: GitWorktreeAdapter,
    reporter: Reporter,
    clock: Clock,
) -> None:
    # `done`: close the thread's tied session. A session pan did not start is only untied.
    # For pan's own, nothing is closed while it is working or waiting on a prompt, or while
    # closing it would lose work (uncommitted files, or branch commits not pushed or merged),
    # unless `discard`. Otherwise its terminal is removed (when it still exists), then its
    # worktree (or, when the folder is already gone, the repository's record of it) and its
    # branch, and only then is the record marked DONE and untied, and the thread's status
    # message says Closed.
    record = thread_map.get(conversation)
    if record is None or not record.tied:
        reporter.report(conversation, ThreadNotice(reason=ThreadNoticeReason.NO_SESSION))
        return
    name = record.terminal_name

    if not record.started_by_pan:
        thread_map.untie(conversation)
        reporter.report(
            conversation,
            SessionNotice(reason=SessionNoticeReason.DESK_SESSION_UNTIED, session_name=name),
        )
        logger.info(f"done untied desk session={name} conversation={conversation.key}")
        return

    terminal = _session_terminal(terminal_host, record)
    _refuse_closing_a_busy_session(
        terminal, discard=discard, conversation=conversation, name=name, reporter=reporter
    )
    worktree, repo, branch = _read_worktree_and_branch(
        record,
        conversation=conversation,
        worktree_base=worktree_base,
        fallback_repo=fallback_repo,
        git=git,
        reporter=reporter,
    )
    _refuse_closing_unsaved_work(
        worktree, branch, discard=discard, conversation=conversation, name=name, reporter=reporter
    )

    if terminal is not None:
        terminal_host.remove(terminal.address)
    worktree_removed, branch_deleted = _remove_worktree_and_branch(
        record,
        worktree,
        repo,
        branch,
        discard=discard,
        conversation=conversation,
        thread_map=thread_map,
        git=git,
        reporter=reporter,
    )

    thread_map.update_status(conversation, WorkerStatus.DONE, session_name=name)
    thread_map.untie(conversation)
    publish_status(
        reporter=reporter,
        thread_map=thread_map,
        record=record,
        state=StatusState.CLOSED,
        clock=clock,
    )
    reporter.report(
        conversation,
        SessionClosed(
            session_name=name,
            worktree_removed=worktree_removed,
            branch_deleted=branch_deleted,
            repo_known=repo is not None,
        ),
    )
    logger.info(
        f"done session={name} conversation={conversation.key} discard={discard} "
        f"worktree_removed={worktree_removed} branch_deleted={branch_deleted}"
    )
