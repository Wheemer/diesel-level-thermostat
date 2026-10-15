"""Virtual staged thermostat for a diesel heater operating in Level Mode."""

from __future__ import annotations

import logging
import time
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
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    CONF_CASE_TEMPERATURE,
    CONF_COLD_TOLERANCE,
    CONF_EXHAUST_TEMPERATURE,
    CONF_FAULT_SENSOR,
    CONF_HEATER_LEVEL,
    CONF_HEATER_POWER,
    CONF_HOT_TOLERANCE,
    CONF_LEVEL_MODE_VALUE,
    CONF_MAX_CASE_TEMPERATURE,
    CONF_MAX_EXHAUST_TEMPERATURE,
    CONF_MAX_LEVEL,
    CONF_MIN_LEVEL,
    CONF_MIN_OFF_SECONDS,
    CONF_MIN_ON_SECONDS,
    CONF_MIN_VOLTAGE,
    CONF_PROPORTIONAL_BAND,
    CONF_ROOM_TEMPERATURE,
    CONF_RUNNING_MODE,
    CONF_SOURCE_DEVICE_ID,
    CONF_VOLTAGE_SENSOR,
    DEFAULT_COLD_TOLERANCE,
    DEFAULT_HOT_TOLERANCE,
    DEFAULT_MAX_LEVEL,
    DEFAULT_MIN_LEVEL,
    DEFAULT_MIN_OFF_SECONDS,
    DEFAULT_MIN_ON_SECONDS,
    DEFAULT_PROPORTIONAL_BAND,
    OPTIONAL_ENTITY_KEYS,
)
from .control import should_start, should_stop, target_level
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
        self._last_power_change: float | None = None
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
        """Return the selected room temperature."""
        return self._temperature_from_entity(self.config[CONF_ROOM_TEMPERATURE])

    @property
    def available(self) -> bool:
        """Only expose control while the source heater is in Level Mode."""
        mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
        level = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        return bool(
            mode
            and level
            and self.current_temperature is not None
            and mode.state == self.config[CONF_LEVEL_MODE_VALUE]
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
    def _async_source_changed(self, event: Event) -> None:
        """Re-evaluate when source telemetry changes."""
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
        if self._hvac_mode != HVACMode.HEAT or not self.available:
            return
        temperature = self.current_temperature
        if temperature is None or self._target_temperature is None:
            return
        if self._safety_blocked():
            await self._turn_source_off("configured safety input is active")
            return

        if should_stop(
            current_temperature=temperature,
            target_temperature=self._target_temperature,
            hot_tolerance=self.config.get(CONF_HOT_TOLERANCE, DEFAULT_HOT_TOLERANCE),
        ):
            if self._can_turn_off():
                await self._turn_source_off("target temperature reached")
            return
        if not should_start(
            current_temperature=temperature,
            target_temperature=self._target_temperature,
            cold_tolerance=self.config.get(CONF_COLD_TOLERANCE, DEFAULT_COLD_TOLERANCE),
        ):
            return
        if not self._is_source_on() and not self._can_turn_on():
            return

        level = target_level(
            current_temperature=temperature,
            target_temperature=self._target_temperature,
            min_level=self.config.get(CONF_MIN_LEVEL, DEFAULT_MIN_LEVEL),
            max_level=self.config.get(CONF_MAX_LEVEL, DEFAULT_MAX_LEVEL),
            proportional_band=self.config.get(
                CONF_PROPORTIONAL_BAND, DEFAULT_PROPORTIONAL_BAND
            ),
        )
        await self._set_source_level(level)
        if not self._is_source_on():
            await self._call_source("switch", "turn_on", self.config[CONF_HEATER_POWER])
            self._last_power_change = time.monotonic()
        self.async_write_ha_state()

    def _is_source_on(self) -> bool:
        """Return current source power state."""
        state = self.hass.states.get(self.config[CONF_HEATER_POWER])
        return bool(state and state.state == STATE_ON)

    def _can_turn_on(self) -> bool:
        """Respect the configured minimum off time."""
        return self._power_transition_elapsed(
            CONF_MIN_OFF_SECONDS, DEFAULT_MIN_OFF_SECONDS
        )

    def _can_turn_off(self) -> bool:
        """Respect the configured minimum on time."""
        return self._power_transition_elapsed(
            CONF_MIN_ON_SECONDS, DEFAULT_MIN_ON_SECONDS
        )

    def _power_transition_elapsed(self, key: str, default: int) -> bool:
        """Return whether the last requested power transition is old enough."""
        if self._last_power_change is None:
            return True
        return time.monotonic() - self._last_power_change >= self.config.get(
            key, default
        )

    async def _set_source_level(self, level: int) -> None:
        """Set a discrete source level via the fan percentage contract."""
        source = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        percentage = level * 10
        if source and source.attributes.get("percentage") == percentage:
            return
        await self._call_source(
            "fan",
            "set_percentage",
            self.config[CONF_HEATER_LEVEL],
            percentage=percentage,
        )

    async def _turn_source_off(self, reason: str) -> None:
        """Request normal heater shutdown through its public power entity."""
        if not self._is_source_on() or not self.available:
            return
        _LOGGER.info("Requesting source heater shutdown: %s", reason)
        await self._call_source("switch", "turn_off", self.config[CONF_HEATER_POWER])
        self._last_power_change = time.monotonic()

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
        return limit > 0 and value is not None and value >= limit

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
