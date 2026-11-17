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
    CONF_SOURCE_DEVICE_ID,
)
from .source import (
    ENTITY_KEYS,
    entity_registry_ids,
    resolve_entity_references,
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

    async def registry_changed(event) -> None:
        entity_id = event.data.get("entity_id")
        registry_entry = er.async_get(hass).async_get(entity_id)
        selected = {**entry.data, **entry.options}
        old_id = event.data.get("changes", {}).get("entity_id")
        if (
            entity_id in {selected.get(key) for key in ENTITY_KEYS}
            or old_id in {selected.get(key) for key in ENTITY_KEYS if selected.get(key)}
            or (
                selected.get(CONF_SOURCE_DEVICE_ID)
                and registry_entry is not None
                and registry_entry.device_id == selected[CONF_SOURCE_DEVICE_ID]
            )
        ):
            await hass.config_entries.async_reload(entry.entry_id)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(update_options))
    entry.async_on_unload(hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the climate; entity removal drains pending control work."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
