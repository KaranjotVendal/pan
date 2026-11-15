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
from pan.adapters.hc import ShellHcAdapter
from pan.config import READ_FULL_HISTORY_LINES, READ_RECENT_LINES, load_config
from pan.credentials import load_credentials, save_credentials
from pan.directive import parse_directive
from pan.errors import (
    ConfigMissingError,
    CredentialsError,
    GatedOpDeniedError,
    HcError,
    InboxError,
    PanError,
    SlackPostError,
    SpawnError,
    TargetAmbiguousError,
    TargetBlockedError,
    TargetNotFoundError,
    TerminalGoneError,
    TerminalNameTakenError,
    ThreadNotFoundError,
    UnauthorizedSenderError,
)
from pan.gateway.app import BoltSlackAdapter
from pan.gateway.slack_post import slack_post
from pan.hooks.notification import notification_hook
from pan.hooks.session_start import session_start_hook
from pan.hooks.stop import stop_hook
from pan.inbox import FileInboxStore
from pan.interface import read_session, relay_to_session
from pan.lifecycle import bring_up
from pan.logging import initialise_logger
from pan.models import PanConfig, SessionSummary, SlackCredentials, WorkerStatus
from pan.seams import SlackAdapter, ThreadMap
from pan.sessions import collect_sessions, worker_status_for
from pan.spawn import ClaudeLauncher, reopen_worker, spawn_worker
from pan.threadmap import FileThreadMap
from pan.watcher import WatchdogInboxWatcher

logger = initialise_logger(__name__)

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="pan — Slack coding orchestrator"
)
config_app = typer.Typer(no_args_is_help=True, help="Manage config and credentials")
inbox_app = typer.Typer(no_args_is_help=True, help="Inbox queue operations")
threads_app = typer.Typer(no_args_is_help=True, help="Thread-map operations")
hook_app = typer.Typer(no_args_is_help=True, help="Claude Code worker hook entrypoints")
app.add_typer(config_app, name="config")
app.add_typer(inbox_app, name="inbox")
app.add_typer(threads_app, name="threads")
app.add_typer(hook_app, name="hook")

# Documented taxonomy -> exit-code table. Unclassified exceptions are defects and
# surface as tracebacks (Principle 4), never mapped here.
_EXIT_CODES: dict[type[PanError], int] = {
    UnauthorizedSenderError: 10,
    ConfigMissingError: 11,
    CredentialsError: 12,
    InboxError: 13,
    ThreadNotFoundError: 14,
    SpawnError: 15,
    HcError: 16,
    SlackPostError: 17,
    GatedOpDeniedError: 18,
    TargetNotFoundError: 20,
    TargetAmbiguousError: 21,
    TerminalGoneError: 22,
    TargetBlockedError: 23,
    TerminalNameTakenError: 24,
}


def _exit_code_for(error: PanError) -> int:
    return _EXIT_CODES.get(type(error), 1)


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
    WatchdogInboxWatcher(ShellHcAdapter(), config.orchestrator.terminal, config.paths.inbox).start()


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


@inbox_app.command("drain")
def inbox_drain(as_json: bool = typer.Option(False, "--json")) -> None:
    """Atomically drain and clear the inbox; emit the items (JSON for the orchestrator)."""
    config = _config()
    items = FileInboxStore(config.paths.inbox).drain()
    # Attach the deterministically-parsed directive to each item so the orchestrator
    # reads the mode/cleaned_text from here (parse_directive runs in code) rather than
    # re-deriving it by judgment — enforcing INV-3.
    if as_json:
        payload = [
            {
                "item": item.model_dump(mode="json"),
                "directive": parse_directive(item.raw_text).model_dump(mode="json"),
            }
            for item in items
        ]
        typer.echo(json.dumps(payload))
        return
    for item in items:
        directive = parse_directive(item.raw_text)
        typer.echo(f"{item.id} {item.channel} {item.thread_ts} {directive.mode.value}")


@app.command()
def spawn(
    thread: str = typer.Option(..., "--thread"),
    task: str = typer.Option(..., "--task"),
    channel: str = typer.Option(..., "--channel"),
    stream: str | None = typer.Option(None, "--stream"),
    repo: Path | None = typer.Option(None, "--repo"),
) -> None:
    """Create worktree + hc terminal + worker session; record a ThreadRecord; post the ack."""
    config = _config()
    credentials = load_credentials(config.paths.credentials)
    clock = SystemClock()
    hc = ShellHcAdapter()
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
        hc=hc,
        launcher=ClaudeLauncher(hc),
        thread_map=FileThreadMap(config.paths.threads, clock),
        slack=BoltSlackAdapter(credentials, config, FileInboxStore(config.paths.inbox), clock),
        clock=clock,
        id_gen=UuidGen(),
    )


@app.command()
def reopen(
    thread: str = typer.Option(..., "--thread"),
    task: str = typer.Option(..., "--task"),
) -> None:
    """Reopen a thread's finished or lost worker in its existing worktree, continuing its
    last Claude conversation with the follow-up task; post the ack."""
    config = _config()
    clock = SystemClock()
    thread_map = FileThreadMap(config.paths.threads, clock)
    record = thread_map.get(thread)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread}")
    credentials = load_credentials(config.paths.credentials)
    hc = ShellHcAdapter()
    # reopen_worker posts its own failure notice and re-raises SpawnError; this boundary
    # only maps the exit code.
    reopen_worker(
        record=record,
        task=task,
        app_space=config.orchestrator.app_space,
        hc=hc,
        launcher=ClaudeLauncher(hc),
        thread_map=thread_map,
        slack=BoltSlackAdapter(credentials, config, FileInboxStore(config.paths.inbox), clock),
        clock=clock,
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
    record = thread_map.get_by_worktree(Path(cwd)) if cwd is not None else None
    if record is None:
        logger.warning("completion hook: no thread record for cwd")
        return
    hook_fn(
        record.thread_ts,
        record.channel,
        thread_map,
        slack,
        stdin=io.StringIO(raw_stdin),
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
    record = thread_map.get_by_worktree(Path(cwd)) if cwd is not None else None
    if record is None:
        logger.warning("session-start hook: no thread record for cwd")
        return
    session_start_hook(record.thread_ts, thread_map, stdin=io.StringIO(raw_stdin))


@hook_app.command("stop")
def hook_stop() -> None:
    """Claude Code Stop hook: post the worker's final summary and mark the thread DONE."""
    _run_completion_hook(stop_hook)


@hook_app.command("notification")
def hook_notification() -> None:
    """Claude Code Notification hook: post the worker's question and mark the thread BLOCKED."""
    _run_completion_hook(notification_hook)


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
            terminal = ShellHcAdapter().get(record.terminal_address)
        except TerminalGoneError:
            terminal = None
    typer.echo(worker_status_for(terminal, record.status).value)


def _render_sessions_table(summaries: list[SessionSummary]) -> str:
    header = f"{'TERMINAL':<9} {'NAME':<24} {'AGENT':<9} {'OWNER':<9} {'PAN_STATUS':<11} DRIFT"
    lines = [header]
    for summary in summaries:
        owner = "pan" if summary.is_pan_owned else "external"
        pan_status = summary.pan_status.value if summary.pan_status is not None else "-"
        display_name = summary.name or summary.where or summary.cwd.name
        drift = "DRIFT" if summary.drift else ""
        lines.append(
            f"{summary.address:<9} {display_name:<24} "
            f"{summary.agent_status.value:<9} {owner:<9} {pan_status:<11} {drift}"
        )
    return "\n".join(lines)


@app.command()
def sessions(as_json: bool = typer.Option(False, "--json")) -> None:
    """List all live hc terminals, reconciled against the pan thread map, flagging status
    drift. --json emits the SessionSummary list for the orchestrator."""
    config = _config()
    summaries = collect_sessions(
        ShellHcAdapter(), FileThreadMap(config.paths.threads, SystemClock())
    )
    if as_json:
        typer.echo(json.dumps([summary.model_dump(mode="json") for summary in summaries]))
        return
    typer.echo(_render_sessions_table(summaries))


_TARGET_HELP = "session selector: hc address | terminal name | Herdr app space label"


@app.command()
def relay(
    target: str = typer.Argument(..., help=_TARGET_HELP),
    message: str = typer.Argument(..., help="text to send into the target terminal"),
) -> None:
    """Relay a message into a live hc terminal (pan-owned or external)."""
    hc = ShellHcAdapter()
    resolved = relay_to_session(hc, target, message, hc.list_terminals())
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
    hc = ShellHcAdapter()
    records = FileThreadMap(config.paths.threads, SystemClock()).records()
    raw = read_session(
        hc,
        records,
        target,
        hc.list_terminals(),
        full=full,
        lines=READ_RECENT_LINES,
        full_history_lines=READ_FULL_HISTORY_LINES,
    )
    # Sanctioned CLI stdout (BR-5): RAW content, never summarized here — the orchestrator
    # owns the summary (INV-3 keeps the CLI deterministic).
    typer.echo(raw)


@app.command()
def stop(thread: str = typer.Option(..., "--thread")) -> None:
    """Kill switch: kill and remove the worker's terminal and mark the ThreadRecord FAILED."""
    config = _config()
    thread_map = FileThreadMap(config.paths.threads, SystemClock())
    record = thread_map.get(thread)
    if record is None:
        raise ThreadNotFoundError(f"no thread record for thread_ts={thread}")
    if record.terminal_address:
        ShellHcAdapter().remove(record.terminal_address)
    thread_map.update_status(thread, WorkerStatus.FAILED)
    typer.echo(f"stopped {thread}")


@app.command()
def up() -> None:
    """Ensure the orchestrator runs in its hc terminal, mark workers lost while pan was down
    FAILED, and wake an already-running orchestrator once. Safe to run any time."""
    config = _config()
    report = bring_up(
        ShellHcAdapter(),
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
