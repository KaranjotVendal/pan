from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pan.models import InboxThread, Terminal


class PanError(Exception): ...


class UnauthorizedSenderError(PanError): ...


class ConfigMissingError(PanError): ...


class CredentialsError(PanError): ...


class InboxError(PanError): ...


class ThreadNotFoundError(PanError): ...


class SpawnError(PanError): ...


# Any failure from the terminal host behind the TerminalHost seam. Callers outside the
# adapters catch this; a host adapter may raise its own subclass for host-specific failures
# (the hc adapter raises HcError) and raises the typed kinds below for the cases pan handles.
class TerminalHostError(PanError): ...


# The terminal does not exist or its program has ended.
class TerminalGoneError(TerminalHostError): ...


# The terminal is waiting on a person (a permission prompt or a question).
# pan never forces input past it.
class TargetBlockedError(TerminalHostError): ...


# A live terminal already holds the requested name.
class TerminalNameTakenError(TerminalHostError): ...


class SlackPostError(PanError): ...


class GatedOpDeniedError(PanError): ...


class TargetNotFoundError(PanError): ...


# `to <selector>:` matched an Inbox thread, but no live Claude session runs in the folder of
# the session that writes it.
class ThreadSessionNotRunningError(TargetNotFoundError):
    def __init__(self, thread_name: str) -> None:
        self.thread_name = thread_name
        super().__init__(f"the session writing the Inbox thread '{thread_name}' is not running")


# `to <selector>:` matched more than one open Inbox thread by its words.
class InboxThreadAmbiguousError(PanError):
    def __init__(self, selector: str, threads: list[InboxThread]) -> None:
        self.selector = selector
        self.threads = threads
        names = "; ".join(thread.name for thread in threads)
        super().__init__(f"selector '{selector}' matches more than one Inbox thread: {names}")


# The session was not started by pan (a desk session): pan never stops, removes or reopens
# it.
class NotPanSessionError(PanError): ...


# The session is busy (working, or in a state pan cannot read), so nothing was typed into
# it: typing could land in the middle of what someone is doing there.
class SessionBusyError(PanError): ...


# Installing, loading, unloading or reading pan's launchd agents failed (`pan service`).
class ServiceError(PanError): ...


class TargetAmbiguousError(PanError):
    # Carries the ambiguous candidates so the CLI boundary and the orchestrator can list
    # each one's address, name and Herdr app label for the user to re-target by a precise
    # address or name. str(self) renders that candidate list.
    def __init__(self, selector: str, candidates: list[Terminal]) -> None:
        self.selector = selector
        self.candidates = candidates
        rendered = "; ".join(
            f"{candidate.address} (name={candidate.name or '-'}, where={candidate.where or '-'})"
            for candidate in candidates
        )
        super().__init__(f"ambiguous selector '{selector}': {rendered}")
