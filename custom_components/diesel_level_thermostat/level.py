"""Validate public Level controls without inferring controller-specific gears."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from .const import DEFAULT_MAX_LEVEL


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def level_count(hass: HomeAssistant, entity_id: str) -> int | None:
    source = hass.states.get(entity_id)
    if source is None:
        return None
    if entity_id.startswith("number."):
        minimum = finite_number(source.attributes.get("min"))
        maximum = finite_number(source.attributes.get("max"))
        step = finite_number(source.attributes.get("step"))
        if minimum != 1 or maximum is None or step != 1:
            return None
        return int(maximum) if maximum.is_integer() and 1 <= maximum <= DEFAULT_MAX_LEVEL else None
    if not entity_id.startswith("fan."):
        return None
    step = finite_number(source.attributes.get("percentage_step", 10))
    if step is None or not 100 / DEFAULT_MAX_LEVEL <= step <= 100:
        return None
    count = round(100 / step)
    return (
        count
        if (
            1 <= count <= DEFAULT_MAX_LEVEL
            and math.isclose(step, 100 / count, rel_tol=1e-5, abs_tol=1e-6)
        )
        else None
    )


def current_level(hass: HomeAssistant, entity_id: str) -> int | None:
    source = hass.states.get(entity_id)
    count = level_count(hass, entity_id)
    if entity_id.startswith("number."):
        value = finite_number(source.state) if source else None
        if value is None or count is None or not value.is_integer() or not 1 <= value <= count:
            return None
        return int(value)
    percentage = finite_number(source.attributes.get("percentage")) if source else None
    if percentage is None or not 0 < percentage <= 100 or count is None:
        return None
    return max(1, min(count, math.ceil(percentage * count / 100)))


def level_unavailable_reason(hass: HomeAssistant, entity_id: str) -> str | None:
    source = hass.states.get(entity_id)
    if not source or source.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return "source_level_unavailable"
    if level_count(hass, entity_id) is None:
        return "source_level_range_invalid"
    if entity_id.startswith("number."):
        return "source_level_invalid" if current_level(hass, entity_id) is None else None
    if source.state not in ("on", "off"):
        return "source_level_invalid"
    percentage = source.attributes.get("percentage")
    if percentage is not None and (
        (value := finite_number(percentage)) is None or not 0 <= value <= 100
    ):
        return "source_level_invalid"
    return None
