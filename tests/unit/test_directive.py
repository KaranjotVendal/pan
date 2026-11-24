from __future__ import annotations

import pytest

from pan.directive import asks_to_discard, clean_message, names_selector, wants_slack_reply


@pytest.mark.parametrize(
    "raw_text, expected_text",
    [
        ("<@U000TEST> fix the login page", "fix the login page"),
        ("  <@U000TEST>   check  ", "check"),
        ("ask <@U000TEST> about it", "ask <@U000TEST> about it"),
        ("<@U000BOT> fix the bug\n*Sent using* <@U000TEST|Claude>", "fix the bug"),
        ("check\n\n> *Sent using* <@U000TEST|Claude>  ", "check"),
        ("check\n&gt; *Sent using* <@U000TEST|Claude>", "check"),
        ("forget\n_sent using_ <@U000TEST|Claude>", "forget"),
        ("say who sent using <@U000TEST> earlier", "say who sent using <@U000TEST> earlier"),
        ("done —discard", "done --discard"),
        ("done –discard", "done --discard"),
        ("plan — which one", "plan — which one"),
        ("build the “thing” now", 'build the "thing" now'),
        ("don’t break it", "don't break it"),
    ],
    ids=[
        "leading-mention",
        "mention-and-spaces",
        "inner-mention-kept",
        "trailing-signature",
        "quoted-signature",
        "escaped-quote-signature",
        "underscore-signature",
        "mid-text-signature-kept",
        "em-dash-flag",
        "en-dash-flag",
        "spaced-dash-kept",
        "curly-double-quotes",
        "curly-apostrophe",
    ],
)
def test_clean_message(raw_text: str, expected_text: str) -> None:
    assert clean_message(raw_text) == expected_text


@pytest.mark.parametrize(
    "raw_text, expected",
    [
        ("<@U000TEST> fix the login page, reply in slack", True),
        ("Reply In Slack when you are done", True),
        ("fix it and REPLY IN SLACK", True),
        ("fix it, reply  in\nslack please", True),
        ("fix the login page", False),
        ("reply in slackware docs", False),
        ("reply on slack", False),
        ("", False),
    ],
)
def test_wants_slack_reply_matches_the_phrase_anywhere(raw_text: str, expected: bool) -> None:
    assert wants_slack_reply(raw_text) is expected


@pytest.mark.parametrize(
    "request_text, selector, expected",
    [
        ("<@U000BOT> to ocr: rerun it", "ocr", True),
        ("<@U000BOT> send this to OCR please", "ocr", True),
        ("<@U000BOT> to ocr-action: rerun it", "ocr-action", True),
        ("<@U000BOT> ask work / 1 to rerun", "work / 1", True),
        ("<@U000BOT> to t7, rerun it", "t7", True),
        ("<@U000BOT> to ocrr: rerun it", "ocr", False),
        ("<@U000BOT> to ocr-action: rerun it", "ocr", False),
        ("<@U000BOT> to my_ocr: rerun it", "ocr", False),
        ("<@U000BOT> rerun the ocr job", "ocr-action", False),
        ("<@U000BOT> rerun it", "", False),
        ("<@U000BOT> to t7: rerun it", "t", False),
    ],
    ids=[
        "exact",
        "any-case",
        "hyphenated-name",
        "phrase-with-spaces",
        "address-before-comma",
        "typo",
        "prefix-of-longer-name",
        "inside-underscore-word",
        "longer-than-typed",
        "empty-selector",
        "letter-of-address",
    ],
)
def test_names_selector(request_text: str, selector: str, expected: bool) -> None:
    assert names_selector(request_text, selector) is expected


@pytest.mark.parametrize(
    "request_text, expected",
    [
        ("<@U000BOT> done --discard", True),
        ("<@U000BOT> done —discard", True),
        ("<@U000BOT> please close it, --discard.", True),
        ("<@U000BOT> done", False),
        ("<@U000BOT> just close it and throw it away", False),
        ("<@U000BOT> done discard", False),
        ("<@U000BOT> done --discarded", False),
    ],
    ids=[
        "flag",
        "phone-dash",
        "flag-in-sentence",
        "plain-done",
        "by-meaning",
        "no-dashes",
        "longer",
    ],
)
def test_asks_to_discard(request_text: str, expected: bool) -> None:
    assert asks_to_discard(request_text) is expected
