from __future__ import annotations

from pan.core.errors import SessionBusyError, TargetBlockedError
from pan.core.events import NotDelivered, NotDeliveredReason
from pan.core.models import AgentStatus, Conversation, Terminal, TerminalState
from pan.core.seams import Reporter, TerminalHost
from pan.utils.config import CLAUDE_AGENT_NAMES, KNOWN_AGENT_NAMES

# The agent states in which a session is waiting for input, so text can be typed into it. Any
# other state (working, an error, no status, an unknown one) counts as busy, except a fresh
# Claude session (see is_ready).
_READY_STATUSES = frozenset({AgentStatus.IDLE, AgentStatus.DONE})


def is_agent_session(agent: str | None, agent_status: AgentStatus) -> bool:
    # A coding agent: it reports a status, or hc names a known agent as its program (one that
    # has not reported yet). Anything else (a shell, a dev server, `tail`) is not a session:
    # the project map never makes it a stream or lets it make its parent an orchestrator.
    return agent_status is not AgentStatus.NONE or agent in KNOWN_AGENT_NAMES


def is_live_agent_session(terminal: Terminal) -> bool:
    return terminal.state is TerminalState.LIVE and is_agent_session(
        terminal.agent, terminal.agent_status
    )


def session_name(terminal: Terminal) -> str:
    return terminal.name or terminal.address


def is_ready(terminal: Terminal) -> bool:
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


def deliver_when_ready(
    terminal_host: TerminalHost,
    terminal: Terminal,
    text: str,
    *,
    reporter: Reporter,
    conversation: Conversation,
    retry_choice: str | None = None,
) -> None:
    # Type the text only into a session that is ready for it. Busy, or a state pan cannot
    # read, is refused (typing could land in the middle of someone's work there); waiting on
    # a person is refused too, by pan here or by the host at the send. A refusal is reported
    # and raises, with nothing typed; `retry_choice`, when given, is the person's answer to
    # pan's question, which the refusal tells them to send again later.
    def refusal(reason: NotDeliveredReason) -> NotDelivered:
        return NotDelivered(
            reason=reason, session_name=session_name(terminal), retry_choice=retry_choice
        )

    if terminal.agent_status is AgentStatus.BLOCKED:
        reporter.report(conversation, refusal(NotDeliveredReason.WAITING_ON_PROMPT))
        raise TargetBlockedError(f"{terminal.address} is waiting on a person")
    if not is_ready(terminal):
        reporter.report(conversation, refusal(NotDeliveredReason.BUSY))
        raise SessionBusyError(f"{terminal.address} is {terminal.agent_status.value}")
    try:
        terminal_host.send(terminal.address, text)
    except TargetBlockedError:
        reporter.report(conversation, refusal(NotDeliveredReason.WAITING_ON_PROMPT))
        raise
