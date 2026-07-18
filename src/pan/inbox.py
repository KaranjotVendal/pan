from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from pan.errors import InboxError
from pan.logging import initialise_logger
from pan.models import InboxItem

logger = initialise_logger(__name__)

_CLAIMED_SUFFIX = ".claimed"
_CORRUPT_SUFFIX = ".corrupt"
_PATH_SEPARATORS = ("/", "\\", "\x00")


def _require_safe_event_id(event_id: str) -> None:
    # The event id becomes a filename, so it must be a single, separator-free path
    # segment. A Slack event id is server-generated ("Ev0..."), but the durable inbox
    # is itself a trust boundary, so reject anything that could escape the directory.
    if (
        not event_id
        or event_id in {".", ".."}
        or any(separator in event_id for separator in _PATH_SEPARATORS)
        or event_id != Path(event_id).name
    ):
        raise InboxError(f"unsafe inbox event id={event_id!r}")


class FileInboxStore:
    def __init__(self, inbox_dir: Path) -> None:
        self._inbox_dir = inbox_dir

    def append(self, item: InboxItem) -> None:
        _require_safe_event_id(item.id)
        self._inbox_dir.mkdir(parents=True, exist_ok=True)
        entry_path = self._inbox_dir / f"{item.id}.json"

        # The event id is the filename, so a redelivered event (same id) before a
        # drain resolves to the same path and is drained exactly once (INV-6).
        if entry_path.exists():
            logger.info(f"inbox append id={item.id} skipped=duplicate")
            return

        temp_path = self._inbox_dir / f".{item.id}.json.tmp"
        try:
            temp_path.write_text(item.model_dump_json())
            temp_path.replace(entry_path)
        except OSError as error:
            temp_path.unlink(missing_ok=True)
            raise InboxError(f"failed to append inbox item id={item.id}") from error

        logger.info(f"inbox append id={item.id} channel={item.channel}")

    def drain(self) -> list[InboxItem]:
        if not self._inbox_dir.exists():
            logger.info("inbox drain count=0")
            return []

        # Claim each entry with an atomic rename first so a concurrent gateway
        # append (or another drainer) cannot clobber or double-return it (R-6).
        # Track (original_entry, claim) so a malformed sibling can be un-claimed.
        claims: list[tuple[Path, Path]] = []
        for entry_path in sorted(self._inbox_dir.glob("*.json")):
            claim_path = entry_path.with_name(entry_path.name + _CLAIMED_SUFFIX)
            try:
                entry_path.rename(claim_path)
            except FileNotFoundError:
                continue
            claims.append((entry_path, claim_path))

        items: list[InboxItem] = []
        malformed_names: list[str] = []
        for entry_path, claim_path in claims:
            try:
                items.append(InboxItem.model_validate_json(claim_path.read_text()))
            except (OSError, ValidationError) as error:
                # Quarantine only the poison entry so it can't wedge every future
                # drain; restore the valid siblings below and surface the failure.
                logger.warning(
                    f"inbox entry unreadable name={entry_path.name} error={type(error).__name__}"
                )
                claim_path.rename(entry_path.with_name(entry_path.name + _CORRUPT_SUFFIX))
                malformed_names.append(entry_path.name)

        if malformed_names:
            for entry_path, claim_path in claims:
                # Un-claim the entries that parsed cleanly so a later drain retries
                # them — a corrupt neighbour must never silently drop valid events.
                if claim_path.exists():
                    claim_path.rename(entry_path)
            logger.warning(f"inbox drain quarantined malformed count={len(malformed_names)}")
            raise InboxError(f"malformed inbox entries: {malformed_names}")

        items.sort(key=lambda item: (item.received_at, item.id))

        for _entry_path, claim_path in claims:
            claim_path.unlink(missing_ok=True)

        logger.info(f"inbox drain count={len(items)}")
        return items
