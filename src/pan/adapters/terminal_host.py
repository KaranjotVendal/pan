from __future__ import annotations

from collections.abc import Callable

from pan.adapters.hc import ShellHcAdapter
from pan.models import TerminalHostKind
from pan.seams import TerminalHost

# One adapter constructor per supported terminal host (config key `terminal_host`).
_TERMINAL_HOST_ADAPTERS: dict[TerminalHostKind, Callable[[], TerminalHost]] = {
    TerminalHostKind.HC: ShellHcAdapter,
}


def build_terminal_host(kind: TerminalHostKind) -> TerminalHost:
    return _TERMINAL_HOST_ADAPTERS[kind]()
