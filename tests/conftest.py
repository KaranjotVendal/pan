from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from pan.utils.logging import LOG_DIR_ENV

_test_log_dir: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    # Runs before any test module imports pan, so every module-level logger writes its file
    # handler into a throwaway folder instead of the real ~/.pan/logs/pan.log.
    global _test_log_dir
    _test_log_dir = tempfile.mkdtemp(prefix="pan-test-logs-")
    os.environ[LOG_DIR_ENV] = _test_log_dir


def pytest_unconfigure(config: pytest.Config) -> None:
    if _test_log_dir is not None:
        shutil.rmtree(_test_log_dir, ignore_errors=True)
