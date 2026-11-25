from __future__ import annotations

import pytest

from pan.frontend.slack_format import redact_paths, split_for_slack, to_slack_mrkdwn


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


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("See [the PR](https://x.test/pull/1).", "See <https://x.test/pull/1|the PR>."),
        ("**bold** and __also bold__", "*bold* and *also bold*"),
        ("# Summary\n## Details", "*Summary*\n*Details*"),
        ("- one\n- two", "- one\n- two"),
        ("```\n**kept** as is\n```", "```\n**kept** as is\n```"),
        ("~~gone~~", "~gone~"),
        ("snake__case__name and `__init__`", "snake__case__name and `__init__`"),
    ],
    ids=[
        "link",
        "bold",
        "headings",
        "bullets",
        "code-block",
        "strikethrough",
        "underscores-in-words",
    ],
)
def test_an_answer_converts_to_slack_mrkdwn(answer: str, expected: str) -> None:
    assert to_slack_mrkdwn(answer) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("see /Users/someone/dev/pan-worktrees/pan-7", "see .../pan-7"),
        ("in /home/someone/repo/", "in .../repo"),
        ("at ~/dev/pan/src/app.py, line 3", "at .../app.py, line 3"),
        ("(/Users/someone/a/b)", "(.../b)"),
        ("`/Users/someone/a/b`", "`.../b`"),
        ("https://example.com/home/someone/page", "https://example.com/home/someone/page"),
        ("file:///Users/someone/a.txt", ".../a.txt"),
        (
            "<https://example.com/Users/a|/Users/someone/a/b>",
            "<https://example.com/Users/a|.../b>",
        ),
        ("<@U000TEST> moved src/home/x", "<@U000TEST> moved src/home/x"),
        ("nothing to cut", "nothing to cut"),
        ("</Users/someone/x/config.py|config.py>", "config.py"),
        ("</Users/someone/x/config.py>", ".../config.py"),
        ("<~/dev/pan/notes.md|notes>", "notes"),
        ("<docs/plan.md|the plan>", "<docs/plan.md|the plan>"),
        ("<./docs/plan.md|the plan>", "the plan"),
        ("<www.example.com|Example>", "<www.example.com|Example>"),
        ("<@U000TEST|/Users/someone/a>", "<@U000TEST|.../a>"),
        ("<!subteam^S000TEST|~/team/x>", "<!subteam^S000TEST|.../x>"),
        ("<#C000TEST|/home/someone/repo>", "<#C000TEST|.../repo>"),
        ("<file:///Users/someone/a.txt|a.txt>", "a.txt"),
        ("<file:///Users/someone/a.txt>", ".../a.txt"),
        ("<mailto:someone@example.com|mail>", "<mailto:someone@example.com|mail>"),
        ("<#C000TEST|general> and <!here>", "<#C000TEST|general> and <!here>"),
        ("path:/Users/someone/a/b", "path:.../b"),
        ("```\nx < /Users/someone/a/b > y\n```", "```\nx < .../b > y\n```"),
        ("`</Users/someone/a/b>`", "`<.../b>`"),
        ("open /Users/someone/My Project/x.py now", "open .../x.py now"),
        ("`/Users/someone/My Project/x.py`", "`.../x.py`"),
        ('see "/Users/someone/My Project/x.py"', 'see ".../x.py"'),
        ("```\n~/a.py ~/b.py\n```", "```\n.../a.py .../b.py\n```"),
        ("`file:///Users/someone/a.py`", "`.../a.py`"),
        ("/Users/someone/a.py and/or ~/b.py", ".../a.py and/or .../b.py"),
        ("~/a.py ~/b.py", ".../a.py .../b.py"),
        ("/Users/someone/x.py 1/2 done", ".../x.py 1/2 done"),
        ("in /Users/someone/My Project files", "in .../My Project files"),
        (
            "saved to /Users/someone/Library/Mobile Documents/com~apple~CloudDocs/"
            "AcmeCorp Contract/draft.pdf",
            "saved to .../draft.pdf",
        ),
        ("/Users/someone/My Project/x.py", ".../x.py"),
        ("edited /Users/someone/a.py and/or ~/b.py", "edited .../a.py and/or .../b.py"),
        ("compare ~/a.py ~/b.py", "compare .../a.py .../b.py"),
        ("`cat /Users/someone/a.txt foo/bar`", "`cat .../a.txt foo/bar`"),
        ("the file:// scheme", "the file:// scheme"),
        ("FILE:///Users/someone/a.py", ".../a.py"),
        ("`File:///home/someone/b.py`", "`.../b.py`"),
        ("<https://example.com|/Users/someone/a.py>", "<https://example.com|.../a.py>"),
        ("<mailto:a@example.com|~/mail/a.txt>", "<mailto:a@example.com|.../a.txt>"),
        ("<www.example.com|/home/someone/b>", "<www.example.com|.../b>"),
        ("/Users/someone/dir https://example.com/x", ".../dir https://example.com/x"),
        ("/Users/someone/a https://example.com/b", ".../a https://example.com/b"),
    ],
    ids=[
        "users",
        "home-trailing-slash",
        "tilde-with-comma",
        "in-parentheses",
        "in-backticks",
        "url-untouched",
        "file-url-cut",
        "web-link-keeps-target-label-cut",
        "relative-path-untouched",
        "plain",
        "local-link-to-label",
        "local-link-without-label",
        "tilde-link-to-label",
        "relative-link-without-dot-kept",
        "dot-relative-link-to-label",
        "bare-domain-link-kept",
        "user-mention-label-cut",
        "subteam-mention-label-cut",
        "channel-mention-label-cut",
        "file-link-to-label",
        "file-link-without-label",
        "mailto-untouched",
        "channel-and-special-mentions-untouched",
        "colon-is-a-boundary",
        "inside-a-fence-with-angle-brackets",
        "angle-brackets-in-inline-code",
        "prose-folder-with-a-space-cut-whole",
        "code-path-with-a-space-cut-whole",
        "quoted-path-with-a-space-cut-whole",
        "code-two-paths-kept-apart",
        "file-url-in-code",
        "prose-and-or-not-swallowed",
        "prose-two-paths-kept-apart",
        "prose-fraction-not-swallowed",
        "space-then-plain-words-cut-at-the-space",
        "url-after-a-path-untouched",
        "icloud-folders-with-spaces",
        "folder-with-a-space",
        "and-or-regression",
        "two-paths-regression",
        "code-path-then-relative-path",
        "bare-file-scheme-untouched",
        "file-scheme-upper-case",
        "file-scheme-mixed-case-in-code",
        "web-link-label-cut",
        "mailto-label-cut",
        "www-label-cut",
        "folder-then-url-not-joined",
    ],
)
def test_redact_paths(text: str, expected: str) -> None:
    assert redact_paths(text) == expected


def test_a_markdown_link_to_a_local_file_keeps_only_its_label() -> None:
    converted = to_slack_mrkdwn("see [config.py](/Users/someone/x/config.py)")

    assert redact_paths(converted) == "see config.py"
