"""Diesel Level Thermostat integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONF_HEATER_LEVEL,
    CONF_HEATER_POWER,
    CONF_RUNNING_MODE,
    CONF_SOURCE_BURNOFF_GUARD_REQUIRED,
    CONF_SOURCE_DEVICE_ID,
)
from .source import (
    ENTITY_KEYS,
    entity_registry_ids,
    resolve_entity_references,
    source_control_contract,
    source_control_entity_ids,
    source_device_id,
)

PLATFORMS: list[Platform] = [Platform.CLIMATE]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Resolve source bindings, then register options/registry reload listeners."""
    config = {**entry.data, **entry.options}
    references = resolve_entity_references(hass, config)
    registry_ids = config.get(CONF_ENTITY_REGISTRY_IDS, {})
    device_id = config.get(CONF_SOURCE_DEVICE_ID) or source_device_id(
        hass,
        (
            references.get(key, "" if key in registry_ids else config.get(key, ""))
            for key in (CONF_HEATER_POWER, CONF_HEATER_LEVEL, CONF_RUNNING_MODE)
        ),
    )
    if not device_id:
        raise ConfigEntryNotReady("Selected heater source identity cannot be resolved")
    controls = source_control_entity_ids(
        hass,
        device_id,
        preferred_level=references.get(CONF_HEATER_LEVEL, config.get(CONF_HEATER_LEVEL)),
    )
    if controls is None and all(
        key in references for key in (CONF_HEATER_POWER, CONF_HEATER_LEVEL, CONF_RUNNING_MODE)
    ):
        recovered_device = source_device_id(
            hass,
            (references[key] for key in (CONF_HEATER_POWER, CONF_HEATER_LEVEL, CONF_RUNNING_MODE)),
        )
        if recovered_device and recovered_device != device_id:
            recovered = source_control_entity_ids(
                hass, recovered_device, preferred_level=references[CONF_HEATER_LEVEL]
            )
            if recovered and all(
                recovered[key] == references[key] for key in (CONF_HEATER_POWER, CONF_RUNNING_MODE)
            ):
                device_id, controls = recovered_device, recovered
    if controls is None:
        raise ConfigEntryNotReady(
            "Selected heater has missing, disabled, or ambiguous "
            "Power, Level, or Running Mode controls"
        )
    references.update(controls)
    # Controls are rediscovered by validated source ownership. Feedback inputs
    # must retain their saved registry identity until explicitly reselected.
    control_keys = {CONF_HEATER_POWER, CONF_HEATER_LEVEL, CONF_RUNNING_MODE}
    missing = [
        key
        for key in registry_ids
        if key in ENTITY_KEYS and key not in control_keys and key not in references
    ]
    if missing:
        raise ConfigEntryNotReady(
            "Selected feedback entity identity cannot be resolved: " + ", ".join(sorted(missing))
        )
    data, options = dict(entry.data), dict(entry.options)
    contract = source_control_contract(hass, device_id)
    if contract and contract[2]:
        data[CONF_SOURCE_BURNOFF_GUARD_REQUIRED] = True
        options.pop(CONF_SOURCE_BURNOFF_GUARD_REQUIRED, None)
    if device_id:
        data[CONF_SOURCE_DEVICE_ID] = device_id
    for key, value in references.items():
        (options if key in options else data)[key] = value
    merged = {**data, **options}
    bindings = entity_registry_ids(hass, merged, rebind=control_keys)
    if CONF_ENTITY_REGISTRY_IDS in options:
        options[CONF_ENTITY_REGISTRY_IDS] = bindings
    else:
        data[CONF_ENTITY_REGISTRY_IDS] = bindings
    if data != entry.data or options != entry.options:
        hass.config_entries.async_update_entry(entry, data=data, options=options)

    async def update_options(hass: HomeAssistant, updated: ConfigEntry) -> None:
        await hass.config_entries.async_reload(updated.entry_id)

    registry_reload_in_progress = False

    async def registry_changed(event) -> None:
        nonlocal registry_reload_in_progress
        entity_id = event.data.get("entity_id")
        if not entity_id:
            return
        changes = event.data.get("changes") or {}
        if event.data.get("action") == "update" and not any(
            key in changes
            for key in ("entity_id", "disabled_by", "device_id", "config_entry_id", "unique_id")
        ):
            return
        registry = er.async_get(hass)
        row = registry.async_get(entity_id)
        selected = {**entry.data, **entry.options}
        selected_ids = {selected[key] for key in ENTITY_KEYS if selected.get(key)}
        relevant = entity_id in selected_ids or changes.get("entity_id") in selected_ids
        power = registry.async_get(selected.get(CONF_HEATER_POWER, ""))
        if not relevant and row is not None and power is not None:
            stem = power.unique_id.removesuffix("_power")
            relevant = (
                row.device_id == selected.get(CONF_SOURCE_DEVICE_ID)
                and row.config_entry_id == power.config_entry_id
                and row.platform == "diesel_heater"
                and row.unique_id
                in {stem + "_heater_level", stem + "_level", stem + "_burnoff_active"}
            )
        if relevant and not registry_reload_in_progress:
            registry_reload_in_progress = True
            try:
                await hass.config_entries.async_reload(entry.entry_id)
            finally:
                registry_reload_in_progress = False

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(update_options))
    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the climate; entity removal drains pending control work."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
