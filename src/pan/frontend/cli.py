from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path

import typer
from pydantic import SecretStr

# Typer 0.27 vendors click; its exception base lives here (no standalone click dep).
from typer._click.exceptions import (  # noqa: PLC2701 - Typer exports no public name for ClickException
    ClickException,
)

from pan.backend.confirmations import (
    cancel_confirmation,
    confirm_session,
    normalize_choice,
    propose_sessions,
)
from pan.backend.drain import DrainedEntry, drain_inbox
from pan.backend.gates import (
    newest_drained_item,
    refuse_x_source,
    require_discard_request,
    require_named_target,
    require_requesters_message,
)
from pan.backend.handoff import handoff_thread
from pan.backend.interface import read_session, relay_to_session
from pan.backend.lifecycle import bring_up
from pan.backend.project_routing import hand_to_project, nudge_unhanded_threads
from pan.backend.service import (
    install_services,
    resolve_pan_binary,
    service_statuses,
    uninstall_services,
)
from pan.backend.session_control import (
    check_thread,
    done_thread,
    follow_up_to_session,
    forget_thread,
    send_to_session,
    stop_thread,
)
from pan.backend.sessions import (
    collect_sessions,
    send_sessions_privately,
    worker_status_for,
)
from pan.backend.spawn import ClaudeLauncher, reopen_worker, spawn_worker
from pan.core.errors import (
    BriefUnavailableError,
    ConfigMissingError,
    CredentialsError,
    DiscardNotRequestedError,
    GitWorktreeError,
    HandoffTargetNotFoundError,
    InboxError,
    NoExactMatchError,
    NotAwaitingHandoffError,
    NotPanSessionError,
    PanError,
    PrivateMessageError,
    ServiceError,
    SessionBusyError,
    SlackPostError,
    SpawnError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetInOtherProjectError,
    TargetNotFoundError,
    TerminalGoneError,
    TerminalHostError,
    TerminalNameTakenError,
    ThreadNotFoundError,
    UnansweredQuestionError,
    UnauthorizedSenderError,
    UnsafeWorktreeError,
    UnsavedWorkError,
    XSourceRefusedError,
)
from pan.core.models import (
    Conversation,
    RequestOrigin,
    SessionSummary,
    SlackCredentials,
    WorkerStatus,
)
from pan.core.seams import Reporter, ThreadMap
from pan.data.adapters.launchd import build_service_manager
from pan.data.threadmap import conversation_for_id
from pan.frontend.gateway.slack_post import slack_post
from pan.frontend.hooks.inbox_link import inbox_link_hook
from pan.frontend.hooks.notification import notification_hook
from pan.frontend.hooks.session_start import session_start_hook
from pan.frontend.hooks.stop import stop_hook
from pan.frontend.slack_render import listed_sessions, sessions_to_show
from pan.frontend.watcher import WatchdogInboxWatcher
from pan.frontend.wiring import Wiring, load_wiring
from pan.utils.clock import UuidGen
from pan.utils.config import READ_FULL_HISTORY_LINES, READ_RECENT_LINES
from pan.utils.credentials import load_credentials, save_credentials
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="pan — Slack coding orchestrator"
)
config_app = typer.Typer(no_args_is_help=True, help="Manage config and credentials")
inbox_app = typer.Typer(no_args_is_help=True, help="Inbox queue operations")
threads_app = typer.Typer(no_args_is_help=True, help="Thread-map operations")
hook_app = typer.Typer(no_args_is_help=True, help="Claude Code worker hook entrypoints")
service_app = typer.Typer(
    no_args_is_help=True, help="Install, remove or inspect pan's launchd agents (macOS)"
)
app.add_typer(config_app, name="config")
app.add_typer(inbox_app, name="inbox")
app.add_typer(threads_app, name="threads")
app.add_typer(hook_app, name="hook")
app.add_typer(service_app, name="service")

# Documented taxonomy -> exit-code table. Unclassified exceptions are defects and
# surface as tracebacks (Principle 4), never mapped here.
_EXIT_CODES: dict[type[PanError], int] = {
    UnauthorizedSenderError: 10,
    ConfigMissingError: 11,
    CredentialsError: 12,
    InboxError: 13,
    ThreadNotFoundError: 14,
    SpawnError: 15,
    TerminalHostError: 16,
    SlackPostError: 17,
    TargetNotFoundError: 20,
    TargetAmbiguousError: 21,
    TerminalGoneError: 22,
    TargetBlockedError: 23,
    TerminalNameTakenError: 24,
    NotPanSessionError: 25,
    SessionBusyError: 26,
    ServiceError: 27,
    NoExactMatchError: 28,
    UnsavedWorkError: 29,
    GitWorktreeError: 30,
    UnsafeWorktreeError: 31,
    PrivateMessageError: 32,
    DiscardNotRequestedError: 33,
    UnansweredQuestionError: 34,
    XSourceRefusedError: 35,
    BriefUnavailableError: 36,
    NotAwaitingHandoffError: 37,
    HandoffTargetNotFoundError: 38,
    TargetInOtherProjectError: 39,
}


def _exit_code_for(error: PanError) -> int:
    # The most specific documented class wins, so an adapter's own subclass (the hc
    # adapter's HcError) maps through its neutral base (TerminalHostError).
    for error_class in type(error).__mro__:
        exit_code = _EXIT_CODES.get(error_class)
        if exit_code is not None:
            return exit_code
    return 1


@app.command()
def gateway() -> None:
    """Run the always-on Bolt Socket Mode gateway (blocking; launchd-supervised)."""
    load_wiring().gateway_slack_adapter().start()


@app.command()
def watcher() -> None:
    """Run the inbox watcher that nudges the orchestrator (blocking; launchd-supervised). It
    also checks, every few seconds, threads handed to a project whose orchestrator ended its
    turn without picking a stream, and says so once in each."""
    wiring = load_wiring()

    def nudge_unhanded() -> None:
        nudge_unhanded_threads(
            thread_map=wiring.thread_map,
            terminal_host=wiring.terminal_host,
            open_reporter=lambda: wiring.reporter,
            clock=wiring.clock,
        )

    WatchdogInboxWatcher(
        wiring.terminal_host,
        wiring.config.orchestrator.terminal,
        wiring.config.paths.inbox,
        sweep=nudge_unhanded,
    ).start()


@service_app.command("install")
def service_install() -> None:
    """Install and load the gateway, watcher and up LaunchAgents, so pan starts at login.
    Running it again reinstalls them."""
    home = Path.home()
    pan_binary = resolve_pan_binary(home)
    labels = install_services(build_service_manager(home), home=home, pan_binary=pan_binary)
    for label in labels:
        typer.echo(f"loaded {label}")
    typer.echo(f"agents run {pan_binary}; logs in {home / '.pan' / 'logs'}")


@service_app.command("uninstall")
def service_uninstall() -> None:
    """Unload the pan LaunchAgents and delete their plists."""
    for result in uninstall_services(build_service_manager(Path.home())):
        loaded = "unloaded" if result.was_loaded else "was not loaded"
        plist = "plist deleted" if result.plist_removed else "no plist"
        typer.echo(f"{result.label}: {loaded}, {plist}")


@service_app.command("status")
def service_status() -> None:
    """Show each pan LaunchAgent: plist installed, loaded, pid and last exit code."""
    for status in service_statuses(build_service_manager(Path.home())):
        installed = "installed" if status.plist_installed else "not installed"
        loaded = "loaded" if status.loaded else "not loaded"
        pid = status.pid if status.pid is not None else "-"
        last_exit = status.last_exit_code if status.last_exit_code is not None else "-"
        typer.echo(
            f"{status.label:<16} {installed:<13} {loaded:<10} pid={pid} last_exit={last_exit}"
        )


@config_app.command("set-token")
def config_set_token(
    bot_token: str = typer.Option(..., prompt=True, hide_input=True),
    app_token: str = typer.Option(..., prompt=True, hide_input=True),
) -> None:
    """Write ~/.pan/credentials.json at mode 0600."""
    config = load_wiring().config
    save_credentials(
        SlackCredentials(bot_token=SecretStr(bot_token), app_token=SecretStr(app_token)),
        config.paths.credentials,
    )
    typer.echo("credentials saved")


@config_app.command("show")
def config_show() -> None:
    """Print the resolved config (credentials masked)."""
    config = load_wiring().config
    typer.echo(config.model_dump_json(indent=2))
    try:
        credentials = load_credentials(config.paths.credentials)
    except CredentialsError:
        typer.echo("credentials: not set")
        return
    # SecretStr fields serialize masked ("**********"); get_secret_value is never called here.
    typer.echo(credentials.model_dump_json())


@inbox_app.command("drain")
def inbox_drain(as_json: bool = typer.Option(False, "--json")) -> None:  # noqa: FBT001 - Typer, not a caller, passes CLI flags
    """Atomically drain and clear the inbox; emit each item with its cleaned text, its thread's
    tie and pan's open question in the thread (JSON for the orchestrator). A reply from the
    requester after the question marks it answered, which pan confirm --choice needs."""
    wiring = load_wiring()
    entries = drain_inbox(
        inbox_store=wiring.inbox_store,
        thread_map=wiring.thread_map,
        confirmations=wiring.confirmations,
        drained_log=wiring.drained_log,
        clock=wiring.clock,
    )
    if as_json:
        typer.echo(json.dumps([entry.model_dump(mode="json") for entry in entries]))
        return
    for entry in entries:
        typer.echo(_drained_entry_text(entry))


def _drained_entry_text(entry: DrainedEntry) -> str:
    item = entry.item
    channel = item.channel if item.channel is not None else "-"
    lines = [f"{item.id} {channel} {item.thread_ts} {item.slack_user}", f"  {entry.text}"]
    if entry.open_question is not None:
        answered = "answered" if entry.open_question.answered_by_requester else "not answered"
        lines.append(f"  open question in this thread ({answered} by the requester)")
    if entry.tie is not None:
        lines.append(f"  {entry.tie.note}")
    return "\n".join(lines)


@app.command()
def spawn(
    thread: str = typer.Option(..., "--thread"),
    task: str = typer.Option(..., "--task"),
    channel: str = typer.Option(..., "--channel"),
    stream: str | None = typer.Option(None, "--stream"),
    repo: Path | None = typer.Option(None, "--repo"),
    requester: str | None = typer.Option(None, "--requester", help="the sender's Slack user id"),
    request_text: str | None = typer.Option(
        None, "--request-text", help="the original message; 'reply in slack' in it sets the mode"
    ),
    reply_in_slack: bool = typer.Option(  # noqa: FBT001 - Typer, not a caller, passes CLI flags
        False, "--reply-in-slack", help="post the answer in Slack at every turn end"
    ),
    thread_reply: bool = typer.Option(  # noqa: FBT001 - Typer, not a caller, passes CLI flags
        False,
        "--thread-reply",
        help="the message was a reply inside a Slack thread; the brief says to read the thread",
    ),
) -> None:
    """Create worktree + hc terminal + worker session; tie the thread to it; post the start
    notice naming its Inbox thread. With --thread-reply the brief tells the session to read the
    Slack thread first with its own Slack read-thread tool."""
    wiring = load_wiring()
    config = wiring.config
    reporter = wiring.reporter
    terminal_host = wiring.terminal_host
    # spawn_worker already posts the failure notice and records a FAILED ThreadRecord,
    # then re-raises SpawnError, so this boundary only maps the exit code (no double post).
    # BriefUnavailableError (no brief for the request's source yet) is raised before anything
    # is created or posted.
    spawn_worker(
        origin=RequestOrigin(
            conversation=Conversation.slack(thread, channel),
            from_thread_reply=thread_reply,
            requester=requester,
        ),
        task=task,
        repo=repo if repo is not None else Path.cwd(),
        base=config.orchestrator.worktree_base,
        stream=stream,
        app_space=config.orchestrator.app_space,
        git=wiring.git,
        terminal_host=terminal_host,
        launcher=ClaudeLauncher(terminal_host),
        thread_map=wiring.thread_map,
        reporter=reporter,
        clock=wiring.clock,
        id_gen=UuidGen(),
        request_text=request_text,
        reply_in_slack=reply_in_slack,
    )


@app.command()
def reopen(
    thread: str = typer.Option(..., "--thread"),
    task: str = typer.Option(..., "--task"),
    reply_in_slack: bool = typer.Option(  # noqa: FBT001 - Typer, not a caller, passes CLI flags
        False,
        "--reply-in-slack",
        help="switch the thread to posting the answer in Slack at every turn end",
    ),
    item: str | None = typer.Option(
        None, "--item", help="the inbox item id (item.id) of the person's follow-up"
    ),
) -> None:
    """Reopen a thread's finished or lost worker in its existing worktree, continuing its
    last Claude conversation with the follow-up task; post the ack. --reply-in-slack switches
    the thread to posting answers in Slack (never back). A thread from X (its record, or the
    --item pan drained) exits 35: X may only start new sessions."""
    wiring = load_wiring()
    refuse_x_source(wiring.thread_map, wiring.drained_log, item, thread)
    record = wiring.thread_map.get(conversation_for_id(wiring.thread_map, thread))
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread}")
    reporter = wiring.reporter
    terminal_host = wiring.terminal_host
    # reopen_worker posts its own failure notice and re-raises SpawnError; this boundary
    # only maps the exit code.
    reopen_worker(
        record=record,
        task=task,
        app_space=wiring.config.orchestrator.app_space,
        terminal_host=terminal_host,
        launcher=ClaudeLauncher(terminal_host),
        thread_map=wiring.thread_map,
        reporter=reporter,
        clock=wiring.clock,
        reply_in_slack=reply_in_slack,
    )


@threads_app.command("get")
def threads_get(thread: str = typer.Option(..., "--thread")) -> None:
    """Print the ThreadRecord for a thread_ts as JSON."""
    thread_map = load_wiring().thread_map
    record = thread_map.get(conversation_for_id(thread_map, thread))
    typer.echo(record.model_dump_json() if record is not None else "null")


@threads_app.command("set")
def threads_set(
    thread: str = typer.Option(..., "--thread"),
    status: WorkerStatus = typer.Option(..., "--status"),
) -> None:
    """Update a ThreadRecord's status."""
    thread_map = load_wiring().thread_map
    thread_map.update_status(conversation_for_id(thread_map, thread), status)
    typer.echo(f"{thread} -> {status.value}")


@app.command("slack-post")
def slack_post_command(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    text: str = typer.Option(..., "--text"),
) -> None:
    """Post text to a Slack thread via the one egress path (INV-4)."""
    slack_post(load_wiring().slack, Conversation.slack(thread, channel), text)


def _extract_cwd(raw_stdin: str) -> str | None:
    try:
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError:
        return None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    return cwd if isinstance(cwd, str) else None


def _dispatch_completion_hook(
    hook_fn: Callable[..., None],
    raw_stdin: str,
    thread_map: ThreadMap,
    reporter: Reporter,
) -> None:
    # Resolve the worker's thread from its cwd via the thread map (INV-7), then hand
    # the already-read stdin to the existing hook function. If no record matches the
    # cwd (or stdin is unparseable), exit cleanly — a hook must never crash the worker.
    cwd = _extract_cwd(raw_stdin)
    if cwd is None:
        logger.warning("completion hook: the payload carries no cwd")
        return
    record = thread_map.get_by_worktree(Path(cwd).expanduser())
    if record is None:
        logger.warning(f"completion hook: no thread record for cwd={cwd}")
        return
    hook_fn(
        record.conversation,
        thread_map,
        reporter,
        stdin=io.StringIO(raw_stdin),
        session_name=record.terminal_name,
    )


def _run_completion_hook(hook_fn: Callable[..., None]) -> None:
    raw_stdin = sys.stdin.read()
    wiring = load_wiring()
    thread_map = wiring.thread_map
    _dispatch_completion_hook(hook_fn, raw_stdin, thread_map, wiring.reporter)


@hook_app.command("session-start")
def hook_session_start() -> None:
    """Claude Code SessionStart hook: record the worker's session id and transcript path."""
    raw_stdin = sys.stdin.read()
    thread_map = load_wiring().thread_map
    cwd = _extract_cwd(raw_stdin)
    if cwd is None:
        logger.warning("session-start hook: the payload carries no cwd")
        return
    record = thread_map.get_by_worktree(Path(cwd).expanduser())
    if record is None:
        logger.warning(f"session-start hook: no thread record for cwd={cwd}")
        return
    session_start_hook(
        record.conversation,
        thread_map,
        stdin=io.StringIO(raw_stdin),
        session_name=record.terminal_name,
    )


@hook_app.command("stop")
def hook_stop() -> None:
    """Claude Code Stop hook: mark the thread IDLE; post the answer only in reply-in-Slack mode."""
    _run_completion_hook(stop_hook)


@hook_app.command("notification")
def hook_notification() -> None:
    """Claude Code Notification hook: mark a prompt waiting on the person BLOCKED; idle is ignored."""
    _run_completion_hook(notification_hook)


def _dispatch_inbox_link_hook(raw_stdin: str, thread_map: ThreadMap) -> None:
    cwd = _extract_cwd(raw_stdin)
    if cwd is None:
        logger.warning("inbox-link hook: the payload carries no cwd")
        return
    record = thread_map.get_by_worktree(Path(cwd).expanduser())
    if record is None:
        logger.warning(f"inbox-link hook: no thread record for cwd={cwd}")
        return
    inbox_link_hook(
        record.conversation,
        thread_map,
        stdin=io.StringIO(raw_stdin),
        session_name=record.terminal_name,
    )


@hook_app.command("inbox-link")
def hook_inbox_link() -> None:
    """Claude Code PostToolUse hook on the Inbox tool: record the worker's Inbox thread link."""
    # This hook must never fail the Inbox tool call it follows, so every error is logged
    # and swallowed here (exit 0), defects included. It never posts to Slack.
    try:
        raw_stdin = sys.stdin.read()
        _dispatch_inbox_link_hook(raw_stdin, load_wiring().thread_map)
    except Exception as e:  # noqa: BLE001 - this hook must never fail the tool call
        logger.warning(f"inbox-link hook failed: {type(e).__name__}")


@app.command()
def status(thread: str = typer.Option(..., "--thread")) -> None:
    """Report a worker's live status from its hc terminal (resolved via its thread record)."""
    wiring = load_wiring()
    record = wiring.thread_map.get(conversation_for_id(wiring.thread_map, thread))
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread}")
    terminal = None
    if record.terminal_address:
        try:
            terminal = wiring.terminal_host.get(record.terminal_address)
        except TerminalGoneError:
            terminal = None
    typer.echo(worker_status_for(terminal, record.status).value)


def _render_sessions_table(summaries: list[SessionSummary]) -> str:
    header = (
        f"{'TERMINAL':<9} {'NAME':<24} {'AGENT':<9} {'OWNER':<9} {'PAN_STATUS':<11} "
        f"{'DRIFT':<5} LAST_ASK"
    )
    lines = [header]
    for summary in summaries:
        owner = "pan" if summary.is_pan_owned else "external"
        pan_status = summary.pan_status.value if summary.pan_status is not None else "-"
        display_name = summary.name or summary.where or summary.cwd.name
        drift = "DRIFT" if summary.drift else ""
        last_ask = summary.last_ask or ""
        row = (
            f"{summary.address:<9} {display_name:<24} "
            f"{summary.agent_status.value:<9} {owner:<9} {pan_status:<11} {drift:<5} {last_ask}"
        )
        lines.append(row.rstrip())
        if summary.inbox_thread_name:
            # The Inbox thread the session reports in, when pan knows it.
            lines.append(f"{'':<9} inbox: {summary.inbox_thread_name}")
    return "\n".join(lines)


@app.command()
def sessions(
    as_json: bool = typer.Option(False, "--json"),  # noqa: FBT001 - Typer, not a caller, passes CLI flags
    show: str | None = typer.Option(
        None,
        "--show",
        help=(
            "terminal addresses to send the requester, comma-separated, in the order to list "
            "them; each project is listed together, where its first session was"
        ),
    ),
    thread: str | None = typer.Option(None, "--thread", help="the thread the list was asked in"),
    channel: str | None = typer.Option(None, "--channel", help="the thread's channel"),
    requester: str | None = typer.Option(
        None, "--requester", help="the sender's Slack user id, the only one who sees the list"
    ),
) -> None:
    """List all live hc terminals with the last thing each was asked, reconciled against the
    pan thread map, flagging status drift. --json emits the SessionSummary list. --show with
    --thread, --channel and --requester sends the requester alone the sessions at those
    addresses (up to 5, in the order given, a project's sessions together where its first one
    was), each with its state, branch and a link to its thread.
    Exit 32: the private message failed."""
    wiring = load_wiring()
    summaries = collect_sessions(
        wiring.terminal_host, wiring.thread_map, project_map_source=wiring.project_map_source
    )
    if as_json:
        typer.echo(json.dumps([summary.model_dump(mode="json") for summary in summaries]))
        return
    if show is not None and thread is not None and channel is not None and requester is not None:
        addresses = [address.strip() for address in show.split(",") if address.strip()]
        _send_sessions_privately(
            wiring, summaries, Conversation.slack(thread, channel), requester, addresses
        )
        return
    if any(option is not None for option in (show, thread, channel, requester)):
        # The list is private and sent only with all four; one alone is a mistake.
        raise typer.BadParameter("--show, --thread, --channel and --requester go together")
    typer.echo(_render_sessions_table(summaries))


def _send_sessions_privately(
    wiring: Wiring,
    summaries: list[SessionSummary],
    conversation: Conversation,
    requester: str,
    addresses: list[str],
) -> None:
    orchestrator_terminal = wiring.config.orchestrator.terminal
    send_sessions_privately(
        wiring.reporter,
        conversation=conversation,
        requester=requester,
        sessions=sessions_to_show(summaries, orchestrator_terminal, addresses),
        any_running=bool(listed_sessions(summaries, orchestrator_terminal)),
    )


@app.command()
def check(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
) -> None:
    """Post the thread's tied session's latest answer to the thread, as is: the last answer in
    its transcript, or its recent screen lines. Names its Inbox thread (looked up by name for a
    session pan did not start) and says if it is still working or waiting on a prompt."""
    wiring = load_wiring()
    check_thread(
        conversation=Conversation.slack(thread, channel),
        thread_map=wiring.thread_map,
        terminal_host=wiring.terminal_host,
        inbox_directory=wiring.inbox_directory,
        reporter=wiring.reporter,
        clock=wiring.clock,
        screen_lines=READ_RECENT_LINES,
    )


@app.command()
def forget(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
) -> None:
    """Untie the thread from its session and say so in the thread. Never stops the session."""
    wiring = load_wiring()
    forget_thread(
        conversation=Conversation.slack(thread, channel),
        thread_map=wiring.thread_map,
        reporter=wiring.reporter,
    )


@app.command()
def done(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    discard: bool = typer.Option(  # noqa: FBT001 - Typer, not a caller, passes CLI flags
        False, "--discard", help="close it even if uncommitted files or unpushed commits are lost"
    ),
    item: str | None = typer.Option(
        None,
        "--item",
        help="the inbox item id (item.id) of the person's message; needed with --discard",
    ),
) -> None:
    """Close the thread's tied session: remove its terminal, its worktree and its branch, then
    mark it done and untie the thread; say so in the thread. Keeps everything (exit 29) while
    the worktree has uncommitted files (pan's own untracked .claude/settings.json aside) or the
    branch has commits not pushed (with no upstream: not merged into the default or base
    branch; an upstream deleted after a merge counts as pushed), and refuses while the session
    is working (exit 26) or waiting on a prompt (exit 23), unless --discard. A failed git
    cleanup exits 30 and keeps the thread tied for a retry; a folder pan never removes (outside
    the worktree folder, or a main repository) exits 31. With the worktree folder already gone
    it still deletes the branch. A session pan did not start is only untied, never closed.
    --discard needs --item: the thread requester's newest message pan drained in this thread,
    exactly done --discard; otherwise nothing is touched and it exits 33 (an unusable item also
    gets a one-line reply asking to send the request again)."""
    wiring = load_wiring()
    conversation = Conversation.slack(thread, channel)
    if discard:
        require_discard_request(
            newest_drained_item(wiring.drained_log, item, conversation),
            wiring.thread_map.get(conversation),
            conversation=conversation,
            open_reporter=lambda: wiring.reporter,
        )
    done_thread(
        conversation=conversation,
        discard=discard,
        worktree_base=wiring.config.orchestrator.worktree_base,
        fallback_repo=wiring.config.orchestrator.cwd,
        thread_map=wiring.thread_map,
        terminal_host=wiring.terminal_host,
        git=wiring.git,
        reporter=wiring.reporter,
        clock=wiring.clock,
    )


@app.command()
def send(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    to: str | None = typer.Option(
        None,
        "--to",
        help=(
            "a live session's exact address, name or Herdr label (or its space); omit to "
            "send to the tied session"
        ),
    ),
    project: str | None = typer.Option(
        None,
        "--project",
        help=(
            "a project's label (or its orchestrator's exact address or name): hand the task to "
            "that project's orchestrator, which picks the stream"
        ),
    ),
    task: str = typer.Option(..., "--task"),
    requester: str | None = typer.Option(None, "--requester", help="the sender's Slack user id"),
    item: str | None = typer.Option(
        None,
        "--item",
        help="the inbox item id (item.id) of the person's message; needed with --to or --project",
    ),
    reply_in_slack: bool = typer.Option(  # noqa: FBT001 - Typer, not a caller, passes CLI flags
        False, "--reply-in-slack", help="post the answer in Slack at every turn end"
    ),
) -> None:
    """Send a task to a live session (pan's own or one of yours) and tie the thread to it; post
    a notice naming its Inbox thread. --to must name one session exactly: its address, name, or
    Herdr label or space, and the message pan drained as --item (in this thread, from the
    requester) must hold it as a whole word or phrase. Otherwise (no exact match, a label
    several sessions share, a name the person did not type, an unknown item) nothing is sent or
    posted and it exits 28: propose a session with pan propose. Without --to, send a follow-up to the thread's tied session; --reply-in-slack then switches the thread to posting
    answers in Slack (never back). A busy or blocked session gets a short reply and
    nothing is typed; a session pan did not start gets one line on top saying the text came
    from Slack and which Inbox thread to use. A thread from X exits 35: X may only start new
    sessions. --project instead hands the task to a project's orchestrator, with the same
    checks (the project's label, or its orchestrator's exact address or name, in the person's
    message; else exit 28): the thread waits for it to run pan handoff, and nothing it says
    reaches the thread until then. A follow-up while it waits goes to it again."""
    if to is not None and project is not None:
        raise typer.BadParameter("pass at most one of --to or --project")
    wiring = load_wiring()
    conversation = Conversation.slack(thread, channel)
    refuse_x_source(wiring.thread_map, wiring.drained_log, item, thread)
    if project is not None:
        _hand_to_named_project(
            wiring, conversation, project, task, requester, item, reply_in_slack=reply_in_slack
        )
        return
    if to is None:
        follow_up_to_session(
            conversation=conversation,
            text=task,
            terminal_host=wiring.terminal_host,
            thread_map=wiring.thread_map,
            reporter=wiring.reporter,
            clock=wiring.clock,
            reply_in_slack=reply_in_slack,
        )
        return
    named_in = require_named_target(
        newest_drained_item(wiring.drained_log, item, conversation),
        action="send",
        item_id=item,
        target=to,
        requester=requester,
        conversation=conversation,
        open_reporter=lambda: wiring.reporter,
    )
    send_to_session(
        conversation=conversation,
        selector=to,
        text=task,
        requester=named_in.author,
        reply_in_slack=reply_in_slack,
        terminal_host=wiring.terminal_host,
        thread_map=wiring.thread_map,
        inbox_directory=wiring.inbox_directory,
        reporter=wiring.reporter,
        clock=wiring.clock,
    )


def _thread_link(wiring: Wiring, conversation: Conversation) -> str | None:
    return wiring.slack.permalink(conversation.slack_channel(), conversation.id)


def _hand_to_named_project(
    wiring: Wiring,
    conversation: Conversation,
    project: str,
    task: str,
    requester: str | None,
    item: str | None,
    *,
    reply_in_slack: bool,
) -> None:
    named_in = require_named_target(
        newest_drained_item(wiring.drained_log, item, conversation),
        action="send",
        item_id=item,
        target=project,
        requester=requester,
        conversation=conversation,
        open_reporter=lambda: wiring.reporter,
    )
    hand_to_project(
        conversation=conversation,
        selector=project,
        text=task,
        requester=named_in.author,
        reply_in_slack=reply_in_slack,
        thread_link=lambda: _thread_link(wiring, conversation),
        terminal_host=wiring.terminal_host,
        thread_map=wiring.thread_map,
        reporter=wiring.reporter,
        clock=wiring.clock,
        project_map_source=wiring.project_map_source,
    )


@app.command()
def handoff(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    to: str = typer.Option(
        ..., "--to", help="the stream (or the orchestrator itself): its exact address or name"
    ),
    item: str | None = typer.Option(
        None,
        "--item",
        help="when the person named the stream: the inbox item id of their message",
    ),
    note: str | None = typer.Option(
        None, "--note", help="the orchestrator's own instructions, typed after the task"
    ),
) -> None:
    """Pass a thread handed to a project to the session its orchestrator picked: type the
    task (then --note, then the thread's link) into it under the usual "from Slack" line, as
    pan send --to does, and tie the thread to it; its status message says where the work went.
    Handed to the orchestrator itself, nothing is typed. Exit 37: the thread is not waiting for
    a handoff. Exit 38: no live agent session has that exact address or terminal name. Exit 39:
    that session is in another project or in none. Exit 26 or 23: the session is busy or waiting
    on a prompt; pan replied and the thread keeps waiting. With --item, the name must be in the
    message of the person who asked for the work, their newest in the thread (else exit 28)."""
    wiring = load_wiring()
    conversation = Conversation.slack(thread, channel)
    if item is not None:
        refuse_x_source(wiring.thread_map, wiring.drained_log, item, thread)
        waiting = wiring.thread_map.get(conversation)
        require_named_target(
            newest_drained_item(wiring.drained_log, item, conversation),
            action="handoff",
            item_id=item,
            target=to,
            # Only the person who asked for the work may pick its stream.
            requester=waiting.requester if waiting is not None else None,
            conversation=conversation,
            open_reporter=lambda: wiring.reporter,
        )
    record = handoff_thread(
        conversation=conversation,
        selector=to,
        terminal_host=wiring.terminal_host,
        thread_map=wiring.thread_map,
        inbox_directory=wiring.inbox_directory,
        reporter=wiring.reporter,
        clock=wiring.clock,
        project_map_source=wiring.project_map_source,
        note=note,
    )
    typer.echo(f"handed thread {thread} to {record.terminal_name}")


@app.command()
def propose(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    to: str = typer.Option(..., "--to", help="the session name as the person wrote it"),
    task: str = typer.Option(..., "--task"),
    candidate: list[str] = typer.Option(
        [],
        "--candidate",
        help="a proposed session's terminal address; repeat for up to 3, most likely first",
    ),
    project_candidate: list[str] = typer.Option(
        [],
        "--project-candidate",
        help=(
            "a proposed project, by its orchestrator's terminal address; repeat for up to 3, "
            "most likely first; confirming one hands the task to that project"
        ),
    ),
    requester: str = typer.Option(
        ..., "--requester", help="the sender's Slack user id; only their reply answers"
    ),
    item: str = typer.Option(
        ..., "--item", help="the inbox item id (item.id) of the person's message"
    ),
    reply_in_slack: bool = typer.Option(  # noqa: FBT001 - Typer, not a caller, passes CLI flags
        False, "--reply-in-slack", help="post the answer in Slack at every turn end"
    ),
) -> None:
    """Ask the person to confirm where a task for a named session goes when pan send exited
    28. One candidate: "Send this to <session>? Reply yes."; several: a numbered list to pick
    1-3 from. Nothing is typed into any session until the requester replies. Candidates that
    are not live terminal addresses are dropped; with none left it says no session matches.
    --item must be --requester's newest message pan drained in this thread; otherwise nothing
    is asked, a one-line reply asks to send the request again, and it exits 28. A thread from X exits 35: X may only start new sessions.
    --project-candidate proposes projects instead of sessions (after pan send --project exited
    28): "Send this to the <project> project? Reply yes."."""
    if candidate and project_candidate:
        raise typer.BadParameter("pass --candidate or --project-candidate, not both")
    wiring = load_wiring()
    conversation = Conversation.slack(thread, channel)
    refuse_x_source(wiring.thread_map, wiring.drained_log, item, thread)
    require_requesters_message(
        newest_drained_item(wiring.drained_log, item, conversation),
        requester=requester,
        conversation=conversation,
        open_reporter=lambda: wiring.reporter,
    )
    propose_sessions(
        conversation=conversation,
        selector=to,
        task=task,
        requester=requester,
        reply_in_slack=reply_in_slack,
        candidate_addresses=candidate,
        project_addresses=project_candidate,
        terminal_host=wiring.terminal_host,
        confirmations=wiring.confirmations,
        reporter=wiring.reporter,
        clock=wiring.clock,
        project_map_source=wiring.project_map_source,
    )


@app.command()
def confirm(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    choice: str | None = typer.Option(
        None, "--choice", help="what the requester picked: yes, or a number from 1 to 3"
    ),
    cancel: bool = typer.Option(  # noqa: FBT001 - Typer, not a caller, passes CLI flags
        False, "--cancel", help="drop the open question and post Not sent."
    ),
) -> None:
    """Act on the requester's reply to the thread's open question from pan propose. --choice
    sends the stored task to the chosen session (busy and prompt refusals still apply) and ties
    the thread to it; it exits 34, with nothing sent or posted, until pan inbox drain has seen
    the requester reply in the thread. --cancel drops the question and posts Not sent."""
    if cancel == (choice is not None):
        raise typer.BadParameter("pass exactly one of --choice or --cancel")
    normalized_choice = normalize_choice(choice) if choice is not None else None
    if choice is not None and normalized_choice is None:
        raise typer.BadParameter("--choice must be yes, y or a number from 1 to 3")
    wiring = load_wiring()
    if normalized_choice is None:
        cancel_confirmation(
            conversation=Conversation.slack(thread, channel),
            confirmations=wiring.confirmations,
            reporter=wiring.reporter,
        )
        return
    conversation = Conversation.slack(thread, channel)
    confirm_session(
        conversation=conversation,
        choice=normalized_choice,
        thread_link=lambda: _thread_link(wiring, conversation),
        terminal_host=wiring.terminal_host,
        thread_map=wiring.thread_map,
        inbox_directory=wiring.inbox_directory,
        confirmations=wiring.confirmations,
        reporter=wiring.reporter,
        clock=wiring.clock,
        project_map_source=wiring.project_map_source,
    )


_TARGET_HELP = "session selector: hc address | terminal name | Herdr app space label"


@app.command()
def relay(
    target: str = typer.Argument(..., help=_TARGET_HELP),
    message: str = typer.Argument(..., help="text to send into the target terminal"),
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    item: str = typer.Option(
        ..., "--item", help="the inbox item id (item.id) of the person's message"
    ),
) -> None:
    """Relay a message into a live terminal (pan-owned or external). --item must be its
    sender's newest message pan drained in this thread (otherwise a one-line reply asks to send
    the request again) and hold the target as a whole word or phrase; otherwise nothing is sent
    and it exits 28. A thread from X exits 35."""
    wiring = load_wiring()
    conversation = Conversation.slack(thread, channel)
    refuse_x_source(wiring.thread_map, wiring.drained_log, item, thread)
    require_named_target(
        newest_drained_item(wiring.drained_log, item, conversation),
        action="relay",
        item_id=item,
        target=target,
        requester=None,
        conversation=conversation,
        open_reporter=lambda: wiring.reporter,
    )
    terminal_host = wiring.terminal_host
    resolved = relay_to_session(terminal_host, target, message, terminal_host.list_terminals())
    typer.echo(
        f"relayed into {resolved.name or resolved.where or resolved.address} terminal {resolved.address}"
    )


@app.command()
def read(
    target: str = typer.Argument(..., help=_TARGET_HELP),
    full: bool = typer.Option(False, "--full", help="full transcript instead of recent lines"),  # noqa: FBT001 - Typer, not a caller, passes CLI flags
) -> None:
    """Print a live session's raw output: recent lines, or the full transcript with --full."""
    wiring = load_wiring()
    terminal_host = wiring.terminal_host
    records = wiring.thread_map.records()
    raw = read_session(
        terminal_host,
        records,
        target,
        terminal_host.list_terminals(),
        full=full,
        lines=READ_RECENT_LINES,
        full_history_lines=READ_FULL_HISTORY_LINES,
    )
    # Sanctioned CLI stdout (BR-5): RAW content, never summarized here — the orchestrator
    # owns the summary (INV-3 keeps the CLI deterministic).
    typer.echo(raw)


@app.command()
def stop(thread: str = typer.Option(..., "--thread")) -> None:
    """Kill switch: kill and remove the worker's terminal and mark the ThreadRecord FAILED;
    the thread's status message says Stopped. Refuses for a session pan did not start: it only
    unties the thread and never stops it."""
    wiring = load_wiring()
    stop_thread(
        conversation=conversation_for_id(wiring.thread_map, thread),
        thread_map=wiring.thread_map,
        terminal_host=wiring.terminal_host,
        open_reporter=lambda: wiring.reporter,
        clock=wiring.clock,
    )
    typer.echo(f"stopped {thread}")


@app.command()
def up() -> None:
    """Ensure the orchestrator runs in its hc terminal, mark workers lost while pan was down
    FAILED, and wake an already-running orchestrator once. Safe to run any time."""
    wiring = load_wiring()
    config = wiring.config
    report = bring_up(
        wiring.terminal_host,
        wiring.thread_map,
        terminal=config.orchestrator.terminal,
        cwd=config.orchestrator.cwd,
        command=config.orchestrator.command,
    )
    state = "started" if report.started else "already running"
    typer.echo(
        f"orchestrator {config.orchestrator.terminal} {state} at {report.orchestrator_address}"
    )
    if report.lost_threads:
        typer.echo(
            f"marked {len(report.lost_threads)} lost worker(s) failed: {', '.join(report.lost_threads)}"
        )
    if not report.started and not report.woke:
        typer.echo("could not wake the orchestrator; it will drain on its next wake")


def _run() -> int:
    try:
        app(standalone_mode=False)
    except PanError as error:
        typer.echo(f"error: {type(error).__name__}: {error}", err=True)
        return _exit_code_for(error)
    except typer.Exit as exit_error:
        return int(exit_error.exit_code)
    except typer.Abort:
        typer.echo("aborted", err=True)
        return 130
    except ClickException as error:
        error.show()
        return error.exit_code
    return 0


def main() -> None:
    raise SystemExit(_run())
