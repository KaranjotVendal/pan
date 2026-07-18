from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from pan.errors import ConfigMissingError
from pan.logging import initialise_logger
from pan.models import PanConfig

logger = initialise_logger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pan" / "config.json"


def _expand_user_paths(raw_config: dict) -> None:
    paths = raw_config.get("paths")
    if isinstance(paths, dict):
        for key, value in list(paths.items()):
            if isinstance(value, str):
                paths[key] = str(Path(value).expanduser())

    orchestrator = raw_config.get("orchestrator")
    if isinstance(orchestrator, dict):
        worktree_base = orchestrator.get("worktree_base")
        if isinstance(worktree_base, str):
            orchestrator["worktree_base"] = str(Path(worktree_base).expanduser())


def load_config(path: Path | None = None) -> PanConfig:
    config_path = path if path is not None else DEFAULT_CONFIG_PATH

    try:
        raw_text = config_path.read_text()
    except FileNotFoundError as error:
        raise ConfigMissingError(f"config file not found: {config_path}") from error

    try:
        raw_config = json.loads(raw_text)
    except json.JSONDecodeError as error:
        raise ConfigMissingError(f"config file is not valid JSON: {config_path}") from error

    if not isinstance(raw_config, dict):
        raise ConfigMissingError(f"config file must be a JSON object: {config_path}")

    _expand_user_paths(raw_config)

    try:
        config = PanConfig.model_validate(raw_config)
    except ValidationError as error:
        raise ConfigMissingError(
            f"config file is missing required fields: {config_path}"
        ) from error

    logger.info(f"config loaded path={config_path}")
    return config
