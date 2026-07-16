from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from pydantic import BaseModel, Field

from pan.errors import GatedOpDeniedError
from pan.gateway.slack_post import slack_post
from pan.logging import initialise_logger
from pan.seams import SlackAdapter

logger = initialise_logger(__name__)


class PreToolUsePayload(BaseModel, frozen=True):
    session_id: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = Field(default_factory=dict)
    hook_event_name: str = ""


def _emit_decision(stdout: TextIO, *, allow: bool, reason: str) -> None:
    # Claude Code reads this PreToolUse decision JSON from stdout to allow or block
    # the pending tool call. Emitting to stdout is the sanctioned exception to the
    # no-stdout rule (BR-5) for a decision-emitting hook.
    decision = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allow else "deny",
            "permissionDecisionReason": reason,
        }
    }
    stdout.write(json.dumps(decision))
    # Flush so the decision reaches Claude Code even when the caller raises
    # immediately afterwards (the deny path).
    stdout.flush()


def pretooluse_gate(
    gated_ops: list[str],
    thread_ts: str,
    channel: str,
    slack: SlackAdapter,
    decide: Callable[[str], bool],
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
) -> None:
    payload = PreToolUsePayload.model_validate_json(stdin.read())
    command = str(payload.tool_input.get("command", ""))

    matched_op = next((op for op in gated_ops if op in command), None)
    if matched_op is None:
        # v1 default: gated_ops is empty, so nothing is gated — allow, touch no Slack.
        _emit_decision(stdout, allow=True, reason="no gated op matched")
        return

    # Ask the user in-thread; `decide` blocks on the approve/deny returned through the
    # inbox round-trip (a fake supplies it in tests).
    slack_post(slack, channel, thread_ts, f"approve gated op? {command}")
    approved = decide(command)
    logger.info(f"gated-op decision op={matched_op} allowed={approved}")

    if approved:
        _emit_decision(stdout, allow=True, reason="approved by user")
        return

    _emit_decision(stdout, allow=False, reason="denied by user")
    raise GatedOpDeniedError(f"gated op denied: {matched_op}")
