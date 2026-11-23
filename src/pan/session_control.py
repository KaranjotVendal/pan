from __future__ import annotations

import re
from pathlib import Path

from pan.config import (
    CHECK_SCREEN_MAX_LINES,
    CLAUDE_AGENT_NAMES,
    WORKER_BRIEF_TASK_MARKER,
    WORKER_SETTINGS_RELATIVE_PATH,
)
from pan.errors import (
    GitWorktreeError,
    NoExactMatchError,
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
from pan.gateway.slack_post import slack_post, slack_post_parts
from pan.interface import INCOMPLETE_READ_NOTICE, read_session, resolve_target
from pan.logging import initialise_logger
from pan.models import (
    AgentStatus,
    BranchStatus,
    InboxThread,
    ReplyMode,
    Terminal,
    TerminalState,
    ThreadRecord,
    ThreadSource,
    WorkerStatus,
    WorktreeStatus,
)
from pan.seams import (
    Clock,
    GitWorktreeAdapter,
    InboxDirectory,
    SlackAdapter,
    TerminalHost,
    ThreadMap,
)
from pan.sessions import (
    inbox_thread_known,
    joins_by_address,
    joins_terminal,
    worker_status_for,
)
from pan.spawn import desk_session_ended_reply, inbox_thread_name, slack_reply_sentence
from pan.transcript import last_assistant_text

logger = initialise_logger(__name__)

UNTIED_THREAD_REPLY = "this thread isn't tied to a session; @pan sessions lists them"
# The line on top of text typed into a session from Slack (see from_slack_text), when pan
# knows the session's Inbox thread.
FROM_SLACK_HEADER = (
    "From Slack via pan. Put questions and progress in the Plannotator Inbox thread named "
    '"{inbox_thread_name}". {slack_reply_sentence}'
)
# The same line for a session pan did not start and whose Inbox thread pan does not know yet:
# it may already report this work in a thread of its own.
FROM_SLACK_EXISTING_OR_NEW_THREAD_HEADER = (
    "From Slack via pan. Put questions and progress in your existing Plannotator Inbox thread "
    'for this work if you already have one; otherwise start one named "{inbox_thread_name}". '
    "{slack_reply_sentence}"
)
# The agent states in which a session is waiting for input, so text can be typed into it. Any
# other state (working, an error, no status, an unknown one) counts as busy, except a fresh
# Claude session (see _is_ready).
_READY_STATUSES = frozenset({AgentStatus.IDLE, AgentStatus.DONE})
# A screen line made only of box-drawing characters (Claude Code's dividers and banner frame),
# ASCII rule characters and whitespace carries nothing worth posting.
_DIVIDER_LINE = re.compile(r"[\s\u2500-\u259f\-=_]*")

# What `check` says about the session's state, from the status its terminal implies.
_STATE_SENTENCES: dict[WorkerStatus, str] = {
    WorkerStatus.SPAWNING: "{name} is starting, so there may be no answer yet.",
    WorkerStatus.RUNNING: "{name} is still working, so this answer may not be final.",
    WorkerStatus.IDLE: "{name} is idle.",
    WorkerStatus.BLOCKED: "{name} is waiting on a prompt at the terminal.",
    WorkerStatus.DONE: "{name} has finished.",
    WorkerStatus.FAILED: "{name} has ended.",
}


def inbox_reference(record: ThreadRecord) -> str | None:
    # The session's Inbox thread: its link once captured, else its name; a name pan only
    # suggested to a desk session is shown as the alternative it is.
    if record.inbox_thread_url:
        return f"Inbox: {record.inbox_thread_url}"
    if record.inbox_thread_name and inbox_thread_known(record):
        return f'Inbox thread: "{record.inbox_thread_name}"'
    if record.inbox_thread_name:
        return f'Inbox thread: its existing one, or "{record.inbox_thread_name}"'
    return None


def _session_name(terminal: Terminal) -> str:
    return terminal.name or terminal.address


def _session_record_for(terminal: Terminal, records: list[ThreadRecord]) -> ThreadRecord | None:
    # The record pan keeps for the session in this terminal (pan's own or a desk session),
    # joined by address or by name under the rules of joins_terminal; a tied record wins,
    # then the newest.
    matches = [record for record in records if joins_terminal(record, terminal)]
    for record in matches:
        if record.tied:
            return record
    return max(matches, key=lambda record: record.updated_at) if matches else None


def sent_notice(record: ThreadRecord) -> str:
    notice = f"Sent to {record.terminal_name}"
    reference = inbox_reference(record)
    if reference is not None:
        notice = f"{notice} · {reference}"
    if record.reply_mode is ReplyMode.SLACK:
        notice = f"{notice} · answers will also be posted here"
    return notice


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
        inbox_thread_name=inbox_thread, slack_reply_sentence=slack_reply_sentence(reply_mode)
    )
    return f"{header}{WORKER_BRIEF_TASK_MARKER}{text}"


def _busy_reply(name: str, retry_hint: str | None = None) -> str:
    return f"{name} is busy; {retry_hint or 'try again, or @pan check'}"


def _blocked_reply(name: str, retry_hint: str | None = None) -> str:
    reply = f"{name} is waiting on a prompt at the terminal; nothing was sent"
    return f"{reply}; {retry_hint}" if retry_hint else reply


def _is_ready(terminal: Terminal) -> bool:
    # A Claude session reports no status until its first turn ends, so a brand-new one reads
    # NONE while it waits at its prompt. NONE counts as ready only for a known Claude agent
    # that has not finished a turn (or whose host does not count turns); a shell or other
    # program with no status, UNKNOWN and WORKING stay refused.
    if terminal.agent_status in _READY_STATUSES:
        return True
    return (
        terminal.agent_status is AgentStatus.NONE
        and terminal.agent in CLAUDE_AGENT_NAMES
        and (terminal.turn is None or terminal.turn == 0)
    )


def _deliver(
    terminal_host: TerminalHost,
    terminal: Terminal,
    text: str,
    *,
    slack: SlackAdapter,
    channel: str,
    thread_ts: str,
    retry_hint: str | None = None,
) -> None:
    # Type the text only into a session that is ready for it. Busy, or a state pan cannot
    # read, is refused (typing could land in the middle of someone's work there); waiting on
    # a person is refused too, by pan here or by the host at the send. A refusal posts a
    # short reply and raises, with nothing typed; `retry_hint`, when given, is what that reply
    # tells the person to do next.
    name = _session_name(terminal)
    if terminal.agent_status is AgentStatus.BLOCKED:
        slack_post(slack, channel, thread_ts, _blocked_reply(name, retry_hint))
        raise TargetBlockedError(f"{terminal.address} is waiting on a person")
    if not _is_ready(terminal):
        slack_post(slack, channel, thread_ts, _busy_reply(name, retry_hint))
        raise SessionBusyError(f"{terminal.address} is {terminal.agent_status.value}")
    try:
        terminal_host.send(terminal.address, text)
    except TargetBlockedError:
        slack_post(slack, channel, thread_ts, _blocked_reply(name, retry_hint))
        raise


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
        record.thread_ts, found.thread_id, found.url, session_name=record.terminal_name
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
    thread_ts: str,
    channel: str,
    selector: str,
    text: str,
    requester: str | None,
    reply_in_slack: bool,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    inbox_directory: InboxDirectory,
    slack: SlackAdapter,
    clock: Clock,
    retry_hint: str | None = None,
) -> ThreadRecord:
    # `to <selector>: <task>`: send the task to the live session the selector names exactly
    # (address, name, or Herdr label or space; see resolve_target), pan's own or a desk
    # session, and tie the thread to it (the session's previous thread is untied). The task
    # goes under the "from Slack" line naming its Inbox thread. With no exact match, or a label
    # several sessions share, nothing is sent or posted: NoExactMatchError tells the
    # orchestrator to propose a session for the person to confirm (`pan propose`). A busy
    # session or one waiting on a prompt gets a short reply and raises, with nothing sent.
    terminals = terminal_host.list_terminals()
    try:
        target = resolve_target(selector, terminals)
    except (TargetNotFoundError, TargetAmbiguousError) as error:
        logger.info(f"no exact match for selector={selector} thread={thread_ts}")
        raise NoExactMatchError(f"no session matches '{selector}' exactly") from error

    name = _session_name(target)
    session_record = _session_record_for(target, thread_map.records())
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
    _deliver(
        terminal_host,
        target,
        from_slack_text(inbox_thread, text, reply_mode, thread_known=thread_known),
        slack=slack,
        channel=channel,
        thread_ts=thread_ts,
        retry_hint=retry_hint,
    )

    now = clock.now()
    if session_record is None:
        # A desk session: pan did not start it, so its record holds its own folder and pan
        # never stops, removes or reopens it.
        tied_record = ThreadRecord(
            thread_ts=thread_ts,
            terminal_name=name,
            terminal_address=target.address,
            channel=channel,
            worktree_path=target.cwd,
            status=WorkerStatus.RUNNING,
            created_at=now,
            updated_at=now,
            source=ThreadSource.SLACK,
            started_by_pan=False,
            requester=requester,
            reply_mode=reply_mode,
            inbox_thread_name=inbox_thread,
        )
    else:
        same_thread = session_record.thread_ts == thread_ts
        updates: dict[str, object] = {
            "thread_ts": thread_ts,
            "channel": channel,
            "terminal_address": target.address,
            "source": ThreadSource.SLACK,
            "requester": requester,
            "reply_mode": reply_mode,
            "inbox_thread_name": inbox_thread,
            "created_at": session_record.created_at if same_thread else now,
            "updated_at": now,
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
    slack_post(slack, channel, thread_ts, sent_notice(tied_record))
    logger.info(
        f"sent to session terminal={target.address} thread={thread_ts} "
        f"started_by_pan={tied_record.started_by_pan} len={len(text)}"
    )
    return tied_record


def follow_up_to_session(
    *,
    thread_ts: str,
    channel: str,
    text: str,
    terminal_host: TerminalHost,
    thread_map: ThreadMap,
    slack: SlackAdapter,
) -> ThreadRecord:
    # A message with no route words in a tied thread goes to the tied session, with the same
    # busy and blocked refusals as `to <name>:`. A desk session gets the "from Slack" line;
    # pan's own sessions already know their Inbox thread from their brief. When the session
    # has ended: for pan's own, nothing is posted and TerminalGoneError tells the caller to
    # reopen it; a desk session is never reopened, so pan says it ended.
    record = thread_map.get(thread_ts)
    if record is None or not record.tied:
        slack_post(slack, channel, thread_ts, UNTIED_THREAD_REPLY)
        raise ThreadNotFoundError(f"no tied session for thread_ts={thread_ts}")
    terminal = _live_terminal(terminal_host, record)
    if terminal is None:
        if not record.started_by_pan:
            slack_post(slack, channel, thread_ts, desk_session_ended_reply(record.terminal_name))
        raise TerminalGoneError(f"the session {record.terminal_name} has ended")

    typed = text
    if not record.started_by_pan:
        inbox_thread = record.inbox_thread_name or inbox_thread_name(record.terminal_name, text)
        typed = from_slack_text(
            inbox_thread, text, record.reply_mode, thread_known=inbox_thread_known(record)
        )
    _deliver(terminal_host, terminal, typed, slack=slack, channel=channel, thread_ts=thread_ts)
    if not record.started_by_pan:
        thread_map.update_status(thread_ts, WorkerStatus.RUNNING, session_name=record.terminal_name)
    logger.info(f"follow-up sent terminal={terminal.address} thread={thread_ts} len={len(text)}")
    return record


def forget_thread(
    *, thread_ts: str, channel: str, thread_map: ThreadMap, slack: SlackAdapter
) -> None:
    # Untie the thread. The session is never stopped or touched.
    record = thread_map.get(thread_ts)
    if record is None or not record.tied:
        slack_post(slack, channel, thread_ts, UNTIED_THREAD_REPLY)
        return
    thread_map.untie(thread_ts)
    slack_post(
        slack,
        channel,
        thread_ts,
        f"untied this thread from {record.terminal_name}; the session was not stopped",
    )
    logger.info(f"forget thread={thread_ts}")


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
            f"treating the session as gone thread={record.thread_ts}"
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


def check_reply(record: ThreadRecord, terminal_host: TerminalHost, *, screen_lines: int) -> str:
    # The tied session's latest answer, as is: the last assistant text from its transcript,
    # or, without one, its recent screen lines labelled as such. Headed by the session name,
    # its Inbox thread and what state it is in.
    terminal = _live_terminal(terminal_host, record)
    status = worker_status_for(terminal, record.status)
    header = record.terminal_name
    reference = inbox_reference(record)
    header = (
        f"{header} · {reference}" if reference is not None else f"{header} · no Inbox thread yet"
    )
    state = _STATE_SENTENCES[status].format(name=record.terminal_name)

    answer = last_assistant_text(record.transcript_path) if record.transcript_path else None
    if answer is not None:
        body = answer
    elif terminal is not None:
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
            body = f"Screen text (no transcript yet):\n```\n{clean_screen_text(screen)}\n```"
        except TerminalHostError:
            body = "Its screen could not be read."
    else:
        body = "No answer recorded."
    return f"{header}\n{state}\n\n{body}"


def check_thread(
    *,
    thread_ts: str,
    channel: str,
    thread_map: ThreadMap,
    terminal_host: TerminalHost,
    inbox_directory: InboxDirectory,
    slack: SlackAdapter,
    screen_lines: int,
) -> None:
    record = thread_map.get(thread_ts)
    if record is None or not record.tied:
        slack_post(slack, channel, thread_ts, UNTIED_THREAD_REPLY)
        return
    record = _with_inbox_link(record, inbox_directory, thread_map)
    slack_post_parts(
        slack, channel, thread_ts, check_reply(record, terminal_host, screen_lines=screen_lines)
    )
    logger.info(f"check thread={thread_ts} session={record.terminal_name}")


NO_SESSION_REPLY = "This thread has no session."


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


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


def kept_reply(name: str, uncommitted_count: int, unpushed_count: int) -> str:
    unsaved = [
        _counted(count, noun)
        for count, noun in (
            (uncommitted_count, "uncommitted file"),
            (unpushed_count, "unpushed commit"),
        )
        if count
    ]
    return (
        f"{name} has {' and '.join(unsaved)}; kept it. "
        "Push or commit them, or send `@pan done --discard`."
    )


def done_busy_reply(name: str) -> str:
    return (
        f"{name} is still working; send `@pan done` when it finishes, "
        "or `@pan done --discard` to stop it now."
    )


def done_blocked_reply(name: str) -> str:
    return (
        f"{name} is waiting on a prompt; answer it at the terminal and send `@pan done` again, "
        "or `@pan done --discard` to stop it now."
    )


def desk_session_done_reply(name: str) -> str:
    return f"Untied this thread from {name}; pan never closes sessions it didn't start."


def closed_reply(
    name: str, *, worktree_removed: bool, branch_deleted: bool, repo_known: bool
) -> str:
    # Says what this run removed and nothing more, so a retry after a half-finished cleanup
    # reports only the part it did.
    if worktree_removed and branch_deleted:
        return f"Closed {name} and removed its worktree and branch."
    if worktree_removed:
        return f"Closed {name} and removed its worktree."
    if branch_deleted:
        return f"Closed {name} and removed its branch; its worktree was already gone."
    if repo_known:
        return f"Closed {name}; its worktree and branch were already gone."
    return f"Closed {name}; its worktree was already gone."


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


def done_thread(
    *,
    thread_ts: str,
    channel: str,
    discard: bool,
    worktree_base: Path,
    fallback_repo: Path,
    thread_map: ThreadMap,
    terminal_host: TerminalHost,
    git: GitWorktreeAdapter,
    slack: SlackAdapter,
) -> None:
    # `done`: close the thread's tied session. A session pan did not start is only untied.
    # For pan's own, nothing is closed while it is working or waiting on a prompt, or while
    # closing it would lose work (uncommitted files, or branch commits not pushed or merged),
    # unless `discard`. Otherwise its terminal is removed (when it still exists), then its
    # worktree (or, when the folder is already gone, the repository's record of it) and its
    # branch, and only then is the record marked DONE and untied.
    record = thread_map.get(thread_ts)
    if record is None or not record.tied:
        slack_post(slack, channel, thread_ts, NO_SESSION_REPLY)
        return
    name = record.terminal_name
    if not record.started_by_pan:
        thread_map.untie(thread_ts)
        slack_post(slack, channel, thread_ts, desk_session_done_reply(name))
        logger.info(f"done untied desk session={name} thread={thread_ts}")
        return

    terminal = _session_terminal(terminal_host, record)
    if terminal is not None and terminal.state is TerminalState.LIVE and not discard:
        if terminal.agent_status is AgentStatus.WORKING:
            slack_post(slack, channel, thread_ts, done_busy_reply(name))
            raise SessionBusyError(f"{name} is still working; not closed")
        if terminal.agent_status is AgentStatus.BLOCKED:
            slack_post(slack, channel, thread_ts, done_blocked_reply(name))
            raise TargetBlockedError(f"{name} is waiting on a prompt; not closed")

    worktree_path = record.worktree_path
    worktree: WorktreeStatus | None = None
    branch: BranchStatus | None = None
    try:
        if worktree_path.exists():
            if not _is_pan_worktree(worktree_path, worktree_base):
                slack_post(
                    slack,
                    channel,
                    thread_ts,
                    f"{name}'s folder is not in pan's worktree folder; pan never removes it.",
                )
                raise UnsafeWorktreeError(f"{worktree_path} is not under {worktree_base}")
            worktree = git.worktree_status(worktree_path)
            if worktree.is_main_worktree:
                slack_post(
                    slack,
                    channel,
                    thread_ts,
                    f"{name}'s folder is a main repository; pan never removes it.",
                )
                raise UnsafeWorktreeError(f"{worktree_path} is a main worktree")
            repo = worktree.main_repo
        else:
            repo = _repo_without_worktree(record, fallback_repo, git)
        if repo is not None:
            branch = git.branch_status(repo, name)
    except GitWorktreeError:
        slack_post(slack, channel, thread_ts, f"Could not read {name}'s worktree; kept it.")
        raise

    uncommitted_count = len(uncommitted_paths(worktree)) if worktree is not None else 0
    unpushed_count = unpushed_commit_count(branch) if branch is not None else 0
    if worktree is not None:
        unpushed_count += worktree.detached_commits
    if (uncommitted_count or unpushed_count) and not discard:
        slack_post(slack, channel, thread_ts, kept_reply(name, uncommitted_count, unpushed_count))
        raise UnsavedWorkError(
            f"{name} has {uncommitted_count} uncommitted file(s) and "
            f"{unpushed_count} unpushed commit(s); not closed"
        )

    if terminal is not None:
        terminal_host.remove(terminal.address)
    worktree_removed = False
    branch_deleted = False
    try:
        if worktree is not None:
            # Without discard, any file left is pan's untracked settings file.
            has_files = bool(worktree.untracked_paths or worktree.modified_paths)
            git.remove_worktree(worktree_path, force=discard or has_files)
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
        slack_post(
            slack,
            channel,
            thread_ts,
            f"Closed {name}, but could not remove its worktree or branch; "
            f"send `@pan done` to try again, or remove {worktree_path} by hand.",
        )
        raise

    thread_map.update_status(thread_ts, WorkerStatus.DONE, session_name=name)
    thread_map.untie(thread_ts)
    slack_post(
        slack,
        channel,
        thread_ts,
        closed_reply(
            name,
            worktree_removed=worktree_removed,
            branch_deleted=branch_deleted,
            repo_known=repo is not None,
        ),
    )
    logger.info(
        f"done session={name} thread={thread_ts} discard={discard} "
        f"worktree_removed={worktree_removed} branch_deleted={branch_deleted}"
    )
