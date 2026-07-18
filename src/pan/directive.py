from __future__ import annotations

from pan.models import Agent, Directive, TaskMode

_VALID_AGENTS = {member.value for member in Agent}


def parse_directive(raw_text: str) -> Directive:
    stripped_text = raw_text.strip()

    leading_bang_sync = stripped_text.startswith("!")
    if leading_bang_sync:
        stripped_text = stripped_text[1:].strip()

    has_sync_flag = leading_bang_sync
    has_status_flag = False
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

    if has_status_flag:
        mode = TaskMode.STATUS
    elif has_sync_flag:
        mode = TaskMode.SYNC
    else:
        mode = TaskMode.DELEGATE

    return Directive(
        mode=mode,
        force_new=force_new,
        target_stream=target_stream,
        agent=agent,
        cleaned_text=" ".join(prose_tokens),
    )
