"""Config flow for Diesel Level Thermostat."""

from __future__ import annotations

import math
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import (
    CONF_ENTITY_REGISTRY_IDS,
    CONF_LEVEL_MODE_VALUE,
    CONF_ROOM_TEMPERATURE,
    CONF_ROOM_TEMPERATURE_OFFSET,
    CONF_SOURCE_DEVICE_ID,
    DEFAULT_LEVEL_MODE_VALUE,
    DEFAULT_ROOM_TEMPERATURE_OFFSET,
    DOMAIN,
)
from .source import entity_registry_ids, source_control_entity_ids, source_telemetry_defaults


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
    room_temperature: str | None, offset: float = DEFAULT_ROOM_TEMPERATURE_OFFSET
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
        }
    )


def feedback_errors(values: dict[str, Any]) -> dict[str, str]:
    """Reject invalid feedback settings before persisting a flow result."""
    errors = {}
    try:
        offset = float(values.get(CONF_ROOM_TEMPERATURE_OFFSET, 0))
        if not math.isfinite(offset) or not -20 <= offset <= 20:
            errors[CONF_ROOM_TEMPERATURE_OFFSET] = "invalid_offset"
    except (TypeError, ValueError):
        errors[CONF_ROOM_TEMPERATURE_OFFSET] = "invalid_offset"
    room = values.get(CONF_ROOM_TEMPERATURE)
    if not isinstance(room, str) or not room.startswith("sensor."):
        errors[CONF_ROOM_TEMPERATURE] = "invalid_sensor"
    return errors


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
            if errors := feedback_errors(user_input):
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
        """Update the two inputs used by the staged control loop."""
        if user_input is not None:
            if errors := feedback_errors(user_input):
                return self.async_show_form(
                    step_id="init",
                    data_schema=options_schema(
                        self.config_entry.options.get(
                            CONF_ROOM_TEMPERATURE, self.config_entry.data.get(CONF_ROOM_TEMPERATURE)
                        )
                    ),
                    errors=errors,
                )
            config = {**self.config_entry.data, **self.config_entry.options, **user_input}
            options = {**self.config_entry.options, **user_input}
            options[CONF_ENTITY_REGISTRY_IDS] = entity_registry_ids(self.hass, config)
            return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                options_schema(
                    self.config_entry.options.get(
                        CONF_ROOM_TEMPERATURE,
                        self.config_entry.data.get(CONF_ROOM_TEMPERATURE),
                    ),
                    self.config_entry.options.get(
                        CONF_ROOM_TEMPERATURE_OFFSET,
                        self.config_entry.data.get(
                            CONF_ROOM_TEMPERATURE_OFFSET, DEFAULT_ROOM_TEMPERATURE_OFFSET
                        ),
                    ),
                ),
                self.config_entry.options,
            ),
        )
