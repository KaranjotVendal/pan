from __future__ import annotations

import pytest

from pan.directive import (
    asks_to_discard,
    clean_message,
    is_decline,
    names_selector,
    picks_number,
    says_yes,
    wants_slack_reply,
)


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
        ("<@U000BOT> Done   --DISCARD", True),
        ("<@U000BOT> done \u2014discard", True),
        ("<@U000BOT> done --discard\n*Sent using* <@U000TEST|Claude>", True),
        ("<@U000BOT> please close it, --discard.", False),
        ("<@U000BOT> close pan-7 --discard", False),
        ("<@U000BOT> done", False),
        ("<@U000BOT> just close it and throw it away", False),
        ("<@U000BOT> done discard", False),
        ("<@U000BOT> done --discarded", False),
    ],
    ids=[
        "exact",
        "case-and-spaces",
        "phone-dash",
        "with-signature",
        "flag-in-sentence",
        "flag-after-other-words",
        "plain-done",
        "by-meaning",
        "no-dashes",
        "longer",
    ],
)
def test_asks_to_discard(request_text: str, expected: bool) -> None:
    assert asks_to_discard(request_text) is expected


@pytest.mark.parametrize(
    "answer_text, expected",
    [
        ("no", True),
        ("No.", True),
        ("n", True),
        ("nope", True),
        ("nah, leave it", True),
        ("<@U000BOT> cancel", True),
        ("stop", True),
        ("none", True),
        ("none of those", True),
        ("hmm, none of them", True),
        ("neither", True),
        ("not those", True),
        ("please don't send it", True),
        ("yes", False),
        ("1", False),
        ("the second one", False),
        ("nice, go with 2", False),
        ("now please", False),
    ],
)
def test_is_decline(answer_text: str, expected: bool) -> None:
    assert is_decline(answer_text) is expected


@pytest.mark.parametrize(
    "answer_text, expected",
    [
        ("yes", True),
        ("Yes please", True),
        ("<@U000BOT> y", True),
        ("yep", True),
        ("yeah, that one", True),
        ("sure", True),
        ("ok", True),
        ("Okay.", True),
        ("go ahead", True),
        ("send it", True),
        ("1", False),
        ("the first one", False),
        ("not yet", False),
        ("yesterday's one", False),
        ("i guess yes", False),
        ("ok, thanks!", True),
        ("yep send it please", True),
        ("yes yes", True),
        ("do it please", True),
        ("ok which one is that?", False),
        ("go with the docs one instead", False),
        ("yes, but not that one", False),
        ("sure, after lunch", False),
        ("goat", False),
    ],
)
def test_says_yes(answer_text: str, expected: bool) -> None:
    assert says_yes(answer_text) is expected


@pytest.mark.parametrize(
    "answer_text, number, expected",
    [
        ("2", "2", True),
        ("<@U000BOT> 2 please", "2", True),
        ("#2", "2", True),
        ("2.", "2", True),
        ("2nd", "2", False),
        ("12", "2", False),
        ("the second one", "2", False),
        ("1", "2", False),
        ("not 2, 3", "2", False),
        ("not 2, 3", "3", False),
        ("option 3", "3", False),
        ("3 not 2", "2", False),
    ],
)
def test_picks_number(answer_text: str, number: str, expected: bool) -> None:
    assert picks_number(answer_text, number) is expected
