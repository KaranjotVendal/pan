from __future__ import annotations

from pan.models import Agent, Directive, TaskMode

_VALID_AGENTS = {member.value for member in Agent}

# Fixed, deterministic soft-trigger phrase set for the reconciled sessions view. Matched
# by pure substring logic on the cleaned prose (never model judgment), so INV-3 holds.
# The explicit --sessions flag is canonical; these are a best-effort convenience and will
# miss paraphrases (tech-spec R-14).
_SESSION_TRIGGERS = (
    "what's running",
    "whats running",
    "list sessions",
    "list threads",
    "list all threads",
    "list all the threads",
)


def parse_directive(raw_text: str) -> Directive:
    stripped_text = raw_text.strip()

    leading_bang_sync = stripped_text.startswith("!")
    if leading_bang_sync:
        stripped_text = stripped_text[1:].strip()

    has_sync_flag = leading_bang_sync
    has_status_flag = False
    has_sessions_flag = False
    force_new = False
    target_stream: str | None = None
    agent: Agent | None = None
    prose_tokens: list[str] = []

    tokens = stripped_text.split()
    token_index = 0
    while token_index < len(tokens):
        token = tokens[token_index]
        if token == "--sync":
            has_sync_flag = True
        elif token == "--status":
            has_status_flag = True
        elif token == "--sessions":
            has_sessions_flag = True
        elif token == "--new":
            force_new = True
        elif token == "--stream":
            # Always strip the flag; consume the next token as the stream name only
            # when it is present and not itself another flag.
            next_value = tokens[token_index + 1] if token_index + 1 < len(tokens) else None
            if next_value is not None and not next_value.startswith("--"):
                target_stream = next_value
                token_index += 1
        elif token == "--agent":
            # Reserved in v1. Always strip the flag; consume the next token as the
            # agent value when present and not another flag, and set `agent` only when
            # that value names a known Agent (an unknown value is consumed but ignored).
            next_value = tokens[token_index + 1] if token_index + 1 < len(tokens) else None
            if next_value is not None and not next_value.startswith("--"):
                token_index += 1
                if next_value in _VALID_AGENTS:
                    agent = Agent(next_value)
        else:
            prose_tokens.append(token)
        token_index += 1

    cleaned_text = " ".join(prose_tokens)
    cleaned_lower = cleaned_text.lower()
    soft_sessions_trigger = any(trigger in cleaned_lower for trigger in _SESSION_TRIGGERS)

    # Precedence, deterministic (INV-3): an explicit --sessions / --status flag is
    # authoritative (--sessions, the broader "list all", wins when both appear); the
    # sessions soft trigger then outranks --sync / delegate but not an explicit --status.
    if has_sessions_flag:
        mode = TaskMode.SESSIONS
    elif has_status_flag:
        mode = TaskMode.STATUS
    elif soft_sessions_trigger:
        mode = TaskMode.SESSIONS
    elif has_sync_flag:
        mode = TaskMode.SYNC
    else:
        mode = TaskMode.DELEGATE

    return Directive(
        mode=mode,
        force_new=force_new,
        target_stream=target_stream,
        agent=agent,
        cleaned_text=cleaned_text,
    )
