"""Discover telemetry belonging to the selected source heater device."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CASE_TEMPERATURE,
    CONF_EXHAUST_TEMPERATURE,
    CONF_FAULT_SENSOR,
    CONF_ROOM_TEMPERATURE,
    CONF_VOLTAGE_SENSOR,
)

# These are integration-local counters and estimates, not controller telemetry.
_ESTIMATE_ENTITY_PARTS = (
    "hourly_fuel",
    "daily_fuel",
    "total_fuel",
    "fuel_remaining",
    "last_refueled",
    "fuel_consumed",
    "daily_runtime",
    "total_runtime",
)

_CONTROL_ENTITY_SUFFIXES = {
    "heater_power": ("switch", "_power"),
    "heater_level": ("fan", "_heater_level"),
    "running_mode": ("select", "_running_mode"),
}

_TELEMETRY_ENTITY_SUFFIXES = {
    CONF_ROOM_TEMPERATURE: (("sensor", "_cab_temp"),),
    CONF_FAULT_SENSOR: (("binary_sensor", "_problem"),),
    CONF_VOLTAGE_SENSOR: (("sensor", "_supply_voltage"),),
    CONF_CASE_TEMPERATURE: (("sensor", "_case_temp"),),
    # HeatGenie exposes these only when the controller reports the probe.
    CONF_EXHAUST_TEMPERATURE: (
        ("sensor", "_outlet_temperature"),
        ("sensor", "_intake_temperature"),
    ),
}


def source_device_id(
    hass: HomeAssistant, control_entity_ids: Iterable[str]
) -> str | None:
    """Return the common source device for the selected control entities."""
    registry = er.async_get(hass)
    entries = [registry.async_get(entity_id) for entity_id in control_entity_ids]
    if not entries or any(
        entry is None or entry.device_id is None for entry in entries
    ):
        return None
    device_ids = {entry.device_id for entry in entries}
    return next(iter(device_ids)) if len(device_ids) == 1 else None


def source_telemetry_entity_ids(
    hass: HomeAssistant, device_id: str | None
) -> tuple[str, ...]:
    """Return physical sensor entities belonging to the selected source device."""
    if not device_id:
        return ()
    registry = er.async_get(hass)
    return tuple(
        entry.entity_id
        for entry in er.async_entries_for_device(registry, device_id)
        if entry.domain in {"sensor", "binary_sensor"}
        and not any(part in entry.entity_id for part in _ESTIMATE_ENTITY_PARTS)
    )


def source_control_entity_ids(
    hass: HomeAssistant, device_id: str
) -> dict[str, str] | None:
    """Resolve the source integration's public Level-Mode control contract."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_device(registry, device_id)
    controls: dict[str, str] = {}
    for key, (domain, suffix) in _CONTROL_ENTITY_SUFFIXES.items():
        matches = [
            entry.entity_id
            for entry in entries
            if entry.domain == domain and entry.unique_id.endswith(suffix)
        ]
        if len(matches) != 1:
            return None
        controls[key] = matches[0]
    return controls


def source_telemetry_defaults(hass: HomeAssistant, device_id: str) -> dict[str, str]:
    """Resolve the physical telemetry the selected heater actually exposes."""
    registry = er.async_get(hass)
    entries = er.async_entries_for_device(registry, device_id)
    defaults: dict[str, str] = {}
    for key, candidates in _TELEMETRY_ENTITY_SUFFIXES.items():
        for domain, suffix in candidates:
            matches = [
                entry.entity_id
                for entry in entries
                if entry.domain == domain and entry.unique_id.endswith(suffix)
            ]
            if len(matches) == 1:
                defaults[key] = matches[0]
                break
    return defaults
