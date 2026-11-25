from __future__ import annotations

from pan.utils.config import STATUS_LABEL_MAX_LENGTH


def status_label(task: str, max_length: int = STATUS_LABEL_MAX_LENGTH) -> str:
    # The task on one line: whitespace collapsed, cut short with "..." past max_length.
    short_task = " ".join(task.split())
    if len(short_task) > max_length:
        short_task = short_task[: max_length - 3].rstrip() + "..."
    return short_task
