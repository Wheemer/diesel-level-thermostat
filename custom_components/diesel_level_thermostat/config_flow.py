"""Config flow for Diesel Level Thermostat."""

from __future__ import annotations

import math
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONF_FAULT_SENSOR,
    CONF_LEVEL_MODE_VALUE,
    CONF_ROOM_TEMPERATURE,
    CONF_ROOM_TEMPERATURE_OFFSET,
    CONF_SOURCE_DEVICE_ID,
    CONF_TEMPERATURE_HYSTERESIS,
    DEFAULT_LEVEL_MODE_VALUE,
    DEFAULT_ROOM_TEMPERATURE_OFFSET,
    DEFAULT_TEMPERATURE_HYSTERESIS,
    DOMAIN,
    OPTIONAL_ENTITY_KEYS,
)
from .source import (
    entity_registry_ids,
    resolve_entity_references,
    source_control_contract,
    source_control_entity_ids,
    source_telemetry_defaults,
)


def _entity_selector(domain: str, device_class: str | None = None) -> selector.EntitySelector:
    """Build a single-entity selector."""
    filter_data: dict[str, Any] = {"domain": domain}
    if device_class:
        filter_data["device_class"] = device_class
    return selector.EntitySelector(selector.EntitySelectorConfig(filter=filter_data))


def _number_selector(minimum: float, maximum: float, step: float = 1) -> selector.NumberSelector:
    """Build a boxed numeric selector with an explicit step."""
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SOURCE_DEVICE_ID): selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration="diesel_heater")
        ),
    }
)


def options_schema(
    room_temperature: str | None,
    offset: float = DEFAULT_ROOM_TEMPERATURE_OFFSET,
    repair_entities: dict[str, str | None] | None = None,
    hysteresis: float = DEFAULT_TEMPERATURE_HYSTERESIS,
) -> vol.Schema:
    """Build the primary room-feedback options schema."""
    return vol.Schema(
        {
            vol.Required(CONF_ROOM_TEMPERATURE, default=room_temperature): _entity_selector(
                "sensor", "temperature"
            ),
            vol.Required(
                CONF_ROOM_TEMPERATURE_OFFSET,
                default=offset,
            ): _number_selector(-20, 20, 0.1),
            vol.Required(CONF_TEMPERATURE_HYSTERESIS, default=hysteresis): _number_selector(
                0, 5, 0.1
            ),
            **{
                vol.Optional(key): _entity_selector(
                    "binary_sensor" if key == CONF_FAULT_SENSOR else "sensor",
                    "problem"
                    if key == CONF_FAULT_SENSOR
                    else ("voltage" if key == "voltage_sensor" else "temperature"),
                )
                for key in (repair_entities or {})
            },
        }
    )


def feedback_errors(values: dict[str, Any], hass) -> dict[str, str]:
    """Reject invalid feedback settings before persisting a flow result."""
    errors = {}
    try:
        offset = float(values.get(CONF_ROOM_TEMPERATURE_OFFSET, 0))
        if not math.isfinite(offset) or not -20 <= offset <= 20:
            errors[CONF_ROOM_TEMPERATURE_OFFSET] = "invalid_offset"
    except (TypeError, ValueError):
        errors[CONF_ROOM_TEMPERATURE_OFFSET] = "invalid_offset"
    try:
        hysteresis = float(values.get(CONF_TEMPERATURE_HYSTERESIS, 0))
        if not math.isfinite(hysteresis) or not 0 <= hysteresis <= 5:
            errors[CONF_TEMPERATURE_HYSTERESIS] = "invalid_hysteresis"
    except (TypeError, ValueError):
        errors[CONF_TEMPERATURE_HYSTERESIS] = "invalid_hysteresis"
    room = values.get(CONF_ROOM_TEMPERATURE)
    if not isinstance(room, str) or not room.startswith("sensor."):
        errors[CONF_ROOM_TEMPERATURE] = "invalid_sensor"
    elif (state := hass.states.get(room)) is None:
        errors[CONF_ROOM_TEMPERATURE] = "invalid_room_temperature"
    else:
        try:
            value = float(state.state)
            valid = math.isfinite(value)
        except (TypeError, ValueError):
            valid = False
        if (
            not valid
            or state.attributes.get("unit_of_measurement") not in TemperatureConverter.VALID_UNITS
            or state.attributes.get("device_class") not in (None, "temperature")
        ):
            errors[CONF_ROOM_TEMPERATURE] = "invalid_room_temperature"
    return errors


def valid_safety_repair(hass, config: dict, key: str, entity_id: str) -> bool:
    """Accept only the original physical quantity from this source heater."""
    contract = source_control_contract(hass, config.get(CONF_SOURCE_DEVICE_ID, ""))
    if contract is None or not isinstance(entity_id, str):
        return False
    registry = er.async_get(hass)
    power = registry.async_get(contract[0]["heater_power"])
    candidate = registry.async_get(entity_id)
    suffixes = {
        "fault_sensor": "problem",
        "voltage_sensor": "supply_voltage",
        "case_temperature": "case_temp",
        "exhaust_temperature": "outlet_temperature",
    }
    if (
        not power
        or not candidate
        or candidate.platform != "diesel_heater"
        or candidate.config_entry_id != power.config_entry_id
        or candidate.device_id != power.device_id
        or candidate.disabled_by
        or getattr(candidate, "orphaned_timestamp", None) is not None
        or candidate.unique_id != power.unique_id.removesuffix("_power") + "_" + suffixes[key]
    ):
        return False
    state = hass.states.get(entity_id)
    if state is None:
        return False
    if key == CONF_FAULT_SENSOR:
        return (
            candidate.domain == "binary_sensor"
            and state.attributes.get("device_class") == "problem"
        )
    if candidate.domain != "sensor":
        return False
    if key == "voltage_sensor":
        return (
            state.attributes.get("device_class") == "voltage"
            and state.attributes.get("unit_of_measurement") == "V"
        )
    return state.attributes.get("unit_of_measurement") in TemperatureConverter.VALID_UNITS


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Diesel Level Thermostat."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select the source Diesel Heater device."""
        if user_input is not None:
            source_device = user_input[CONF_SOURCE_DEVICE_ID]
            controls = source_control_entity_ids(self.hass, source_device)
            if controls is None:
                return self.async_show_form(
                    step_id="user",
                    data_schema=USER_SCHEMA,
                    errors={"base": "unsupported_source_device"},
                )
            await self.async_set_unique_id(source_device)
            self._abort_if_unique_id_configured()
            self._source_data = {
                CONF_SOURCE_DEVICE_ID: source_device,
                CONF_LEVEL_MODE_VALUE: DEFAULT_LEVEL_MODE_VALUE,
                **controls,
                **source_telemetry_defaults(self.hass, source_device),
            }
            return await self.async_step_telemetry()
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA)

    async def async_step_telemetry(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Select the room and optional safety telemetry."""
        if user_input is not None:
            if errors := feedback_errors(user_input, self.hass):
                return self.async_show_form(
                    step_id="telemetry", data_schema=self._telemetry_schema(), errors=errors
                )
            config = {**self._source_data, **user_input}
            config[CONF_ENTITY_REGISTRY_IDS] = entity_registry_ids(self.hass, config)
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data=config,
                options={},
            )
        return self.async_show_form(step_id="telemetry", data_schema=self._telemetry_schema())

    def _telemetry_schema(self) -> vol.Schema:
        """Build the editable control-temperature step with a heater default."""
        room_temperature = self._source_data.get(CONF_ROOM_TEMPERATURE)
        room_selector = _entity_selector("sensor", "temperature")
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default="Diesel Level Thermostat"): str,
                vol.Required(CONF_ROOM_TEMPERATURE, default=room_temperature): room_selector,
                vol.Required(
                    CONF_ROOM_TEMPERATURE_OFFSET,
                    default=DEFAULT_ROOM_TEMPERATURE_OFFSET,
                ): _number_selector(-20, 20, 0.1),
                vol.Required(
                    CONF_TEMPERATURE_HYSTERESIS, default=DEFAULT_TEMPERATURE_HYSTERESIS
                ): _number_selector(0, 5, 0.1),
            }
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> DieselLevelThermostatOptionsFlow:
        """Return the options flow."""
        return DieselLevelThermostatOptionsFlow()


class DieselLevelThermostatOptionsFlow(config_entries.OptionsFlow):
    """Edit the selected room-feedback source and its local correction."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Update room feedback and the temperature switching band."""
        saved = {**self.config_entry.data, **self.config_entry.options}
        registry_ids = saved.get(CONF_ENTITY_REGISTRY_IDS, {})
        resolved = resolve_entity_references(self.hass, saved) if registry_ids else {}
        repairs = {
            key: None for key in OPTIONAL_ENTITY_KEYS if key in registry_ids and key not in resolved
        }
        room = (
            resolved.get(CONF_ROOM_TEMPERATURE)
            if CONF_ROOM_TEMPERATURE in registry_ids
            else saved.get(CONF_ROOM_TEMPERATURE)
        )
        schema = options_schema(
            room,
            saved.get(CONF_ROOM_TEMPERATURE_OFFSET, DEFAULT_ROOM_TEMPERATURE_OFFSET),
            repair_entities=repairs,
            hysteresis=saved.get(CONF_TEMPERATURE_HYSTERESIS, DEFAULT_TEMPERATURE_HYSTERESIS),
        )
        if user_input is not None:
            errors = feedback_errors(user_input, self.hass)
            selected_repairs = {key for key in repairs if key in user_input}
            for key in selected_repairs:
                if not valid_safety_repair(self.hass, saved, key, user_input[key]):
                    errors[key] = "invalid_safety_sensor"
            if errors:
                return self.async_show_form(
                    step_id="init",
                    data_schema=self.add_suggested_values_to_schema(schema, user_input),
                    errors=errors,
                )
            config = {**saved, **user_input}
            options = {**self.config_entry.options, **user_input}
            options[CONF_ENTITY_REGISTRY_IDS] = entity_registry_ids(
                self.hass,
                config,
                rebind=(CONF_ROOM_TEMPERATURE, *selected_repairs),
            )
            return self.async_create_entry(title="", data=options)
        suggestions = dict(self.config_entry.options)
        # An old entity name must not preselect a replacement after identity loss.
        if CONF_ROOM_TEMPERATURE in registry_ids:
            suggestions.pop(CONF_ROOM_TEMPERATURE, None)
        for key in repairs:
            suggestions.pop(key, None)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggestions),
        )
