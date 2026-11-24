from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pan.models import Terminal


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


# `pan send --to <selector>` found no exact match (or several sessions share the label), so
# nothing was sent and nothing was posted: the orchestrator proposes a session with
# `pan propose` and the person confirms it in the thread.
class NoExactMatchError(PanError): ...


# The session was not started by pan (a desk session): pan never stops, removes or reopens
# it.
class NotPanSessionError(PanError): ...


# The session is busy (working, or in a state pan cannot read), so nothing was typed into
# it: typing could land in the middle of what someone is doing there.
class SessionBusyError(PanError): ...


# `done` found work that closing the session would lose (uncommitted files, or commits not
# pushed or merged), so it kept the session, its worktree and its branch.
class UnsavedWorkError(PanError): ...


# A git command reading or cleaning up a worker's worktree or branch failed. Retrying may
# work (the cleanup picks up where it stopped).
class GitWorktreeError(PanError): ...


# `done` will never remove this folder: it is outside pan's worktree folder, or it is a
# repository's main worktree. Retrying cannot change that.
class UnsafeWorktreeError(PanError): ...


# `pan sessions` could not send the list to the requester privately (an ephemeral message),
# so nothing was posted: the orchestrator says so in the thread, without the list.
class PrivateMessageError(PanError): ...


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
