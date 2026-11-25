from __future__ import annotations

import plistlib
import shutil
from collections.abc import Callable
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from xml.parsers.expat import ExpatError
from xml.sax.saxutils import escape

from pydantic import BaseModel

from pan.core.errors import ServiceError
from pan.core.models import ServiceStatus
from pan.core.seams import ServiceManager
from pan.utils.config import (
    LAUNCHD_BINARY_PLACEHOLDER,
    LAUNCHD_HOME_PLACEHOLDER,
    LAUNCHD_LABEL_PREFIX,
    LAUNCHD_SERVICE_NAMES,
    LAUNCHD_TEMPLATE_SUFFIX,
)
from pan.utils.logging import initialise_logger

logger = initialise_logger(__name__)

_PAN_EXECUTABLE = "pan"


class UninstallResult(BaseModel, frozen=True):
    name: str
    label: str
    was_loaded: bool
    plist_removed: bool


def service_label(name: str) -> str:
    return f"{LAUNCHD_LABEL_PREFIX}{name}"


def packaged_templates() -> Traversable:
    # Shipped inside the package so an installed pan (uv tool install) finds them too.
    return files("pan") / "launchd"


def pan_log_directory(home: Path) -> Path:
    # The templates send each agent's stdout/stderr to <home>/.pan/logs; launchd does not
    # create that folder.
    return home / ".pan" / "logs"


def resolve_pan_binary(home: Path, which: Callable[[str], str | None] | None = None) -> Path:
    # The `pan` on PATH, as an absolute path (launchd has no PATH lookup for the program);
    # else where `uv tool install` puts it.
    found = (which if which is not None else shutil.which)(_PAN_EXECUTABLE)
    if found is not None:
        return Path(found).absolute()
    return home / ".local" / "bin" / _PAN_EXECUTABLE


def render_plist(template: str, *, home: Path, pan_binary: Path) -> str:
    rendered = template.replace(LAUNCHD_HOME_PLACEHOLDER, escape(str(home))).replace(
        LAUNCHD_BINARY_PLACEHOLDER, escape(str(pan_binary))
    )
    try:
        plistlib.loads(rendered.encode())
    except (plistlib.InvalidFileException, ExpatError, ValueError) as error:
        raise ServiceError(f"rendered launchd plist is not a valid plist: {error}") from error
    return rendered


def _render_all(templates: Traversable, *, home: Path, pan_binary: Path) -> dict[str, str]:
    rendered_by_name: dict[str, str] = {}
    for name in LAUNCHD_SERVICE_NAMES:
        template_file = templates / f"{service_label(name)}{LAUNCHD_TEMPLATE_SUFFIX}"
        try:
            template = template_file.read_text()
        except OSError as error:
            raise ServiceError(f"launchd template missing for {name}: {template_file}") from error
        rendered_by_name[name] = render_plist(template, home=home, pan_binary=pan_binary)
    return rendered_by_name


def install_services(
    manager: ServiceManager,
    *,
    home: Path,
    pan_binary: Path,
    templates: Traversable | None = None,
) -> list[str]:
    # Every template is rendered before anything is written, so a bad template changes
    # nothing. A loaded agent is booted out first, so installing again reloads the new plist.
    rendered_by_name = _render_all(
        templates if templates is not None else packaged_templates(),
        home=home,
        pan_binary=pan_binary,
    )
    manager.ensure_directory(pan_log_directory(home))
    installed_labels: list[str] = []
    for name in LAUNCHD_SERVICE_NAMES:
        label = service_label(name)
        if manager.job_state(label) is not None:
            logger.info(f"reinstalling loaded launchd agent label={label}")
            manager.bootout(label)
        plist_path = manager.write_plist(label, rendered_by_name[name])
        manager.bootstrap(plist_path)
        installed_labels.append(label)
    logger.info(f"pan services installed labels={installed_labels} pan_binary={pan_binary}")
    return installed_labels


def uninstall_services(manager: ServiceManager) -> list[UninstallResult]:
    # The reverse of install order, so `up` never runs against a gateway being removed.
    results: list[UninstallResult] = []
    for name in reversed(LAUNCHD_SERVICE_NAMES):
        label = service_label(name)
        was_loaded = manager.job_state(label) is not None
        if was_loaded:
            manager.bootout(label)
        plist_removed = manager.remove_plist(label)
        results.append(
            UninstallResult(
                name=name, label=label, was_loaded=was_loaded, plist_removed=plist_removed
            )
        )
    logger.info(f"pan services uninstalled results={[result.label for result in results]}")
    return results


def service_statuses(manager: ServiceManager) -> list[ServiceStatus]:
    statuses: list[ServiceStatus] = []
    for name in LAUNCHD_SERVICE_NAMES:
        label = service_label(name)
        job_state = manager.job_state(label)
        statuses.append(
            ServiceStatus(
                name=name,
                label=label,
                plist_installed=manager.plist_installed(label),
                loaded=job_state is not None,
                pid=job_state.pid if job_state is not None else None,
                last_exit_code=job_state.last_exit_code if job_state is not None else None,
            )
        )
    return statuses
