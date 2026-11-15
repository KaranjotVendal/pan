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


class HcError(PanError): ...


# The terminal does not exist (hc kind not_found) or its program has ended (kind finished).
class TerminalGoneError(HcError): ...


# The terminal is waiting on a person (a permission prompt or a question; hc kind blocked).
# pan never forces input past it.
class TargetBlockedError(HcError): ...


# A live terminal already holds the requested name (hc kind name_taken).
class TerminalNameTakenError(HcError): ...


class SlackPostError(PanError): ...


class GatedOpDeniedError(PanError): ...


class TargetNotFoundError(PanError): ...


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
