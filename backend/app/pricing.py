"""Language pricing and the rate captured when a task is charged."""

from typing import Any

from .config import POINTS_PER_MINUTE


ENGLISH_POINTS_PER_MINUTE = 8


def language_rate(source_language: str, output_language: str) -> int:
    if source_language == "en" or output_language in {"en", "both"}:
        return ENGLISH_POINTS_PER_MINUTE
    return POINTS_PER_MINUTE


def task_rate(task: Any) -> int:
    # Tasks created before language pricing were all charged at the base rate.
    values = dict(task)
    return int(values.get("points_per_minute") or values.get("pointsPerMinute") or POINTS_PER_MINUTE)
