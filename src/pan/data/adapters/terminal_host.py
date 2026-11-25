from __future__ import annotations

from collections.abc import Callable

from pan.core.models import TerminalHostKind
from pan.core.seams import TerminalHost
from pan.data.adapters.hc import ShellHcAdapter

# One adapter constructor per supported terminal host (config key `terminal_host`).
_TERMINAL_HOST_ADAPTERS: dict[TerminalHostKind, Callable[[], TerminalHost]] = {
    TerminalHostKind.HC: ShellHcAdapter,
}


def build_terminal_host(kind: TerminalHostKind) -> TerminalHost:
    return _TERMINAL_HOST_ADAPTERS[kind]()
