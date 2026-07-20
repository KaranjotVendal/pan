from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pan.models import LiveSession


class PanError(Exception): ...


class UnauthorizedSenderError(PanError): ...


class ConfigMissingError(PanError): ...


class CredentialsError(PanError): ...


class InboxError(PanError): ...


class ThreadNotFoundError(PanError): ...


class SpawnError(PanError): ...


class HerdrError(PanError): ...


class MorcliError(PanError): ...


class SlackPostError(PanError): ...


class GatedOpDeniedError(PanError): ...


class TargetNotFoundError(PanError): ...


class TargetAmbiguousError(PanError):
    # Carries the ambiguous candidates so the CLI boundary and the orchestrator can list
    # label + workspace_id + pane_id for the user to re-target by a precise id. str(self)
    # renders that candidate list.
    def __init__(self, selector: str, candidates: list[LiveSession]) -> None:
        self.selector = selector
        self.candidates = candidates
        rendered = "; ".join(
            f"{candidate.workspace_name} "
            f"(workspace_id={candidate.workspace_id}, pane_id={candidate.pane_id})"
            for candidate in candidates
        )
        super().__init__(f"ambiguous selector '{selector}': {rendered}")
