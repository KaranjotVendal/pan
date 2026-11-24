from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path

import typer
from pydantic import SecretStr

# Typer 0.27 vendors click; its exception base lives here (no standalone click dep).
from typer._click.exceptions import ClickException

from pan.adapters.clock import SystemClock, UuidGen
from pan.adapters.git_worktree import ShellGitWorktreeAdapter
from pan.adapters.launchd import build_service_manager
from pan.adapters.plannotator_inbox import ShellPlannotatorInboxAdapter
from pan.adapters.terminal_host import build_terminal_host
from pan.config import CODE_FENCE, READ_FULL_HISTORY_LINES, READ_RECENT_LINES, load_config
from pan.confirmations import (
    FilePendingConfirmations,
    OpenQuestions,
    cancel_confirmation,
    confirm_session,
    confirmations_path,
    propose_sessions,
)
from pan.credentials import load_credentials, save_credentials
from pan.directive import (
    CONFIRM_NO,
    parse_confirm_reply,
    parse_directive,
    parse_route,
    route_for_source,
)
from pan.errors import (
    ConfigMissingError,
    CredentialsError,
    GatedOpDeniedError,
    GitWorktreeError,
    InboxError,
    NoExactMatchError,
    NotPanSessionError,
    PanError,
    ServiceError,
    SessionBusyError,
    SlackPostError,
    SpawnError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
    TerminalGoneError,
    TerminalHostError,
    TerminalNameTakenError,
    ThreadNotFoundError,
    UnauthorizedSenderError,
    UnsafeWorktreeError,
    UnsavedWorkError,
)
from pan.gateway.app import BoltSlackAdapter
from pan.gateway.slack_post import slack_post, slack_post_parts
from pan.hooks.inbox_link import inbox_link_hook
from pan.hooks.notification import notification_hook
from pan.hooks.session_start import session_start_hook
from pan.hooks.stop import stop_hook
from pan.inbox import FileInboxStore
from pan.interface import read_session, relay_to_session
from pan.lifecycle import bring_up
from pan.logging import initialise_logger
from pan.models import (
    InboxItem,
    PanConfig,
    Route,
    SessionSummary,
    SlackCredentials,
    StatusState,
    ThreadRecord,
    WorkerStatus,
)
from pan.seams import SlackAdapter, ThreadMap
from pan.service import (
    install_services,
    resolve_pan_binary,
    service_statuses,
    uninstall_services,
)
from pan.session_control import (
    check_thread,
    done_thread,
    follow_up_to_session,
    forget_thread,
    send_to_session,
)
from pan.sessions import collect_sessions, worker_status_for
from pan.spawn import ClaudeLauncher, reopen_worker, spawn_worker
from pan.status_message import publish_status
from pan.threadmap import FileThreadMap, tie_note
from pan.watcher import WatchdogInboxWatcher

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
    GatedOpDeniedError: 18,
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
}


def _exit_code_for(error: PanError) -> int:
    # The most specific documented class wins, so an adapter's own subclass (the hc
    # adapter's HcError) maps through its neutral base (TerminalHostError).
    for error_class in type(error).__mro__:
        exit_code = _EXIT_CODES.get(error_class)
        if exit_code is not None:
            return exit_code
    return 1


def _config() -> PanConfig:
    return load_config()


@app.command()
def gateway() -> None:
    """Run the always-on Bolt Socket Mode gateway (blocking; launchd-supervised)."""
    config = _config()
    credentials = load_credentials(config.paths.credentials)
    adapter = BoltSlackAdapter(
        credentials, config, FileInboxStore(config.paths.inbox), SystemClock()
    )
    adapter.start()


@app.command()
def watcher() -> None:
    """Run the inbox watcher that nudges the orchestrator (blocking; launchd-supervised)."""
    config = _config()
    WatchdogInboxWatcher(
        build_terminal_host(config.terminal_host), config.orchestrator.terminal, config.paths.inbox
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
    config = _config()
    save_credentials(
        SlackCredentials(bot_token=SecretStr(bot_token), app_token=SecretStr(app_token)),
        config.paths.credentials,
    )
    typer.echo("credentials saved")


@config_app.command("show")
def config_show() -> None:
    """Print the resolved config (credentials masked)."""
    config = _config()
    typer.echo(config.model_dump_json(indent=2))
    try:
        credentials = load_credentials(config.paths.credentials)
    except CredentialsError:
        typer.echo("credentials: not set")
        return
    # SecretStr fields serialize masked ("**********"); get_secret_value is never called here.
    typer.echo(credentials.model_dump_json())


def _item_route(item: InboxItem) -> Route:
    # The route the gateway parsed (parsed here for an item an older gateway wrote), held to
    # the routes the item's source may use.
    route = item.route if item.route is not None else parse_route(item.raw_text, item.source)
    return route_for_source(route, item.source)


def _confirmations(config: PanConfig) -> FilePendingConfirmations:
    return FilePendingConfirmations(confirmations_path(config.paths.threads))


@inbox_app.command("drain")
def inbox_drain(as_json: bool = typer.Option(False, "--json")) -> None:
    """Atomically drain and clear the inbox; emit the items (JSON for the orchestrator)."""
    config = _config()
    # The open questions are read before the drain, and reading them never fails, so a bad
    # confirmations file can never cost drained items.
    open_questions = OpenQuestions(_confirmations(config).all_pending())
    clock = SystemClock()
    items = FileInboxStore(config.paths.inbox).drain()
    thread_map = FileThreadMap(config.paths.threads, clock)

    # Attach the route the gateway parsed (parsed here for an item an older gateway wrote)
    # and the directive parsed from the route's text, so the orchestrator reads them as
    # facts (both pure code) rather than re-deriving them by judgment — enforcing INV-3. The
    # thread's tie is attached as a plain-words fact, read now because it can change after
    # the gateway wrote the item. So is pan's open question in the thread (`pan propose`): a
    # `yes`/1-3 reply to it becomes a `confirm` route, and any other message is marked
    # `cancels_confirmation`.
    def routed(item: InboxItem) -> tuple[Route, bool]:
        return open_questions.route(
            _item_route(item), item.source, item.thread_ts, item.raw_text, clock.now()
        )

    if as_json:
        payload = []
        for item in items:
            route, cancels_confirmation = routed(item)
            record = thread_map.get(item.thread_ts)
            note = tie_note(record)
            tie = None
            if record is not None and note is not None:
                tie = {
                    "session": record.terminal_name,
                    "terminal_address": record.terminal_address,
                    "status": record.status.value,
                    "note": note,
                }
            payload.append(
                {
                    "item": item.model_dump(mode="json", exclude={"route"}),
                    "route": route.model_dump(mode="json"),
                    "directive": parse_directive(route.text).model_dump(mode="json"),
                    "tie": tie,
                    "cancels_confirmation": cancels_confirmation,
                }
            )
        typer.echo(json.dumps(payload))
        return
    for item in items:
        route, cancels_confirmation = routed(item)
        directive = parse_directive(route.text)
        typer.echo(
            f"{item.id} {item.channel} {item.thread_ts} {route.kind.value} {directive.mode.value}"
        )
        if cancels_confirmation:
            typer.echo("  cancels the open confirmation question in this thread")
        note = tie_note(thread_map.get(item.thread_ts))
        if note is not None:
            typer.echo(f"  {note}")


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
    reply_in_slack: bool = typer.Option(
        False, "--reply-in-slack", help="post the answer in Slack at every turn end"
    ),
    thread_reply: bool = typer.Option(
        False,
        "--thread-reply",
        help="the message was a reply inside a Slack thread; the brief says to read the thread",
    ),
) -> None:
    """Create worktree + hc terminal + worker session; tie the thread to it; post the start
    notice naming its Inbox thread. With --thread-reply the brief tells the session to read the
    Slack thread first with its own Slack read-thread tool."""
    config = _config()
    credentials = load_credentials(config.paths.credentials)
    clock = SystemClock()
    terminal_host = build_terminal_host(config.terminal_host)
    # spawn_worker already posts the failure notice and records a FAILED ThreadRecord,
    # then re-raises SpawnError, so this boundary only maps the exit code (no double post).
    spawn_worker(
        thread_ts=thread,
        channel=channel,
        task=task,
        repo=repo if repo is not None else Path.cwd(),
        base=config.orchestrator.worktree_base,
        stream=stream,
        app_space=config.orchestrator.app_space,
        git=ShellGitWorktreeAdapter(),
        terminal_host=terminal_host,
        launcher=ClaudeLauncher(terminal_host),
        thread_map=FileThreadMap(config.paths.threads, clock),
        slack=BoltSlackAdapter(credentials, config, FileInboxStore(config.paths.inbox), clock),
        clock=clock,
        id_gen=UuidGen(),
        requester=requester,
        request_text=request_text,
        reply_in_slack=reply_in_slack,
        from_thread_reply=thread_reply,
    )


@app.command()
def reopen(
    thread: str = typer.Option(..., "--thread"),
    task: str = typer.Option(..., "--task"),
    reply_in_slack: bool = typer.Option(
        False,
        "--reply-in-slack",
        help="switch the thread to posting the answer in Slack at every turn end",
    ),
) -> None:
    """Reopen a thread's finished or lost worker in its existing worktree, continuing its
    last Claude conversation with the follow-up task; post the ack. --reply-in-slack switches
    the thread to posting answers in Slack (never back)."""
    config = _config()
    clock = SystemClock()
    thread_map = FileThreadMap(config.paths.threads, clock)
    record = thread_map.get(thread)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread}")
    credentials = load_credentials(config.paths.credentials)
    terminal_host = build_terminal_host(config.terminal_host)
    # reopen_worker posts its own failure notice and re-raises SpawnError; this boundary
    # only maps the exit code.
    reopen_worker(
        record=record,
        task=task,
        app_space=config.orchestrator.app_space,
        terminal_host=terminal_host,
        launcher=ClaudeLauncher(terminal_host),
        thread_map=thread_map,
        slack=BoltSlackAdapter(credentials, config, FileInboxStore(config.paths.inbox), clock),
        clock=clock,
        reply_in_slack=reply_in_slack,
    )


@threads_app.command("get")
def threads_get(thread: str = typer.Option(..., "--thread")) -> None:
    """Print the ThreadRecord for a thread_ts as JSON."""
    config = _config()
    record = FileThreadMap(config.paths.threads, SystemClock()).get(thread)
    typer.echo(record.model_dump_json() if record is not None else "null")


@threads_app.command("set")
def threads_set(
    thread: str = typer.Option(..., "--thread"),
    status: WorkerStatus = typer.Option(..., "--status"),
) -> None:
    """Update a ThreadRecord's status."""
    config = _config()
    FileThreadMap(config.paths.threads, SystemClock()).update_status(thread, status)
    typer.echo(f"{thread} -> {status.value}")


@app.command("slack-post")
def slack_post_command(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    text: str = typer.Option(..., "--text"),
) -> None:
    """Post text to a Slack thread via the one egress path (INV-4)."""
    config = _config()
    credentials = load_credentials(config.paths.credentials)
    adapter = BoltSlackAdapter(
        credentials, config, FileInboxStore(config.paths.inbox), SystemClock()
    )
    slack_post(adapter, channel, thread, text)


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
    slack: SlackAdapter,
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
        record.thread_ts,
        record.channel,
        thread_map,
        slack,
        stdin=io.StringIO(raw_stdin),
        session_name=record.terminal_name,
    )


def _run_completion_hook(hook_fn: Callable[..., None]) -> None:
    raw_stdin = sys.stdin.read()
    config = _config()
    thread_map = FileThreadMap(config.paths.threads, SystemClock())
    credentials = load_credentials(config.paths.credentials)
    slack = BoltSlackAdapter(credentials, config, FileInboxStore(config.paths.inbox), SystemClock())
    _dispatch_completion_hook(hook_fn, raw_stdin, thread_map, slack)


@hook_app.command("session-start")
def hook_session_start() -> None:
    """Claude Code SessionStart hook: record the worker's session id and transcript path."""
    raw_stdin = sys.stdin.read()
    config = _config()
    thread_map = FileThreadMap(config.paths.threads, SystemClock())
    cwd = _extract_cwd(raw_stdin)
    if cwd is None:
        logger.warning("session-start hook: the payload carries no cwd")
        return
    record = thread_map.get_by_worktree(Path(cwd).expanduser())
    if record is None:
        logger.warning(f"session-start hook: no thread record for cwd={cwd}")
        return
    session_start_hook(
        record.thread_ts,
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
        record.thread_ts,
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
        config = _config()
        _dispatch_inbox_link_hook(raw_stdin, FileThreadMap(config.paths.threads, SystemClock()))
    except Exception as e:
        logger.warning(f"inbox-link hook failed: {type(e).__name__}")


@app.command()
def status(thread: str = typer.Option(..., "--thread")) -> None:
    """Report a worker's live status from its hc terminal (resolved via its thread record)."""
    config = _config()
    record = FileThreadMap(config.paths.threads, SystemClock()).get(thread)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread}")
    terminal = None
    if record.terminal_address:
        try:
            terminal = build_terminal_host(config.terminal_host).get(record.terminal_address)
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
    as_json: bool = typer.Option(False, "--json"),
    thread: str | None = typer.Option(None, "--thread", help="post the list to this thread"),
    channel: str | None = typer.Option(None, "--channel", help="the thread's channel"),
) -> None:
    """List all live hc terminals with the last thing each was asked, reconciled against the
    pan thread map, flagging status drift. --json emits the SessionSummary list; --thread and
    --channel post the table to that Slack thread as is."""
    config = _config()
    summaries = collect_sessions(
        build_terminal_host(config.terminal_host),
        FileThreadMap(config.paths.threads, SystemClock()),
    )
    if as_json:
        typer.echo(json.dumps([summary.model_dump(mode="json") for summary in summaries]))
        return
    table = _render_sessions_table(summaries)
    if thread is not None and channel is not None:
        slack_post_parts(
            _slack_adapter(config), channel, thread, f"{CODE_FENCE}\n{table}\n{CODE_FENCE}"
        )
        return
    typer.echo(table)


def _slack_adapter(config: PanConfig) -> BoltSlackAdapter:
    credentials = load_credentials(config.paths.credentials)
    return BoltSlackAdapter(credentials, config, FileInboxStore(config.paths.inbox), SystemClock())


@app.command()
def check(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
) -> None:
    """Post the thread's tied session's latest answer to the thread, as is: the last answer in
    its transcript, or its recent screen lines. Names its Inbox thread (looked up by name for a
    session pan did not start) and says if it is still working or waiting on a prompt."""
    config = _config()
    check_thread(
        thread_ts=thread,
        channel=channel,
        thread_map=FileThreadMap(config.paths.threads, SystemClock()),
        terminal_host=build_terminal_host(config.terminal_host),
        inbox_directory=ShellPlannotatorInboxAdapter(),
        slack=_slack_adapter(config),
        clock=SystemClock(),
        screen_lines=READ_RECENT_LINES,
    )


@app.command()
def forget(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
) -> None:
    """Untie the thread from its session and say so in the thread. Never stops the session."""
    config = _config()
    forget_thread(
        thread_ts=thread,
        channel=channel,
        thread_map=FileThreadMap(config.paths.threads, SystemClock()),
        slack=_slack_adapter(config),
    )


@app.command()
def done(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    discard: bool = typer.Option(
        False, "--discard", help="close it even if uncommitted files or unpushed commits are lost"
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
    it still deletes the branch. A session pan did not start is only untied, never closed."""
    config = _config()
    done_thread(
        thread_ts=thread,
        channel=channel,
        discard=discard,
        worktree_base=config.orchestrator.worktree_base,
        fallback_repo=config.orchestrator.cwd,
        thread_map=FileThreadMap(config.paths.threads, SystemClock()),
        terminal_host=build_terminal_host(config.terminal_host),
        git=ShellGitWorktreeAdapter(),
        slack=_slack_adapter(config),
        clock=SystemClock(),
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
    task: str = typer.Option(..., "--task"),
    requester: str | None = typer.Option(None, "--requester", help="the sender's Slack user id"),
    reply_in_slack: bool = typer.Option(
        False, "--reply-in-slack", help="post the answer in Slack at every turn end"
    ),
) -> None:
    """Send a task to a live session (pan's own or one of yours) and tie the thread to it; post
    a notice naming its Inbox thread. --to must name one session exactly: its address, name, or
    Herdr label or space. With no exact match (or a label several sessions share) nothing is
    sent or posted and it exits 28: propose a session with pan propose. Without --to, send a
    follow-up to the thread's tied session; --reply-in-slack then switches the thread to posting
    answers in Slack (never back). A busy or blocked session gets a short reply and
    nothing is typed; a session pan did not start gets one line on top saying the text came
    from Slack and which Inbox thread to use."""
    config = _config()
    clock = SystemClock()
    thread_map = FileThreadMap(config.paths.threads, clock)
    terminal_host = build_terminal_host(config.terminal_host)
    if to is None:
        follow_up_to_session(
            thread_ts=thread,
            channel=channel,
            text=task,
            terminal_host=terminal_host,
            thread_map=thread_map,
            slack=_slack_adapter(config),
            clock=clock,
            reply_in_slack=reply_in_slack,
        )
        return
    send_to_session(
        thread_ts=thread,
        channel=channel,
        selector=to,
        text=task,
        requester=requester,
        reply_in_slack=reply_in_slack,
        terminal_host=terminal_host,
        thread_map=thread_map,
        inbox_directory=ShellPlannotatorInboxAdapter(),
        slack=_slack_adapter(config),
        clock=clock,
    )


@app.command()
def propose(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    to: str = typer.Option(..., "--to", help="the selector the person wrote after to"),
    task: str = typer.Option(..., "--task"),
    candidate: list[str] = typer.Option(
        [],
        "--candidate",
        help="a proposed session's terminal address; repeat for up to 3, most likely first",
    ),
    requester: str | None = typer.Option(None, "--requester", help="the sender's Slack user id"),
    reply_in_slack: bool = typer.Option(
        False, "--reply-in-slack", help="post the answer in Slack at every turn end"
    ),
) -> None:
    """Ask the person to confirm where a to <name>: task goes when pan send found no exact
    match. One candidate: "Send this to <session>? Reply yes."; several: a numbered list to
    pick 1-3 from. Nothing is typed into any session until they reply. Candidates that are not
    live terminal addresses are dropped; with none left it says no session matches."""
    config = _config()
    propose_sessions(
        thread_ts=thread,
        channel=channel,
        selector=to,
        task=task,
        requester=requester,
        reply_in_slack=reply_in_slack,
        candidate_addresses=candidate,
        terminal_host=build_terminal_host(config.terminal_host),
        confirmations=_confirmations(config),
        slack=_slack_adapter(config),
        clock=SystemClock(),
    )


@app.command()
def confirm(
    thread: str = typer.Option(..., "--thread"),
    channel: str = typer.Option(..., "--channel"),
    choice: str | None = typer.Option(
        None, "--choice", help="the reply to the open question: yes, 1 to 3, or no"
    ),
    cancel: bool = typer.Option(
        False, "--cancel", help="drop the open question and post Not sent."
    ),
) -> None:
    """Act on the person's reply to the thread's open question from pan propose. --choice sends
    the stored task to the chosen session (busy and prompt refusals still apply) and ties the
    thread to it; --cancel (or --choice no) drops the question and posts Not sent."""
    if cancel == (choice is not None):
        raise typer.BadParameter("pass exactly one of --choice or --cancel")
    # The same reading as a Slack reply: any case, surrounding spaces, y for yes, n for no.
    normalized_choice = parse_confirm_reply(choice) if choice is not None else None
    if choice is not None and normalized_choice is None:
        raise typer.BadParameter("--choice must be yes, y, a number from 1 to 3, or no")
    config = _config()
    clock = SystemClock()
    if normalized_choice is None or normalized_choice == CONFIRM_NO:
        cancel_confirmation(
            thread_ts=thread,
            channel=channel,
            confirmations=_confirmations(config),
            slack=_slack_adapter(config),
        )
        return
    confirm_session(
        thread_ts=thread,
        channel=channel,
        choice=normalized_choice,
        terminal_host=build_terminal_host(config.terminal_host),
        thread_map=FileThreadMap(config.paths.threads, clock),
        inbox_directory=ShellPlannotatorInboxAdapter(),
        confirmations=_confirmations(config),
        slack=_slack_adapter(config),
        clock=clock,
    )


_TARGET_HELP = "session selector: hc address | terminal name | Herdr app space label"


@app.command()
def relay(
    target: str = typer.Argument(..., help=_TARGET_HELP),
    message: str = typer.Argument(..., help="text to send into the target terminal"),
) -> None:
    """Relay a message into a live terminal (pan-owned or external)."""
    config = _config()
    terminal_host = build_terminal_host(config.terminal_host)
    resolved = relay_to_session(terminal_host, target, message, terminal_host.list_terminals())
    typer.echo(
        f"relayed into {resolved.name or resolved.where or resolved.address} terminal {resolved.address}"
    )


@app.command()
def read(
    target: str = typer.Argument(..., help=_TARGET_HELP),
    full: bool = typer.Option(False, "--full", help="full transcript instead of recent lines"),
) -> None:
    """Print a live session's raw output: recent lines, or the full transcript with --full."""
    config = _config()
    terminal_host = build_terminal_host(config.terminal_host)
    records = FileThreadMap(config.paths.threads, SystemClock()).records()
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
    config = _config()
    clock = SystemClock()
    thread_map = FileThreadMap(config.paths.threads, clock)
    record = thread_map.get(thread)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread}")
    if not record.started_by_pan:
        if record.tied:
            thread_map.untie(thread)
        raise NotPanSessionError(
            f"{record.terminal_name} was not started by pan, so pan never stops it; "
            "this thread is untied from it"
        )
    if record.terminal_address:
        build_terminal_host(config.terminal_host).remove(record.terminal_address)
    thread_map.update_status(thread, WorkerStatus.FAILED, session_name=record.terminal_name)
    if record.tied:
        _publish_stopped(config, thread_map, record, clock)
    typer.echo(f"stopped {thread}")


def _publish_stopped(
    config: PanConfig, thread_map: ThreadMap, record: ThreadRecord, clock: SystemClock
) -> None:
    # Best effort: the kill switch has done its work whether or not Slack can be reached.
    try:
        slack = _slack_adapter(config)
    except CredentialsError as e:
        logger.warning(f"stop could not update the status message: {type(e).__name__}")
        return
    publish_status(
        slack=slack, thread_map=thread_map, record=record, state=StatusState.STOPPED, clock=clock
    )


@app.command()
def up() -> None:
    """Ensure the orchestrator runs in its hc terminal, mark workers lost while pan was down
    FAILED, and wake an already-running orchestrator once. Safe to run any time."""
    config = _config()
    report = bring_up(
        build_terminal_host(config.terminal_host),
        FileThreadMap(config.paths.threads, SystemClock()),
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


@app.command()
def pause() -> None:
    """Toggle the pause flag. (Design intent: paused → new tasks acked 'paused' and dropped,
    running work untouched. The gateway does not yet consult the flag — see TODOS.)"""
    config = _config()
    flag = config.paths.threads.parent / "paused.flag"
    if flag.exists():
        flag.unlink()
        typer.echo("resumed")
        return
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("")
    typer.echo("paused")


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
