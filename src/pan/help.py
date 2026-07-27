from __future__ import annotations

import re
from collections.abc import Mapping

import typer.main
from pydantic import BaseModel

# Typer 0.27 vendors click, so the Command base lives here (no standalone click dep); the
# Rich-aware subclasses Typer builds the tree from live in typer.core.
from typer._click.core import Command
from typer.core import TyperCommand, TyperGroup

from pan.config import (
    HELP_ECHOED_COMMAND_MAX_LENGTH,
    HELP_FLAG_TOKENS,
    HELP_REQUEST_TOKENS,
    HELP_USER_FACING_COMMANDS,
    SLACK_DIRECTIVE_FLAGS_FOOTER,
)
from pan.directive import _LEADING_MENTION, _normalize_punctuation
from pan.logging import initialise_logger

logger = initialise_logger(__name__)

# The program name every rendered usage line is written against. Passed as the context's
# info_name for a named subcommand too (`pan relay`), because using the bare command name with
# the group as parent renders the usage line as `pan pan relay`.
_PROGRAM_NAME = "pan"
# A help request is a help token alone, or a help token plus at most one command word.
_MAX_HELP_TOKENS = 2
# Everything a real command name can contain. The unknown-command notice echoes a
# Slack-supplied token, so every other character is dropped before it is posted: a backtick
# run would break out of the gateway's code fence, and a Slack entity (`<!channel>`) survives
# the fence unescaped and would ping the channel.
_UNECHOABLE_CHARACTERS = re.compile(r"[^A-Za-z0-9._-]+")


class HelpRequest(BaseModel, frozen=True):
    # None -> the top-level listing; otherwise the requested command name, validated by
    # render_cli_help against the real command set (unknown -> top-level fallback).
    command: str | None


def parse_help_request(raw_text: str) -> HelpRequest | None:
    # Pure and deterministic (INV-3): a fixed token set plus the two normalization rules
    # reused from directive.py, never model judgment.
    normalized_text = _normalize_punctuation(raw_text.strip())
    normalized_text = _LEADING_MENTION.sub("", normalized_text, count=1)

    tokens = [token.lower() for token in normalized_text.split()]
    if not tokens or len(tokens) > _MAX_HELP_TOKENS:
        # A longer message is a real task brief; one that merely mentions `--help` is not a
        # help request and must still reach the inbox.
        return None

    if len(tokens) == 1:
        return HelpRequest(command=None) if tokens[0] in HELP_REQUEST_TOKENS else None

    first_token, second_token = tokens
    if first_token in HELP_REQUEST_TOKENS:
        # `help relay`, `? relay` — and `help --help`, which names no command.
        second_is_help = second_token in HELP_REQUEST_TOKENS
        return HelpRequest(command=None if second_is_help else second_token)
    if second_token in HELP_FLAG_TOKENS:
        # `relay --help`. Only the flag forms count in trailing position: a trailing bare
        # `help`/`?` (`deploy ?`) is prose, and answering it would swallow a real task.
        return HelpRequest(command=first_token)
    return None


def _echoable(command_path: str) -> str:
    # Reduce a Slack-supplied token to a real-command-name charset and bound its length, so
    # the notice cannot break the gateway's fence, smuggle a Slack entity, or echo a
    # multi-kilobyte message back into the thread.
    return _UNECHOABLE_CHARACTERS.sub("", command_path)[:HELP_ECHOED_COMMAND_MAX_LENGTH]


def _subcommands(command: Command) -> Mapping[str, Command]:
    return command.commands if isinstance(command, TyperGroup) else {}


def _force_plain_text(command: Command) -> None:
    # Typer's default help is Rich: boxed, ANSI-decorated, and get_help() returns "" because
    # Rich prints straight to a console — unusable inside a Slack code block. With
    # rich_markup_mode disabled, Typer's format_help falls back to Click's plain formatter.
    if isinstance(command, TyperCommand | TyperGroup):
        command.rich_markup_mode = None
    for subcommand in _subcommands(command).values():
        _force_plain_text(subcommand)


def _plain_help(command: Command, info_name: str) -> str:
    context = command.make_context(info_name, [], resilient_parsing=True)
    return command.get_help(context)


def _render_top_level(group: Command) -> str:
    # Drop the launchd/dev-only commands from the Slack listing. This mutates only this
    # call's fresh tree, never the shared module-global app.
    if isinstance(group, TyperGroup):
        group.commands = {
            name: subcommand
            for name, subcommand in group.commands.items()
            if name in HELP_USER_FACING_COMMANDS
        }
    listing = _plain_help(group, _PROGRAM_NAME).rstrip()
    # The Slack-only directive grammar is invisible to the generator (R-1), so it is appended.
    return f"{listing}\n\n{SLACK_DIRECTIVE_FLAGS_FOOTER}"


def render_cli_help(command_path: str | None = None) -> str:
    # Lazy import: at module-import time help.py must not pull cli.py, or the cycle
    # gateway/app.py -> help.py -> cli.py -> gateway/app.py closes. By call time (inside
    # handle_event) cli is already fully loaded.
    from pan.cli import app

    # A fresh command tree per call keeps the renderer pure — the plain-text switch and the
    # top-level filtering below mutate this local copy only.
    group = typer.main.get_command(app)
    _force_plain_text(group)

    if command_path is None:
        return _render_top_level(group)

    subcommand = _subcommands(group).get(command_path)
    if subcommand is None:
        # A phone typo degrades to the listing rather than an error; render_cli_help is total.
        safe_name = _echoable(command_path)
        logger.info(f"help requested for unknown command name={safe_name}")
        notice = f"unknown command: {safe_name}" if safe_name else "unknown command"
        return f"{notice}\n\n{_render_top_level(group)}"
    return _plain_help(subcommand, f"{_PROGRAM_NAME} {command_path}")
