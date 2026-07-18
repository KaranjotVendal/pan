from __future__ import annotations

import json
from pathlib import Path

import pytest

from pan.config import load_config
from pan.errors import ConfigMissingError
from pan.models import Agent


def _minimal_config() -> dict:
    return {
        "orchestrator": {
            "pane_id": "%3",
            "worktree_base": "/Users/me/dev/pan-worktrees",
        },
        "defaults": {},
        "paths": {
            "inbox": "/Users/me/.pan/inbox",
            "threads": "/Users/me/.pan/threads.json",
            "logs": "/Users/me/.pan/logs",
            "credentials": "/Users/me/.pan/credentials.json",
        },
    }


def _write_config(tmp_path: Path, data: dict) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(data))
    return config_path


def test_load_config_validates_into_pan_config(tmp_path: Path) -> None:
    data = _minimal_config()
    data["orchestrator"]["workspace_name"] = "pan-orchestrator"
    data["defaults"] = {"agent": "claude"}
    data["users"] = {"U1": {"channels": ["C1"]}}

    config = load_config(_write_config(tmp_path, data))

    assert config.orchestrator.workspace_name == "pan-orchestrator"
    assert config.orchestrator.pane_id == "%3"
    assert config.defaults.agent is Agent.CLAUDE
    assert config.users["U1"].channels == ["C1"]


def test_missing_file_raises_config_missing_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigMissingError):
        load_config(tmp_path / "does-not-exist.json")


def test_omitted_optional_sections_fall_back_to_defaults(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, _minimal_config()))

    assert config.slack.socket_mode is True
    assert config.gated_ops == []
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

    config = load_config(_write_config(tmp_path, data))

    assert config.paths.inbox.is_absolute()
    assert "~" not in str(config.paths.inbox)
    assert config.paths.inbox == Path.home() / ".pan" / "inbox"
    assert config.orchestrator.worktree_base == Path.home() / "dev" / "pan-worktrees"


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
