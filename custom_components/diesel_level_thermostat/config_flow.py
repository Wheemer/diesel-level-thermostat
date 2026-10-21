"""Config flow for Diesel Level Thermostat."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import (
    CONF_COLD_TOLERANCE,
    CONF_HOT_TOLERANCE,
    CONF_LEVEL_MODE_VALUE,
    CONF_MAX_CASE_TEMPERATURE,
    CONF_MAX_EXHAUST_TEMPERATURE,
    CONF_MAX_LEVEL,
    CONF_MIN_LEVEL,
    CONF_MIN_LEVEL_CHANGE_SECONDS,
    CONF_MIN_OFF_SECONDS,
    CONF_MIN_ON_SECONDS,
    CONF_MIN_VOLTAGE,
    CONF_PROPORTIONAL_BAND,
    CONF_ROOM_TEMPERATURE,
    CONF_ROOM_TEMPERATURE_OFFSET,
    CONF_SOURCE_DEVICE_ID,
    DEFAULT_COLD_TOLERANCE,
    DEFAULT_HOT_TOLERANCE,
    DEFAULT_LEVEL_MODE_VALUE,
    DEFAULT_MAX_LEVEL,
    DEFAULT_MIN_LEVEL,
    DEFAULT_MIN_LEVEL_CHANGE_SECONDS,
    DEFAULT_MIN_OFF_SECONDS,
    DEFAULT_MIN_ON_SECONDS,
    DEFAULT_PROPORTIONAL_BAND,
    DEFAULT_ROOM_TEMPERATURE_OFFSET,
    DISABLED_THRESHOLD,
    DOMAIN,
)
from .source import source_control_entity_ids, source_telemetry_defaults


def _entity_selector(
    domain: str, device_class: str | None = None
) -> selector.EntitySelector:
    """Build a single-entity selector."""
    filter_data: dict[str, Any] = {"domain": domain}
    if device_class:
        filter_data["device_class"] = device_class
    return selector.EntitySelector(selector.EntitySelectorConfig(filter=filter_data))


def _number_selector(
    minimum: float, maximum: float, step: float = 1
) -> selector.NumberSelector:
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


def options_schema(room_temperature: str | None) -> vol.Schema:
    """Build the primary room-feedback options schema."""
    return vol.Schema(
        {
            vol.Required(
                CONF_ROOM_TEMPERATURE, default=room_temperature
            ): _entity_selector("sensor", "temperature"),
            vol.Required(
                CONF_ROOM_TEMPERATURE_OFFSET,
                default=DEFAULT_ROOM_TEMPERATURE_OFFSET,
            ): _number_selector(-20, 20, 0.1),
        }
    )


def advanced_options_schema() -> vol.Schema:
    """Build advanced controller tuning and safety options."""
    return vol.Schema(
        {
            vol.Required(CONF_MIN_LEVEL, default=DEFAULT_MIN_LEVEL): _number_selector(
                1, 10
            ),
            vol.Required(CONF_MAX_LEVEL, default=DEFAULT_MAX_LEVEL): _number_selector(
                1, 10
            ),
            vol.Required(
                CONF_PROPORTIONAL_BAND, default=DEFAULT_PROPORTIONAL_BAND
            ): _number_selector(0.1, 30, 0.1),
            vol.Required(
                CONF_COLD_TOLERANCE, default=DEFAULT_COLD_TOLERANCE
            ): _number_selector(0, 10, 0.1),
            vol.Required(
                CONF_HOT_TOLERANCE, default=DEFAULT_HOT_TOLERANCE
            ): _number_selector(0, 10, 0.1),
            vol.Required(
                CONF_MIN_ON_SECONDS, default=DEFAULT_MIN_ON_SECONDS
            ): _number_selector(0, 3600),
            vol.Required(
                CONF_MIN_OFF_SECONDS, default=DEFAULT_MIN_OFF_SECONDS
            ): _number_selector(0, 3600),
            vol.Required(
                CONF_MIN_LEVEL_CHANGE_SECONDS,
                default=DEFAULT_MIN_LEVEL_CHANGE_SECONDS,
            ): _number_selector(0, 3600),
            vol.Required(
                CONF_MIN_VOLTAGE, default=DISABLED_THRESHOLD
            ): _number_selector(0, 100, 0.1),
            vol.Required(
                CONF_MAX_CASE_TEMPERATURE, default=DISABLED_THRESHOLD
            ): _number_selector(0, 500, 0.1),
            vol.Required(
                CONF_MAX_EXHAUST_TEMPERATURE, default=DISABLED_THRESHOLD
            ): _number_selector(0, 1000, 0.1),
        }
    )


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
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data={**self._source_data, **user_input},
                options={},
            )
        return self.async_show_form(
            step_id="telemetry", data_schema=self._telemetry_schema()
        )

    def _telemetry_schema(self) -> vol.Schema:
        """Build the editable control-temperature step with a heater default."""
        room_temperature = self._source_data.get(CONF_ROOM_TEMPERATURE)
        room_selector = _entity_selector("sensor", "temperature")
        return vol.Schema(
            {
                vol.Required(CONF_NAME, default="Diesel Level Thermostat"): str,
                vol.Required(
                    CONF_ROOM_TEMPERATURE, default=room_temperature
                ): room_selector,
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
    """Handle controller tuning options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure the room-feedback source first."""
        if user_input is not None:
            self._room_feedback = user_input
            return await self.async_step_advanced()
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                self._room_feedback_schema(), self.config_entry.options
            ),
        )

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Configure controller tuning and optional safety limits."""
        if user_input is not None:
            data = {
                **self.config_entry.options,
                **getattr(self, "_room_feedback", {}),
                **user_input,
            }
            if data[CONF_MIN_LEVEL] > data[CONF_MAX_LEVEL]:
                return self.async_show_form(
                    step_id="advanced",
                    data_schema=self.add_suggested_values_to_schema(
                        self._advanced_options_schema(), user_input
                    ),
                    errors={"base": "invalid_level_range"},
                )
            return self.async_create_entry(title="", data=data)
        return self.async_show_form(
            step_id="advanced",
            data_schema=self.add_suggested_values_to_schema(
                self._advanced_options_schema(), self.config_entry.options
            ),
        )

    def _room_feedback_schema(self) -> vol.Schema:
        """Use the configured room sensor as the editable default."""
        return options_schema(
            self.config_entry.options.get(
                CONF_ROOM_TEMPERATURE,
                self.config_entry.data.get(CONF_ROOM_TEMPERATURE),
            )
        )

    @staticmethod
    def _advanced_options_schema() -> vol.Schema:
        """Return the advanced tuning and safety options schema."""
        return advanced_options_schema()
