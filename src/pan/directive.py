from __future__ import annotations

import re

from pan.models import Agent, Directive, TaskMode

_VALID_AGENTS = {member.value for member in Agent}

# A single leading Slack mention token (`<@U...>` — a Slack user id, uppercase-alnum), plus
# any trailing whitespace, anchored at the START of the text only. Real Slack app-mention
# text always begins with `<@BOTID>`; leaving it in place puts a non-verb token at position 0
# and defeats the leading-verb grammar below (INV-3), mis-routing `@pan relay`/`@pan read`.
_LEADING_MENTION = re.compile(r"^<@[A-Z0-9]+>\s*")

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

# Phone keyboards autocorrect a typed ASCII `--` into an em-dash (U+2014) or en-dash
# (U+2013) glued to the following word, so `@pan --sessions` arrives as `—sessions` and
# falls through the flag scan into a spurious DELEGATE/spawn. Normalize a dash glued to a
# word char back to `--`; a SPACED prose dash (`plan — which`) is real punctuation and is
# left intact. Curly quotes (also autocorrected) are straightened. All exact string logic,
# no model judgment (INV-3).
_DASH_GLUED_TO_WORD = re.compile(r"[—–](?=\w)")
_SMART_QUOTE_MAP = {
    ord("“"): '"',
    ord("”"): '"',
    ord("‘"): "'",
    ord("’"): "'",
}


def _normalize_punctuation(text: str) -> str:
    text = _DASH_GLUED_TO_WORD.sub("--", text)
    return text.translate(_SMART_QUOTE_MAP)


def parse_directive(raw_text: str) -> Directive:
    stripped_text = raw_text.strip()

    # Repair phone smart-punctuation on the whole text BEFORE any verb/flag detection, so a
    # phone-mangled `—sessions`/`—full` is recognized as `--sessions`/`--full` (INV-3).
    stripped_text = _normalize_punctuation(stripped_text)

    # Strip a single leading Slack mention before any verb/flag/mode detection, so the
    # leading-verb check sees `relay`/`read` at position 0 and cleaned_text (worker briefs)
    # never carries the stray mention. Only the leading one is removed; a mention inside a
    # relay message body is preserved.
    stripped_text = _LEADING_MENTION.sub("", stripped_text, count=1)

    leading_bang_sync = stripped_text.startswith("!")
    if leading_bang_sync:
        stripped_text = stripped_text[1:].strip()

    # Leading-verb grammar (checked before any flag scan, so the verb outranks every flag
    # and the sessions soft trigger — INV-3). The verb is recognized only in position 0;
    # the second token is the target, and a missing target leaves target=None for the
    # resolver to surface deterministically rather than the parser guessing.
    #
    # `relay <target> <message...>`: EVERYTHING after the target is the message verbatim
    # (no flag scan of the message body, so a flag-looking token inside a worker brief
    # survives). `read <target> [--full]`: read carries no message; the remainder is scanned
    # only for the --full modifier.
    verb_tokens = stripped_text.split()
    if verb_tokens and verb_tokens[0] == "relay":
        target = verb_tokens[1] if len(verb_tokens) > 1 else None
        message = " ".join(verb_tokens[2:])
        return Directive(
            mode=TaskMode.RELAY,
            target=target,
            message=message,
            cleaned_text=message,
        )
    if verb_tokens and verb_tokens[0] == "read":
        target = verb_tokens[1] if len(verb_tokens) > 1 else None
        full = "--full" in verb_tokens[2:]
        return Directive(
            mode=TaskMode.READ,
            target=target,
            full=full,
            cleaned_text="",
        )
    if verb_tokens and verb_tokens[0] == "sessions":
        # Bare leading `sessions` is a SESSIONS trigger (mirrors the leading relay/read
        # verbs and the `--sessions` flag). Recognized only in position 0, so a `sessions`
        # deeper in a task stays prose; it carries no target/message (cleaned_text="").
        return Directive(mode=TaskMode.SESSIONS, cleaned_text="")

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
