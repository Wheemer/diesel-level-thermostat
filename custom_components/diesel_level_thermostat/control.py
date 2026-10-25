"""Pure level-selection calculation for Diesel Level Thermostat."""

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
    """Map a positive temperature gap to a discrete heater level."""
    if min_level < 1 or max_level < min_level:
        raise ValueError("Invalid level range")
    if proportional_band <= 0:
        raise ValueError("Proportional band must be positive")
    error = target_temperature - current_temperature
    if error <= 0:
        return min_level
    span = max_level - min_level
    scaled = math.ceil((error / proportional_band) * span - 1e-9)
    return min(max_level, max(min_level, min_level + scaled))
