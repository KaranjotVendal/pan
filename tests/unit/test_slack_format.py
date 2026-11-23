from __future__ import annotations

import pytest

from pan.slack_format import split_for_slack, to_slack_mrkdwn


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("**bold**", "*bold*"),
        ("***both***", "*_both_*"),
        ("# H1", "*H1*"),
        ("## Header", "*Header*"),
        ("###### H6", "*H6*"),
        ("## **Bold Header**", "*Bold Header*"),
        ("*em*", "_em_"),
        # Disambiguation guard: spaced asterisks are literal, not italic.
        ("a * b * c", "a * b * c"),
    ],
)
def test_emphasis_and_headers(raw: str, expected: str) -> None:
    assert to_slack_mrkdwn(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("[text](https://e.co)", "<https://e.co|text>"),
        ("~~struck~~", "~struck~"),
        ("> quoted line", "> quoted line"),
    ],
)
def test_links_strike_blockquote(raw: str, expected: str) -> None:
    assert to_slack_mrkdwn(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "```\n**not bold** in code\n```",
        "`**x**`",
        "<@U123>",
        "<#C456>",
        "<https://e.co|site>",
    ],
)
def test_protected_regions_pass_through_untouched(raw: str) -> None:
    # Fenced/inline code and existing Slack entities/manual links must survive
    # byte-for-byte, including `**` inside code which must NOT become `*`.
    assert to_slack_mrkdwn(raw) == raw


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a & b", "a &amp; b"),
        ("a < b > c", "a &lt; b &gt; c"),
        # Already-escaped input is not double-escaped (unescape-first).
        ("a &amp; b", "a &amp; b"),
        # A protected entity is not escaped (it was stashed before the escape pass).
        ("<@U123>", "<@U123>"),
    ],
)
def test_escaping(raw: str, expected: str) -> None:
    assert to_slack_mrkdwn(raw) == expected


def test_table_three_columns_widths_aligned() -> None:
    # Genuinely 3 columns so middle-column padding (a non-terminal cell, not rstripped)
    # is exercised — e.g. "42%" padded to width 5 in the middle column.
    raw = (
        "| Metric | Value | Note |\n"
        "| --- | --- | --- |\n"
        "| SR | 42% | ok |\n"
        "| Conversions | 1200 | high |"
    )
    expected = (
        "```\nMetric      | Value | Note\nSR          | 42%   | ok\nConversions | 1200  | high\n```"
    )
    assert to_slack_mrkdwn(raw) == expected


def test_table_two_columns_aligned() -> None:
    raw = "| a | bb |\n|---|---|\n| ccc | d |"
    expected = "```\na   | bb\nccc | d\n```"
    assert to_slack_mrkdwn(raw) == expected


def test_pipe_row_without_delimiter_is_left_as_prose() -> None:
    raw = "| a | b |"
    assert to_slack_mrkdwn(raw) == raw


def test_prose_pipe_line_followed_by_divider_is_not_a_table() -> None:
    # GFM requires the delimiter row's cell count to match the header's. A prose line with
    # a pipe followed by a `---` section divider (one cell) must NOT be fenced as a table,
    # and the divider must survive rather than being consumed.
    raw = "Ran tests | 5 passed\n---\nmore prose"
    assert to_slack_mrkdwn(raw) == raw


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("", ""),
        ("just plain prose.", "just plain prose."),
    ],
)
def test_totality(raw: str, expected: str) -> None:
    assert to_slack_mrkdwn(raw) == expected


@pytest.mark.parametrize(
    "text, max_length, expected_parts",
    [
        ("short answer", 40, ["short answer"]),
        ("aaaa\nbbbb\ncccc\ndddd", 14, ["aaaa\nbbbb", "cccc\ndddd"]),
        ("a" * 25, 14, ["aaaaaa", "aaaaaa", "aaaaaa", "aaaaaa\na"]),
        (
            "intro\n```\nline one\nline two\n```\nend",
            24,
            ["intro\n```\nline one\n```", "```\nline two\n```\nend"],
        ),
    ],
    ids=["fits", "at-line-breaks", "long-line-cut", "fence-closed-and-reopened"],
)
def test_split_for_slack(text: str, max_length: int, expected_parts: list[str]) -> None:
    parts = split_for_slack(text, max_length)

    assert parts == expected_parts
    assert all(len(part) <= max_length for part in parts)


def test_split_for_slack_keeps_every_line_in_order() -> None:
    lines = [f"line {number}" for number in range(500)]

    parts = split_for_slack("\n".join(lines), 300)

    assert len(parts) > 1
    assert "\n".join(parts).split("\n") == lines
