from __future__ import annotations


def should_caffeinate(active_worker_count: int, on_ac_power: bool) -> bool:
    # Hold the machine awake (caffeinate + pmset disablesleep) only while there is real
    # work to protect AND we are on AC. On battery we never force-wake — lid-closed
    # execution needs AC (R-3) — and when idle we let the machine sleep rather than
    # burn power for nothing.
    return on_ac_power and active_worker_count >= 1
