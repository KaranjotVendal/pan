from __future__ import annotations

import pytest

from pan.utils.text import status_label


@pytest.mark.parametrize(
    "task, max_length, expected",
    [
        ("fix the login page", 80, "fix the login page"),
        ("  fix\n the   login\tpage ", 80, "fix the login page"),
        ("fix the login page", 10, "fix the..."),
        ("abcdefghij", 10, "abcdefghij"),
        ("", 80, ""),
    ],
    ids=["short", "whitespace-collapsed", "cut", "exactly-max", "empty"],
)
def test_status_label(task: str, max_length: int, expected: str) -> None:
    assert status_label(task, max_length) == expected
