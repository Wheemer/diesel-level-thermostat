"""Pure control calculations for Diesel Level Thermostat."""

from __future__ import annotations

import math


def target_level(
    *,
    current_temperature: float,
    target_temperature: float,
    min_level: int,
    max_level: int,
    proportional_band: float,
) -> int:
    """Map a positive temperature error to a discrete heater level."""
    if min_level < 1 or max_level < min_level:
        raise ValueError("Invalid level range")
    if proportional_band <= 0:
        raise ValueError("Proportional band must be positive")

    error = target_temperature - current_temperature
    if error <= 0:
        return min_level

    span = max_level - min_level
    # Unit conversion can turn an exact discrete boundary into a value such as
    # 5.000000000000001. Keep that boundary at its intended level.
    scaled = math.ceil((error / proportional_band) * span - 1e-9)
    return min(max_level, max(min_level, min_level + scaled))


def should_start(
    *, current_temperature: float, target_temperature: float, cold_tolerance: float
) -> bool:
    """Return whether room temperature is sufficiently below target to heat."""
    return current_temperature < target_temperature - cold_tolerance


def should_stop(
    *, current_temperature: float, target_temperature: float, hot_tolerance: float
) -> bool:
    """Return whether room temperature is sufficiently above target to stop."""
    return current_temperature >= target_temperature + hot_tolerance
