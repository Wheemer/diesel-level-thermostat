"""Virtual staged thermostat for a diesel heater operating in Level Mode."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, ClassVar

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_CASE_TEMPERATURE,
    CONF_EXHAUST_TEMPERATURE,
    CONF_FAULT_SENSOR,
    CONF_HEATER_LEVEL,
    CONF_HEATER_POWER,
    CONF_LEVEL_MODE_VALUE,
    CONF_MAX_CASE_TEMPERATURE,
    CONF_MAX_EXHAUST_TEMPERATURE,
    CONF_MIN_VOLTAGE,
    CONF_ROOM_TEMPERATURE,
    CONF_ROOM_TEMPERATURE_OFFSET,
    CONF_RUNNING_MODE,
    CONF_SOURCE_DEVICE_ID,
    CONF_VOLTAGE_SENSOR,
    DEFAULT_MAX_LEVEL,
    DEFAULT_MIN_LEVEL,
    DEFAULT_PROPORTIONAL_BAND,
    DEFAULT_ROOM_TEMPERATURE_OFFSET,
    LEVEL_MODE_VALUES,
    OPTIONAL_ENTITY_KEYS,
)
from .control import target_level
from .source import source_device_id, source_telemetry_entity_ids

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the virtual thermostat."""
    async_add_entities([DieselLevelThermostat(hass, entry)])


class DieselLevelThermostat(ClimateEntity, RestoreEntity):
    """A staged virtual thermostat driving a source heater in Level Mode."""

    _attr_has_entity_name = True
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 5
    _attr_max_temp = 35

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the controller."""
        self.hass = hass
        self.entry = entry
        self.config = {**entry.data, **entry.options}
        self._attr_unique_id = f"{entry.entry_id}_thermostat"
        self._attr_name = entry.title
        self._attr_temperature_unit = hass.config.units.temperature_unit
        if self._attr_temperature_unit == UnitOfTemperature.FAHRENHEIT:
            self._attr_min_temp = 41
            self._attr_max_temp = 95
            self._attr_target_temperature_step = 1
        self._hvac_mode = HVACMode.OFF
        self._target_temperature: float | None = None
        self._control_lock = asyncio.Lock()
        self._source_device_id = self.config.get(CONF_SOURCE_DEVICE_ID)
        self._source_telemetry: tuple[str, ...] = ()

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the active HVAC mode."""
        return self._hvac_mode

    @property
    def hvac_action(self) -> HVACAction:
        """Return the current heating action."""
        if self._hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        if self._is_source_on() and self.available:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def target_temperature(self) -> float | None:
        """Return the selected room target."""
        return self._target_temperature

    @property
    def current_temperature(self) -> float | None:
        """Return the selected room temperature with its local correction."""
        temperature = self._temperature_from_entity(self.config[CONF_ROOM_TEMPERATURE])
        if temperature is None:
            return None
        offset_c = self.config.get(
            CONF_ROOM_TEMPERATURE_OFFSET, DEFAULT_ROOM_TEMPERATURE_OFFSET
        )
        return temperature + (
            TemperatureConverter.convert(
                offset_c,
                UnitOfTemperature.CELSIUS,
                self._attr_temperature_unit,
            )
            - TemperatureConverter.convert(
                0,
                UnitOfTemperature.CELSIUS,
                self._attr_temperature_unit,
            )
        )

    @property
    def available(self) -> bool:
        """Only expose control while the source heater is in Level Mode."""
        power = self.hass.states.get(self.config[CONF_HEATER_POWER])
        mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
        level = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        return bool(
            power
            and mode
            and level
            and self.current_temperature is not None
            and power.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            and self._is_level_mode(mode.state)
            and level.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the source entities that drive this controller."""
        attributes: dict[str, Any] = {
            "source_power": self.config[CONF_HEATER_POWER],
            "source_level": self.config[CONF_HEATER_LEVEL],
            "source_mode": self.config[CONF_RUNNING_MODE],
            "control_active": self.available and self._hvac_mode == HVACMode.HEAT,
            "room_temperature_offset_c": self.config.get(
                CONF_ROOM_TEMPERATURE_OFFSET, DEFAULT_ROOM_TEMPERATURE_OFFSET
            ),
        }
        telemetry = {
            entity_id: value
            for entity_id in self._source_telemetry
            if (value := self._telemetry_value(entity_id)) is not None
        }
        if telemetry:
            attributes["source_telemetry"] = telemetry
        return attributes

    async def async_added_to_hass(self) -> None:
        """Restore user state and subscribe to selected source telemetry."""
        if old_state := await self.async_get_last_state():
            if old_state.state in (HVACMode.HEAT, HVACMode.OFF):
                self._hvac_mode = HVACMode(old_state.state)
            if (temperature := old_state.attributes.get(ATTR_TEMPERATURE)) is not None:
                self._target_temperature = float(temperature)
        if self._target_temperature is None:
            self._target_temperature = (
                68.0
                if self._attr_temperature_unit == UnitOfTemperature.FAHRENHEIT
                else 20.0
            )

        if self._source_device_id is None:
            self._source_device_id = source_device_id(
                self.hass,
                (
                    self.config[CONF_HEATER_POWER],
                    self.config[CONF_HEATER_LEVEL],
                    self.config[CONF_RUNNING_MODE],
                ),
            )
        self._source_telemetry = source_telemetry_entity_ids(
            self.hass, self._source_device_id
        )

        watched = [
            self.config[CONF_ROOM_TEMPERATURE],
            self.config[CONF_HEATER_POWER],
            self.config[CONF_HEATER_LEVEL],
            self.config[CONF_RUNNING_MODE],
        ]
        watched.extend(
            self.config[key] for key in OPTIONAL_ENTITY_KEYS if self.config.get(key)
        )
        watched.extend(self._source_telemetry)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, watched, self._async_source_changed
            )
        )
        await self._async_control()

    @callback
    def _async_source_changed(self, _event: Event) -> None:
        """Re-evaluate directly when the selected source state changes."""
        self.async_write_ha_state()
        self.hass.async_create_task(self._async_control())

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the virtual target without touching native temperature mode."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            self._target_temperature = float(temperature)
            self.async_write_ha_state()
            await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Change virtual heating demand."""
        self._hvac_mode = hvac_mode
        if hvac_mode == HVACMode.OFF:
            async with self._control_lock:
                await self._turn_source_off("virtual thermostat turned off")
        else:
            await self._async_control()
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Enable virtual heating demand."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Disable virtual heating demand."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def _async_control(self) -> None:
        """Apply staged output only while this controller owns Level Mode."""
        async with self._control_lock:
            if self._hvac_mode != HVACMode.HEAT:
                return
            # The source marks the level fan unavailable during burn-off.
            # Preserve that hard gate so this thermostat never interrupts its
            # controlled cooldown sequence.
            if self._safety_blocked():
                await self._turn_source_off("configured safety input is active")
                return
            if not self.available:
                return
            temperature = self.current_temperature
            if temperature is None or self._target_temperature is None:
                return

            # Tuning values are stored in Celsius, regardless of Home
            # Assistant's display unit. Compare all control inputs in that
            # same unit so a Fahrenheit installation does not treat a 5°F
            # error as a 5°C proportional band.
            control_temperature = self._to_celsius(temperature)
            control_target = self._to_celsius(self._target_temperature)

            source_on = self._is_source_on()
            _LOGGER.debug(
                "Level control: room=%s%s (%sC) target=%s%s (%sC) source_on=%s",
                temperature,
                self._attr_temperature_unit,
                control_temperature,
                self._target_temperature,
                self._attr_temperature_unit,
                control_target,
                source_on,
            )
            # The controller has one job: heat below target at the level
            # implied by the current gap, then request normal shutdown at target.
            if control_temperature >= control_target:
                if source_on:
                    await self._turn_source_off("target temperature reached")
                return

            demand_level = target_level(
                current_temperature=control_temperature,
                target_temperature=control_target,
                min_level=DEFAULT_MIN_LEVEL,
                max_level=DEFAULT_MAX_LEVEL,
                proportional_band=DEFAULT_PROPORTIONAL_BAND,
            )
            _LOGGER.debug("Level control demand: level=%s", demand_level)
            await self._set_source_level(demand_level)
            if not source_on:
                await self._call_source(
                    "switch", "turn_on", self.config[CONF_HEATER_POWER]
                )
            self.async_write_ha_state()

    def _to_celsius(self, temperature: float) -> float:
        """Convert a Home Assistant display temperature to control Celsius."""
        return TemperatureConverter.convert(
            temperature,
            self._attr_temperature_unit,
            UnitOfTemperature.CELSIUS,
        )

    def _is_source_on(self) -> bool:
        """Return current source power state."""
        state = self.hass.states.get(self.config[CONF_HEATER_POWER])
        return bool(state and state.state == STATE_ON)

    def _is_level_mode(self, value: str) -> bool:
        """Accept the source integration's current and legacy Level labels."""
        return value in {
            self.config.get(CONF_LEVEL_MODE_VALUE),
            *LEVEL_MODE_VALUES,
        }

    def _source_level(self) -> int | None:
        """Return the current source level from its ten-step fan contract."""
        source = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        try:
            percentage = float(source.attributes["percentage"]) if source else 0
        except (KeyError, TypeError, ValueError):
            return None
        if percentage <= 0:
            return None
        return max(1, min(10, math.ceil(percentage / 10)))

    async def _set_source_level(self, level: int) -> None:
        """Set a discrete source level via the fan percentage contract."""
        current_level = self._source_level()
        if current_level == level:
            return
        await self._call_source(
            "fan",
            "set_percentage",
            self.config[CONF_HEATER_LEVEL],
            percentage=level * 10,
        )

    def _can_request_source_shutdown(self) -> bool:
        """Keep burn-off/cooldown ownership with the source integration."""
        level = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        return bool(level and level.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN))

    async def _turn_source_off(self, reason: str) -> None:
        """Request normal heater shutdown without interrupting burn-off."""
        if not self._is_source_on() or not self._can_request_source_shutdown():
            return
        _LOGGER.info("Requesting source heater shutdown: %s", reason)
        await self._call_source("switch", "turn_off", self.config[CONF_HEATER_POWER])

    async def _call_source(
        self, domain: str, service: str, entity_id: str, **data: Any
    ) -> None:
        """Call a source entity through Home Assistant's public service API."""
        await self.hass.services.async_call(
            domain, service, {"entity_id": entity_id, **data}, blocking=True
        )

    def _safety_blocked(self) -> bool:
        """Return true for a selected fault or configured telemetry limit."""
        fault_sensor = self.config.get(CONF_FAULT_SENSOR)
        if (
            fault_sensor
            and (state := self.hass.states.get(fault_sensor))
            and state.state == STATE_ON
        ):
            return True
        return any(
            (
                self._above_limit(CONF_CASE_TEMPERATURE, CONF_MAX_CASE_TEMPERATURE),
                self._above_limit(
                    CONF_EXHAUST_TEMPERATURE, CONF_MAX_EXHAUST_TEMPERATURE
                ),
                self._below_limit(CONF_VOLTAGE_SENSOR, CONF_MIN_VOLTAGE),
            )
        )

    def _above_limit(self, entity_key: str, limit_key: str) -> bool:
        """Return true when an enabled temperature safety limit is exceeded."""
        limit = float(self.config.get(limit_key, 0))
        value = self._temperature_from_entity(self.config.get(entity_key))
        return limit > 0 and value is not None and self._to_celsius(value) >= limit

    def _below_limit(self, entity_key: str, limit_key: str) -> bool:
        """Return true when an enabled voltage limit is breached."""
        limit = float(self.config.get(limit_key, 0))
        entity_id = self.config.get(entity_key)
        if not limit or not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        try:
            return state is not None and float(state.state) < limit
        except (TypeError, ValueError):
            return False

    def _temperature_from_entity(self, entity_id: str | None) -> float | None:
        """Read a temperature entity in Home Assistant's configured unit."""
        if not entity_id or not (state := self.hass.states.get(entity_id)):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        source_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if source_unit in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT):
            return TemperatureConverter.convert(
                value, source_unit, self.hass.config.units.temperature_unit
            )
        return value

    def _telemetry_value(self, entity_id: str) -> dict[str, Any] | None:
        """Return a compact, source-of-truth telemetry snapshot for the climate."""
        if not (state := self.hass.states.get(entity_id)):
            return None
        value: dict[str, Any] = {"state": state.state}
        if unit := state.attributes.get(ATTR_UNIT_OF_MEASUREMENT):
            value["unit"] = unit
        if device_class := state.attributes.get("device_class"):
            value["device_class"] = device_class
        return value
