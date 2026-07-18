from __future__ import annotations

import pytest

from pan.power import should_caffeinate


@pytest.mark.parametrize(
    "active_worker_count, on_ac_power, expected",
    [
        (1, True, True),  # on AC with a worker: hold the machine awake
        (3, True, True),  # on AC with several workers: still awake
        (0, True, False),  # idle on AC: let it sleep, do not force wake for nothing
        (1, False, False),  # on battery: never caffeinate regardless of workers
        (5, False, False),  # on battery with many workers: still no caffeinate
        (0, False, False),  # idle on battery
    ],
)
def test_should_caffeinate(active_worker_count: int, on_ac_power: bool, expected: bool) -> None:
    assert should_caffeinate(active_worker_count, on_ac_power) is expected
