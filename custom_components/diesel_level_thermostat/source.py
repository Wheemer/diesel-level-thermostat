"""Discover telemetry belonging to the selected source heater device."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_CASE_TEMPERATURE,
    CONF_ENTITY_REGISTRY_IDS,
    CONF_EXHAUST_TEMPERATURE,
    CONF_FAULT_SENSOR,
    CONF_HEATER_LEVEL,
    CONF_HEATER_POWER,
    CONF_ROOM_TEMPERATURE,
    CONF_RUNNING_MODE,
    CONF_VOLTAGE_SENSOR,
    LEVEL_MODE_VALUES,
    OPTIONAL_ENTITY_KEYS,
)
from .level import level_unavailable_reason

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
    CONF_EXHAUST_TEMPERATURE: (("sensor", "_outlet_temperature"),),
}


def _active_source_entry(hass: HomeAssistant, entry) -> bool:
    """Ignore disabled or orphaned registry rows, not merely hidden entities."""
    owner_id = getattr(entry, "config_entry_id", None)
    owner = hass.config_entries.async_get_entry(owner_id) if owner_id else None
    return (
        owner is not None
        and owner.domain == "diesel_heater"
        and getattr(entry, "platform", None) == "diesel_heater"
        and not getattr(entry, "disabled_by", None)
        and getattr(entry, "orphaned_timestamp", None) is None
    )


def source_device_id(hass: HomeAssistant, control_entity_ids: Iterable[str]) -> str | None:
    """Return the common source device for the selected control entities."""
    registry = er.async_get(hass)
    entries = [registry.async_get(entity_id) for entity_id in control_entity_ids]
    if not entries or any(entry is None or entry.device_id is None for entry in entries):
        return None
    device_ids = {entry.device_id for entry in entries}
    return next(iter(device_ids)) if len(device_ids) == 1 else None


def source_telemetry_entity_ids(hass: HomeAssistant, device_id: str | None) -> tuple[str, ...]:
    """Return physical sensor entities belonging to the selected source device."""
    if not device_id:
        return ()
    registry = er.async_get(hass)
    return tuple(
        entry.entity_id
        for entry in er.async_entries_for_device(registry, device_id)
        if entry.domain in {"sensor", "binary_sensor"}
        and _active_source_entry(hass, entry)
        and not any(part in entry.unique_id for part in _ESTIMATE_ENTITY_PARTS)
    )


def source_control_contract(
    hass: HomeAssistant,
    device_id: str,
) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]] | None:
    """Return same-owner/stem controls and optional public burn-off guards."""
    registry = er.async_get(hass)
    entries = [
        entry
        for entry in er.async_entries_for_device(registry, device_id)
        if _active_source_entry(hass, entry)
    ]
    controls: dict[str, str] = {}
    selected = {}
    for key in (CONF_HEATER_POWER, CONF_RUNNING_MODE):
        domain, suffix = _CONTROL_ENTITY_SUFFIXES[key]
        matches = [e for e in entries if e.domain == domain and e.unique_id.endswith(suffix)]
        if len(matches) != 1:
            return None
        selected[key] = matches[0]
        controls[key] = matches[0].entity_id
    power = selected[CONF_HEATER_POWER]
    mode = selected[CONF_RUNNING_MODE]
    stem = power.unique_id.removesuffix("_power")
    if mode.config_entry_id != power.config_entry_id or mode.unique_id != f"{stem}_running_mode":
        return None
    levels = []
    for domain, suffix in (("fan", "_heater_level"), ("number", "_level")):
        matches = [
            e
            for e in entries
            if e.domain == domain
            and (
                e.unique_id.endswith(suffix)
                if domain == "fan"
                else e.unique_id == f"{stem}{suffix}"
            )
        ]
        if len(matches) > 1:
            return None
        if matches:
            candidate = matches[0]
            if (
                candidate.config_entry_id != power.config_entry_id
                or candidate.unique_id != f"{stem}{suffix}"
            ):
                continue
            levels.append(candidate.entity_id)
    if not levels:
        return None
    guards = tuple(
        e.entity_id
        for e in entries
        if e.domain == "binary_sensor"
        and e.unique_id == f"{stem}_burnoff_active"
        and e.config_entry_id == power.config_entry_id
    )
    return controls, tuple(levels), guards


def select_level_entity(
    hass: HomeAssistant,
    candidates: tuple[str, ...],
    preferred: str | None,
    *,
    allow_available: bool,
) -> str:
    """Keep a usable preference; otherwise choose a usable public alternate."""
    ordered = (
        tuple(dict.fromkeys((preferred, *candidates))) if preferred in candidates else candidates
    )
    if allow_available:
        for entity_id in ordered:
            if level_unavailable_reason(hass, entity_id) is None:
                return entity_id
    return ordered[0]


def source_control_entity_ids(
    hass: HomeAssistant,
    device_id: str,
    *,
    preferred_level: str | None = None,
) -> dict[str, str] | None:
    """Resolve bindings, preferring available controls only in explicit Level mode."""
    contract = source_control_contract(hass, device_id)
    if contract is None:
        return None
    controls, candidates, guards = contract
    mode = hass.states.get(controls[CONF_RUNNING_MODE])
    safe = bool(mode and mode.state in LEVEL_MODE_VALUES) and all(
        (state := hass.states.get(entity_id)) is not None and state.state == "off"
        for entity_id in guards
    )
    controls[CONF_HEATER_LEVEL] = select_level_entity(
        hass,
        candidates,
        preferred_level,
        allow_available=safe,
    )
    return controls


def source_binding_identity(hass: HomeAssistant, entity_ids: tuple[str, ...]) -> tuple:
    """Snapshot registry identities and ownership across a public service await."""
    registry = er.async_get(hass)
    return tuple(
        (entry.id, entry.config_entry_id, entry.unique_id, entry.device_id)
        if (entry := registry.async_get(entity_id)) is not None
        else None
        for entity_id in entity_ids
    )


def source_telemetry_defaults(hass: HomeAssistant, device_id: str) -> dict[str, str]:
    """Resolve the physical telemetry the selected heater actually exposes."""
    registry = er.async_get(hass)
    entries = [
        entry
        for entry in er.async_entries_for_device(registry, device_id)
        if _active_source_entry(hass, entry)
    ]
    defaults: dict[str, str] = {}
    for key, candidates in _TELEMETRY_ENTITY_SUFFIXES.items():
        for domain, suffix in candidates:
            matches = [
                entry.entity_id
                for entry in entries
                if entry.domain == domain
                and entry.unique_id.endswith(suffix)
                and getattr(entry, "platform", "diesel_heater") == "diesel_heater"
                and not getattr(entry, "disabled_by", None)
            ]
            if len(matches) == 1:
                defaults[key] = matches[0]
                break
    return defaults


ENTITY_KEYS = (
    CONF_HEATER_POWER,
    CONF_HEATER_LEVEL,
    CONF_RUNNING_MODE,
    CONF_ROOM_TEMPERATURE,
    *OPTIONAL_ENTITY_KEYS,
)


def entity_registry_ids(
    hass: HomeAssistant, config: dict, *, rebind: Iterable[str] = ()
) -> dict[str, str]:
    """Persist registry identity so selected sensors survive entity-ID renames."""
    registry = er.async_get(hass)
    result = dict(config.get(CONF_ENTITY_REGISTRY_IDS, {}))
    for key in ENTITY_KEYS:
        if key in result and key not in rebind:
            continue
        entry = registry.async_get(config.get(key, ""))
        if entry is not None:
            result[key] = entry.id
        else:
            result.pop(key, None)
    return result


def resolve_entity_references(hass: HomeAssistant, config: dict) -> dict[str, str]:
    """Resolve stable references before setting up listeners or issuing controls."""
    registry = er.async_get(hass)
    by_id = {entry.id: entry.entity_id for entry in registry.entities.values()}
    result = {}
    for key, registry_id in config.get(CONF_ENTITY_REGISTRY_IDS, {}).items():
        if key in ENTITY_KEYS and registry_id in by_id:
            result[key] = by_id[registry_id]
    return result
