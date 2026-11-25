from __future__ import annotations

import re

# A single leading Slack mention token (`<@U...>` — a Slack user id, uppercase-alnum), plus
# any trailing whitespace, anchored at the START of the text only. Real Slack app-mention
# text always begins with `<@BOTID>`; it is removed so the orchestrator and the briefs see
# only what the person typed.
_LEADING_MENTION = re.compile(r"^<@[A-Z0-9]+>\s*")

# Phone keyboards autocorrect a typed ASCII `--` into an em-dash (U+2014) or en-dash
# (U+2013) glued to the following word, so `@pan done --discard` arrives as `—discard`.
# Normalize a dash glued to a word char back to `--`; a SPACED prose dash (`plan — which`) is
# real punctuation and is left intact. Curly quotes (also autocorrected) are straightened.
_DASH_GLUED_TO_WORD = re.compile(r"[—–](?=\w)")
_SMART_QUOTE_MAP = {
    ord("“"): '"',
    ord("”"): '"',
    ord("‘"): "'",
    ord("’"): "'",
}


# Some Slack clients and connectors end a message with a signature such as
# "\n*Sent using* <@U0EXAMPLE|Claude>" (sometimes quoted with `>`). Left in, it leaks into
# task text, briefs and Inbox thread names.
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


def clean_message(raw_text: str) -> str:
    # Phone punctuation repaired first, then a trailing client signature and a single leading
    # Slack mention removed, so the first word is the one the person typed after `@pan` and
    # the last word is theirs too.
    text = _strip_signature(_normalize_punctuation(raw_text.strip()))
    return _LEADING_MENTION.sub("", text, count=1).strip()


# Safety gates on the person's own message, as pan recorded it (pan send --to, pan done
# --discard, pan confirm). The orchestrator decides what a message means; these only check that
# the words that make an action risky were typed by the person. A selector is named when it
# appears as a whole word or phrase, any letter case: letters, digits, `_` and `-` on either
# side make it part of a longer word, so "ocr" is not named by "ocr-action".
_DONE_DISCARD_WORDS = ("done", "--discard")
# A reply that turns pan's question down: it starts with one of these words, or holds one of
# these phrases anywhere.
_DECLINE_START = re.compile(r"^(?:no|n|nope|nah|cancel|stop|none|neither|nothing)\b", re.IGNORECASE)
_DECLINE_PHRASE = re.compile(
    r"\b(?:none of (?:those|these|them)|not (?:those|these|them|that one|any)|"
    r"don'?t send|do not send)\b",
    re.IGNORECASE,
)
# A reply saying yes is a yes word, followed by nothing but punctuation, more yes words and
# polite filler (any letter case): "yes", "ok, thanks!", "yep send it please". Anything else
# ("ok which one is that?", "go with the docs one instead") is not a yes.
_YES_WORDS = r"yes|y|yep|yup|yeah|yea|sure|ok|okay"
_YES_FILLER = r"please|thanks|thank you|thx|go ahead|send it|do it|that one|" + _YES_WORDS
_YES_REPLY = re.compile(
    rf"^(?:{_YES_WORDS}|go ahead|send it|do it|go)(?:[\s.,!]*\b(?:{_YES_FILLER})\b)*[\s.,!:)]*$",
    re.IGNORECASE,
)
# A reply picking a number starts with it, alone or as `#N`, before any other word.
_LEADING_NUMBER = re.compile(r"^#?(\d+)(?![\w-])")


def names_selector(request_text: str, selector: str) -> bool:
    wanted = selector.strip()
    if not wanted:
        return False
    pattern = re.compile(rf"(?<![\w-]){re.escape(wanted)}(?![\w-])", re.IGNORECASE)
    return pattern.search(clean_message(request_text)) is not None


def asks_to_discard(request_text: str) -> bool:
    # The whole message is `done --discard`, any letter case and spacing (a phone's `—discard`
    # is read as `--discard`).
    return tuple(clean_message(request_text).lower().split()) == _DONE_DISCARD_WORDS


def is_decline(answer_text: str) -> bool:
    cleaned = clean_message(answer_text)
    return _DECLINE_START.search(cleaned) is not None or _DECLINE_PHRASE.search(cleaned) is not None


def says_yes(answer_text: str) -> bool:
    return _YES_REPLY.match(clean_message(answer_text)) is not None


def picks_number(answer_text: str, number: str) -> bool:
    # The reply is the number, or starts with it ("2", "2 please", "#2"); "not 2, 3" and
    # "the 2nd" pick nothing.
    leading = _LEADING_NUMBER.search(clean_message(answer_text))
    return leading is not None and leading.group(1) == number
