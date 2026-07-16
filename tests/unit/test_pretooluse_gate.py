from __future__ import annotations

import io
import json

import pytest

from pan.errors import GatedOpDeniedError
from pan.hooks.pretooluse_gate import pretooluse_gate


class FakeSlack:
    def __init__(self) -> None:
        self.posts: list[tuple[str, str, str]] = []

    def add_reaction(self, channel: str, ts: str, name: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def post_message(self, channel: str, thread_ts: str, text: str) -> None:
        self.posts.append((channel, thread_ts, text))

    def start(self) -> None:  # pragma: no cover
        raise NotImplementedError


def _payload(command: str) -> str:
    return json.dumps(
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )


def _decision(stdout: io.StringIO) -> str:
    emitted = json.loads(stdout.getvalue())
    return emitted["hookSpecificOutput"]["permissionDecision"]


def _never_called(command: str) -> bool:  # pragma: no cover
    raise AssertionError("decision source should not be consulted")


@pytest.mark.parametrize(
    "gated_ops, command",
    [
        ([], "rm -rf /"),
        (["rm -rf"], "ls -la"),
    ],
    ids=["empty-gated-ops", "command-does-not-match"],
)
def test_unmatched_command_allows_without_touching_slack(
    gated_ops: list[str], command: str
) -> None:
    slack = FakeSlack()
    stdout = io.StringIO()

    pretooluse_gate(
        gated_ops,
        "1718000000.000200",
        "C1",
        slack,
        _never_called,
        stdin=io.StringIO(_payload(command)),
        stdout=stdout,
    )

    assert _decision(stdout) == "allow"
    assert slack.posts == []


def test_matching_op_approved_posts_and_allows() -> None:
    slack = FakeSlack()
    stdout = io.StringIO()

    pretooluse_gate(
        ["rm -rf"],
        "1718000000.000200",
        "C1",
        slack,
        lambda command: True,
        stdin=io.StringIO(_payload("rm -rf /tmp/x")),
        stdout=stdout,
    )

    assert len(slack.posts) == 1  # approval request posted via egress
    assert slack.posts[0][0:2] == ("C1", "1718000000.000200")
    assert _decision(stdout) == "allow"


def test_matching_op_denied_raises_and_blocks() -> None:
    slack = FakeSlack()
    stdout = io.StringIO()

    with pytest.raises(GatedOpDeniedError):
        pretooluse_gate(
            ["rm -rf"],
            "1718000000.000200",
            "C1",
            slack,
            lambda command: False,
            stdin=io.StringIO(_payload("rm -rf /tmp/x")),
            stdout=stdout,
        )

    assert len(slack.posts) == 1
    assert _decision(stdout) == "deny"
