from __future__ import annotations

import pytest

from pan.core.models import TerminalHostKind
from pan.data.adapters.hc import ShellHcAdapter
from pan.data.adapters.terminal_host import build_terminal_host


@pytest.mark.parametrize("kind, expected_adapter", [(TerminalHostKind.HC, ShellHcAdapter)])
def test_build_terminal_host_returns_the_configured_adapter(
    kind: TerminalHostKind, expected_adapter: type
) -> None:
    assert isinstance(build_terminal_host(kind), expected_adapter)


def test_every_terminal_host_kind_has_an_adapter() -> None:
    for kind in TerminalHostKind:
        build_terminal_host(kind)
