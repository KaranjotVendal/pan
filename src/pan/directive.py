from __future__ import annotations

import re

from pan.models import Agent, Directive, Route, RouteKind, TaskMode, ThreadSource

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


# Some Slack clients and connectors end a message with a signature such as
# "\n*Sent using* <@U0EXAMPLE|Claude>" (sometimes quoted with `>`). Left in, it stops a whole-message
# route word (`check`) from matching and leaks into task text, briefs and Inbox thread names.
# Only a signature at the very end is removed: "sent using" (any case, optional `*`/`_`
# emphasis) and one user mention with an optional `|label`.
_TRAILING_SIGNATURE = re.compile(
    r"(?:^|\s+)(?:(?:>|&gt;)\s*)?[*_]{0,2}sent\s+using[*_]{0,2}\s*"
    r"<@[A-Z0-9]+(?:\|[^>]*)?>[*_]{0,2}\s*$",
    re.IGNORECASE,
)


def _strip_signature(text: str) -> str:
    return _TRAILING_SIGNATURE.sub("", text, count=1)


# "reply in slack" anywhere in a request, any letter case, sets the thread's reply mode to
# SLACK. The one phrase matched anywhere: a false match costs only an extra Slack reply.
_REPLY_IN_SLACK = re.compile(r"\breply\s+in\s+slack\b", re.IGNORECASE)


def wants_slack_reply(raw_text: str) -> bool:
    return _REPLY_IN_SLACK.search(raw_text) is not None


def _normalize_punctuation(text: str) -> str:
    text = _DASH_GLUED_TO_WORD.sub("--", text)
    return text.translate(_SMART_QUOTE_MAP)


def _clean_message(raw_text: str) -> str:
    # Phone punctuation repaired first, then a trailing client signature and a single leading
    # Slack mention removed, so the first word is the one the person typed after `@pan` and
    # the last word is theirs too.
    text = _strip_signature(_normalize_punctuation(raw_text.strip()))
    return _LEADING_MENTION.sub("", text, count=1).strip()


def parse_directive(raw_text: str) -> Directive:
    # Repair phone smart-punctuation on the whole text BEFORE any verb/flag detection, so a
    # phone-mangled `—sessions`/`—full` is recognized as `--sessions`/`--full` (INV-3). Strip
    # a single leading Slack mention before any verb/flag/mode detection, so the leading-verb
    # check sees `relay`/`read` at position 0 and cleaned_text (worker briefs) never carries
    # the stray mention. A mention inside a relay message body is preserved.
    stripped_text = _clean_message(raw_text)

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

    # The retired --new / --status / --sync flags and the leading `!` are not parsed here:
    # parse_route turns a message carrying one into a notice before any directive is used.
    has_sessions_flag = False
    target_stream: str | None = None
    agent: Agent | None = None
    prose_tokens: list[str] = []

    tokens = stripped_text.split()
    token_index = 0
    while token_index < len(tokens):
        token = tokens[token_index]
        if token == "--sessions":
            has_sessions_flag = True
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
    mode = TaskMode.SESSIONS if has_sessions_flag or soft_sessions_trigger else TaskMode.DELEGATE

    return Directive(
        mode=mode,
        target_stream=target_stream,
        agent=agent,
        cleaned_text=cleaned_text,
    )


# Route words (session control). Strict: the route word is the first word after the mention,
# spelled exactly, in any letter case; anything else is task text. `new:` and `to <name>:`
# need the colon right after the word, so "new login page" stays a plain task. The `to` name is
# one token, so a sentence that starts with "to" and holds a colon stays a plain task.
_NEW_ROUTE = re.compile(r"^new:(?:\s+|$)", re.IGNORECASE)
_TO_ROUTE = re.compile(r"^to\s+([^\s:]+):(?:\s+|$)", re.IGNORECASE)
_WHOLE_MESSAGE_ROUTES = {
    "sessions": RouteKind.SESSIONS,
    "forget": RouteKind.FORGET,
    "check": RouteKind.CHECK,
    "done": RouteKind.DONE,
}
# `done --discard` (the whole message, words separated by any spaces) closes the session even
# when that throws away uncommitted files or unpushed commits. A phone's em-dash `—discard` is
# read as `--discard` (see _normalize_punctuation).
_DONE_DISCARD_WORDS = ("done", "--discard")
# The one-off verbs keep their own grammar; their message body is sent verbatim, so a
# flag-looking token inside it is never read as a retired flag.
_ONE_OFF_VERBS = frozenset({"relay", "read"})
_RETIRED_FLAG_NOTICES = {
    "--new": "--new is retired: start the message with new: instead, as in @pan new: <task>",
    "--status": (
        "--status is retired: @pan check posts this thread's latest answer, "
        "and @pan sessions lists every session"
    ),
    "--sync": "--sync is retired: add reply in slack to the message instead",
}
_RETIRED_BANG_NOTICE = "a leading ! is retired: add reply in slack to the message instead"
_EMPTY_NEW_NOTICE = "new: needs a task after it, as in @pan new: <task>"
# Text from X is someone else's text, so it must never steer a session that already exists:
# until the X safety gate (MOB-87) is in place, X may only start new sessions.
X_TO_ROUTE_NOTICE = "messages from X cannot use to <name>: yet; they can only start new sessions"


def _retired_flag_notice(text: str) -> str | None:
    if text.startswith("!"):
        return _RETIRED_BANG_NOTICE
    for token in text.split():
        notice = _RETIRED_FLAG_NOTICES.get(token)
        if notice is not None:
            return notice
    return None


def route_for_source(route: Route, source: ThreadSource) -> Route:
    # The routes a front door may use. X may not use `to <name>:` (see X_TO_ROUTE_NOTICE).
    # Applied at parse time and again at drain, so a route an older gateway wrote is held to
    # the same rule.
    if source is ThreadSource.X and route.kind is RouteKind.TO:
        return Route(kind=RouteKind.NOTICE, session=route.session, notice=X_TO_ROUTE_NOTICE)
    return route


def parse_route(raw_text: str, source: ThreadSource = ThreadSource.SLACK) -> Route:
    # Pure string logic (INV-3), run by the gateway so the route travels on the inbox item.
    return route_for_source(_parse_route_words(raw_text), source)


def _parse_route_words(raw_text: str) -> Route:
    text = _clean_message(raw_text)
    reply_in_slack = wants_slack_reply(text)

    whole_message_kind = _WHOLE_MESSAGE_ROUTES.get(text.lower())
    if whole_message_kind is not None:
        return Route(kind=whole_message_kind)
    if tuple(text.lower().split()) == _DONE_DISCARD_WORDS:
        return Route(kind=RouteKind.DONE, discard=True)

    new_match = _NEW_ROUTE.match(text)
    if new_match is not None:
        task = text[new_match.end() :].strip()
        if not task:
            return Route(kind=RouteKind.NOTICE, notice=_EMPTY_NEW_NOTICE)
        return Route(kind=RouteKind.NEW, text=task, reply_in_slack=reply_in_slack)

    to_match = _TO_ROUTE.match(text)
    if to_match is not None:
        session = to_match.group(1)
        task = text[to_match.end() :].strip()
        if not task:
            notice = f"to {session}: needs a task after it, as in @pan to {session}: <task>"
            return Route(kind=RouteKind.NOTICE, session=session, notice=notice)
        return Route(kind=RouteKind.TO, session=session, text=task, reply_in_slack=reply_in_slack)

    first_word = text.split(maxsplit=1)[0] if text else ""
    if first_word not in _ONE_OFF_VERBS:
        notice = _retired_flag_notice(text)
        if notice is not None:
            return Route(kind=RouteKind.NOTICE, text=text, notice=notice)

    return Route(kind=RouteKind.TASK, text=text, reply_in_slack=reply_in_slack)


# A reply to pan's open question asking which session should get a task (`pan propose`):
# the whole message is `yes` or `y` (any case) or a number from 1 to 3, or a decline (`no`,
# `n`, `cancel`, `stop`), optionally after the mention and with a closing `.` or `!`. Whether a
# question is open is not known here; `pan inbox drain` checks that before turning the reply
# into a CONFIRM route.
_CONFIRM_REPLY = re.compile(r"^(yes|y|[1-3]|no|n|cancel|stop)[.!]?$", re.IGNORECASE)
CONFIRM_YES = "yes"
CONFIRM_NO = "no"
_YES_WORDS = frozenset({"yes", "y"})
_NO_WORDS = frozenset({"no", "n", "cancel", "stop"})


def parse_confirm_reply(raw_text: str) -> str | None:
    # `yes` for yes or y, `no` for a decline, the digit for a number, else None.
    reply_match = _CONFIRM_REPLY.match(_clean_message(raw_text))
    if reply_match is None:
        return None
    reply = reply_match.group(1).lower()
    if reply in _YES_WORDS:
        return CONFIRM_YES
    if reply in _NO_WORDS:
        return CONFIRM_NO
    return reply
