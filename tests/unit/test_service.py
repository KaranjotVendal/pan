from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
from service_manager_fakes import FakeServiceManager

from pan.errors import ServiceError
from pan.models import LaunchdJobState, ServiceStatus
from pan.service import (
    UninstallResult,
    install_services,
    packaged_templates,
    render_plist,
    resolve_pan_binary,
    service_label,
    service_statuses,
    uninstall_services,
)

_LABELS = ("com.pan.gateway", "com.pan.watcher", "com.pan.up")
_PAN_BINARY = Path("/opt/tools/bin/pan")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def manager(tmp_path: Path) -> FakeServiceManager:
    return FakeServiceManager(tmp_path / "LaunchAgents")


@pytest.mark.parametrize(
    ("name", "expected_keep_alive"),
    [("gateway", True), ("watcher", True), ("up", {"SuccessfulExit": False})],
)
def test_packaged_templates_render_paths_and_keep_their_semantics(
    home: Path, name: str, expected_keep_alive: object
) -> None:
    template = (packaged_templates() / f"com.pan.{name}.plist.template").read_text()

    rendered = render_plist(template, home=home, pan_binary=_PAN_BINARY)

    assert "<PAN_" not in rendered
    plist = plistlib.loads(rendered.encode())
    assert plist["Label"] == f"com.pan.{name}"
    assert plist["ProgramArguments"] == [str(_PAN_BINARY), name]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == expected_keep_alive
    assert plist["StandardOutPath"] == f"{home}/.pan/logs/{name}.out.log"
    assert plist["StandardErrorPath"] == f"{home}/.pan/logs/{name}.err.log"
    assert plist["EnvironmentVariables"]["PATH"].startswith(f"{home}/.local/bin:")


def test_render_plist_escapes_xml_special_characters_in_paths() -> None:
    template = (packaged_templates() / "com.pan.gateway.plist.template").read_text()
    home = Path("/Users/a & b")

    plist = plistlib.loads(render_plist(template, home=home, pan_binary=_PAN_BINARY).encode())

    assert plist["StandardOutPath"] == "/Users/a & b/.pan/logs/gateway.out.log"


def test_render_plist_rejects_a_template_that_is_not_a_plist(home: Path) -> None:
    with pytest.raises(ServiceError, match="not a valid plist"):
        render_plist("<plist><dict>", home=home, pan_binary=_PAN_BINARY)


@pytest.mark.parametrize(
    ("found_on_path", "expected_binary"),
    [
        ("/opt/tools/bin/pan", Path("/opt/tools/bin/pan")),
        (None, Path("/Users/example/.local/bin/pan")),
    ],
    ids=["on-path", "fallback"],
)
def test_resolve_pan_binary(found_on_path: str | None, expected_binary: Path) -> None:
    resolved = resolve_pan_binary(Path("/Users/example"), which=lambda _name: found_on_path)

    assert resolved == expected_binary


def test_install_writes_and_loads_agents_in_order(home: Path, manager: FakeServiceManager) -> None:
    labels = install_services(manager, home=home, pan_binary=_PAN_BINARY)

    assert labels == list(_LABELS)
    assert manager.calls == [
        call for label in _LABELS for call in (("write", label), ("bootstrap", label))
    ]
    assert manager.directories == [home / ".pan" / "logs"]
    assert set(manager.loaded) == set(_LABELS)


def test_install_again_boots_out_loaded_agents_before_reloading(
    home: Path, manager: FakeServiceManager
) -> None:
    install_services(manager, home=home, pan_binary=_PAN_BINARY)
    manager.calls.clear()

    install_services(manager, home=home, pan_binary=Path("/new/bin/pan"))

    assert manager.calls == [
        call
        for label in _LABELS
        for call in (("bootout", label), ("write", label), ("bootstrap", label))
    ]
    assert "/new/bin/pan" in manager.plists["com.pan.up"]


def test_install_with_a_missing_template_changes_nothing(
    tmp_path: Path, home: Path, manager: FakeServiceManager
) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "com.pan.gateway.plist.template").write_text(
        (packaged_templates() / "com.pan.gateway.plist.template").read_text()
    )

    with pytest.raises(ServiceError, match="template missing for watcher"):
        install_services(manager, home=home, pan_binary=_PAN_BINARY, templates=templates)

    assert manager.calls == []
    assert manager.directories == []


def test_install_surfaces_a_launchctl_failure(home: Path, manager: FakeServiceManager) -> None:
    manager.fail_bootstrap_for = "com.pan.watcher"

    with pytest.raises(ServiceError, match="com.pan.watcher"):
        install_services(manager, home=home, pan_binary=_PAN_BINARY)

    assert "com.pan.up" not in manager.plists


def test_uninstall_unloads_and_deletes_in_reverse_order(
    home: Path, manager: FakeServiceManager
) -> None:
    install_services(manager, home=home, pan_binary=_PAN_BINARY)
    manager.calls.clear()

    results = uninstall_services(manager)

    assert manager.calls == [
        call for label in reversed(_LABELS) for call in (("bootout", label), ("remove", label))
    ]
    assert all(result.was_loaded and result.plist_removed for result in results)
    assert manager.plists == {}
    assert manager.loaded == {}


def test_uninstall_when_nothing_is_installed_skips_bootout(manager: FakeServiceManager) -> None:
    results = uninstall_services(manager)

    assert results == [
        UninstallResult(name=name, label=service_label(name), was_loaded=False, plist_removed=False)
        for name in ("up", "watcher", "gateway")
    ]
    assert ("bootout", "com.pan.up") not in manager.calls


def test_service_statuses_reports_each_agent(manager: FakeServiceManager) -> None:
    manager.plists["com.pan.gateway"] = "<plist/>"
    manager.plists["com.pan.up"] = "<plist/>"
    manager.loaded["com.pan.gateway"] = LaunchdJobState(pid=412, last_exit_code=0)
    manager.loaded["com.pan.up"] = LaunchdJobState(pid=None, last_exit_code=0)

    statuses = service_statuses(manager)

    assert statuses == [
        ServiceStatus(
            name="gateway",
            label="com.pan.gateway",
            plist_installed=True,
            loaded=True,
            pid=412,
            last_exit_code=0,
        ),
        ServiceStatus(name="watcher", label="com.pan.watcher", plist_installed=False, loaded=False),
        ServiceStatus(
            name="up", label="com.pan.up", plist_installed=True, loaded=True, last_exit_code=0
        ),
    ]
