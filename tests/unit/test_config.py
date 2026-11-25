from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from pan.core.errors import ConfigMissingError
from pan.core.models import TerminalHostKind
from pan.utils.config import load_config


def _minimal_config() -> dict:
    return {
        "orchestrator": {
            "cwd": "/Users/me/dev/pan",
            "worktree_base": "/Users/me/dev/pan-worktrees",
        },
        "paths": {
            "inbox": "/Users/me/.pan/inbox",
            "threads": "/Users/me/.pan/threads.json",
            "credentials": "/Users/me/.pan/credentials.json",
        },
    }


def _write_config(tmp_path: Path, data: dict) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data))
    return config_path


def test_load_config_validates_into_pan_config(tmp_path: Path) -> None:
    data = _minimal_config()
    data["orchestrator"]["terminal"] = "t42"
    data["orchestrator"]["app_space"] = "sp123"
    data["users"] = {"U1": {"channels": ["C1"]}}

    config = load_config(_write_config(tmp_path, data))

    assert config.orchestrator.terminal == "t42"
    assert config.orchestrator.app_space == "sp123"
    assert config.users["U1"].channels == ["C1"]


def test_missing_file_raises_config_missing_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigMissingError):
        load_config(tmp_path / "does-not-exist.json")


def test_omitted_optional_sections_fall_back_to_defaults(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, _minimal_config()))

    assert config.slack.socket_mode is True
    assert config.orchestrator.terminal == "pan-orchestrator"
    assert config.orchestrator.app_space is None
    assert config.orchestrator.command == ["claude"]
    assert config.users == {}


def test_missing_required_field_raises_config_missing_error(tmp_path: Path) -> None:
    data = _minimal_config()
    del data["orchestrator"]

    with pytest.raises(ConfigMissingError):
        load_config(_write_config(tmp_path, data))


def test_tilde_paths_expand_to_absolute(tmp_path: Path) -> None:
    data = _minimal_config()
    data["paths"]["inbox"] = "~/.pan/inbox"
    data["orchestrator"]["worktree_base"] = "~/dev/pan-worktrees"
    data["orchestrator"]["cwd"] = "~/dev/pan"

    config = load_config(_write_config(tmp_path, data))

    assert config.paths.inbox.is_absolute()
    assert "~" not in str(config.paths.inbox)
    assert config.paths.inbox == Path.home() / ".pan" / "inbox"
    assert config.orchestrator.worktree_base == Path.home() / "dev" / "pan-worktrees"
    assert config.orchestrator.cwd == Path.home() / "dev" / "pan"


def test_herdr_era_pane_id_names_its_replacement(tmp_path: Path) -> None:
    data = _minimal_config()
    data["orchestrator"]["pane_id"] = "w1:p1"

    with pytest.raises(ConfigMissingError, match=r"orchestrator\.terminal"):
        load_config(_write_config(tmp_path, data))


@pytest.mark.parametrize(
    "terminal_host_entry", [{}, {"terminal_host": "hc"}], ids=["omitted", "explicit-hc"]
)
def test_terminal_host_defaults_to_hc(tmp_path: Path, terminal_host_entry: dict) -> None:
    data = {**_minimal_config(), **terminal_host_entry}

    config = load_config(_write_config(tmp_path, data))

    assert config.terminal_host is TerminalHostKind.HC


@pytest.mark.parametrize("unknown_terminal_host", ["rex", "HC", "", 42, None])
def test_unknown_terminal_host_is_a_clear_config_error(
    tmp_path: Path, unknown_terminal_host: object
) -> None:
    data = {**_minimal_config(), "terminal_host": unknown_terminal_host}

    with pytest.raises(ConfigMissingError, match=r"terminal_host .* is not supported"):
        load_config(_write_config(tmp_path, data))


def test_malformed_json_raises_config_missing_error(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("{ not valid json")

    with pytest.raises(ConfigMissingError):
        load_config(config_path)


@pytest.mark.parametrize("non_object_json", ["[]", '"just a string"', "42"])
def test_non_object_json_raises_config_missing_error(tmp_path: Path, non_object_json: str) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(non_object_json)

    with pytest.raises(ConfigMissingError):
        load_config(config_path)


def _set_removed_defaults(data: dict) -> None:
    data["defaults"] = {"agent": "secret-default-value"}


def _set_removed_gated_ops(data: dict) -> None:
    data["gated_ops"] = ["secret-gated-op"]


def _set_removed_paths_logs(data: dict) -> None:
    data["paths"]["logs"] = "/secret/logs/path"


def _set_removed_user_autonomy(data: dict) -> None:
    data["users"] = {"U1": {"channels": ["C1"]}, "U2": {"autonomy": "secret-autonomy"}}


def _set_removed_user_repos(data: dict) -> None:
    data["users"] = {"U1": {"repos": ["secret-repo"]}}


def _no_removed_keys(data: dict) -> None:
    data["users"] = {"U1": {"channels": ["C1"]}}


@pytest.mark.parametrize(
    "add_keys, expected_key_name",
    [
        (_set_removed_defaults, "defaults"),
        (_set_removed_gated_ops, "gated_ops"),
        (_set_removed_paths_logs, "paths.logs"),
        (_set_removed_user_autonomy, "users.*.autonomy"),
        (_set_removed_user_repos, "users.*.repos"),
        (_no_removed_keys, None),
    ],
    ids=["defaults", "gated-ops", "paths-logs", "user-autonomy", "user-repos", "none"],
)
def test_removed_config_keys_are_named_in_one_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    add_keys: Callable[[dict], None],
    expected_key_name: str | None,
) -> None:
    data = _minimal_config()
    add_keys(data)
    config_path = _write_config(tmp_path, data)

    # propagate=False on this logger, so attach caplog's handler directly.
    config_logger = logging.getLogger("pan.utils.config")
    config_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="pan.utils.config"):
            load_config(config_path)
    finally:
        config_logger.removeHandler(caplog.handler)

    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    if expected_key_name is None:
        assert warnings == []
        return
    assert len(warnings) == 1
    assert expected_key_name in warnings[0]
    assert "ignored" in warnings[0]
    assert "secret" not in warnings[0]
