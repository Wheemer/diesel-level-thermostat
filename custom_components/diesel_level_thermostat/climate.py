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
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
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
        self._removed = False
        self._control_tasks: set[asyncio.Task] = set()
        self._event_task: asyncio.Task | None = None
        self._control_dirty = False
        self._power_on_pending = False

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
        offset_c = self.config.get(CONF_ROOM_TEMPERATURE_OFFSET, DEFAULT_ROOM_TEMPERATURE_OFFSET)
        try:
            offset_c = float(offset_c)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(offset_c) or not -20 <= offset_c <= 20:
            return None
        corrected = temperature + (
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
        return corrected if math.isfinite(corrected) else None

    def _source_ready(self) -> bool:
        """Require a writable Level-mode source, without assuming ten speeds."""
        power = self.hass.states.get(self.config[CONF_HEATER_POWER])
        mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
        level = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        return bool(
            not self._removed
            and power
            and power.state in (STATE_ON, STATE_OFF)
            and mode
            and self._is_level_mode(mode.state)
            and level
            and level.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            and self._source_level_count() is not None
            and (
                level.attributes.get("percentage") is None
                or self._finite_number(level.attributes["percentage"]) is not None
            )
        )

    @property
    def available(self) -> bool:
        """Expose control only with valid room feedback and a Level-mode source."""
        return self._source_ready() and self.current_temperature is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the source entities that drive this controller."""
        attributes: dict[str, Any] = {
            "control_hvac_mode": self._hvac_mode,
            "control_temperature_unit": self._attr_temperature_unit,
            "control_target_temperature": self._target_temperature,
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
            restored_mode = old_state.attributes.get("control_hvac_mode", old_state.state)
            if restored_mode in (HVACMode.HEAT, HVACMode.OFF):
                self._hvac_mode = HVACMode(restored_mode)
            restored = self._finite_number(
                old_state.attributes.get(
                    "control_target_temperature", old_state.attributes.get(ATTR_TEMPERATURE)
                )
            )
            unit = old_state.attributes.get("control_temperature_unit")
            # Legacy states did not persist the native unit. HA's displayed
            # min/max bounds identify those old C/F ranges without guessing
            # from an arbitrary target value.
            if unit is None:
                bounds = (
                    old_state.attributes.get("min_temp"),
                    old_state.attributes.get("max_temp"),
                )
                unit = {
                    (5, 35): UnitOfTemperature.CELSIUS,
                    (41, 95): UnitOfTemperature.FAHRENHEIT,
                }.get(bounds)
            if unit is not None and unit not in TemperatureConverter.VALID_UNITS:
                restored = None
            if restored is not None and unit in TemperatureConverter.VALID_UNITS:
                restored = TemperatureConverter.convert(restored, unit, self._attr_temperature_unit)
            if restored is not None and self.min_temp <= restored <= self.max_temp:
                self._target_temperature = restored
            elif old_state.attributes.get(ATTR_TEMPERATURE) is not None:
                self._hvac_mode = HVACMode.OFF
                _LOGGER.warning("Invalid restored target; leaving virtual heating off")
        if self._target_temperature is None:
            self._target_temperature = (
                68.0 if self._attr_temperature_unit == UnitOfTemperature.FAHRENHEIT else 20.0
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
        self._source_telemetry = source_telemetry_entity_ids(self.hass, self._source_device_id)

        watched = [
            self.config[CONF_ROOM_TEMPERATURE],
            self.config[CONF_HEATER_POWER],
            self.config[CONF_HEATER_LEVEL],
            self.config[CONF_RUNNING_MODE],
        ]
        watched.extend(self.config[key] for key in OPTIONAL_ENTITY_KEYS if self.config.get(key))
        watched.extend(self._source_telemetry)
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, sorted(set(watched)), self._async_source_changed
            )
        )
        await self._async_control()

    async def async_will_remove_from_hass(self) -> None:
        """Stop owned work before removal; never dispatch a late source command."""
        self._removed = True
        tasks = self._control_tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().async_will_remove_from_hass()

    @callback
    def _async_source_changed(self, _event: Event) -> None:
        """Coalesce state bursts without adding a timer or delaying control."""
        if self._removed:
            return
        if self._is_source_on():
            self._power_on_pending = False
        self.async_write_ha_state()
        self._control_dirty = True
        if self._event_task is None or self._event_task.done():
            self._event_task = self.hass.async_create_task(self._async_event_control())
            self._control_tasks.add(self._event_task)
            self._event_task.add_done_callback(self._control_tasks.discard)

    async def _async_event_control(self) -> None:
        try:
            while self._control_dirty and not self._removed:
                self._control_dirty = False
                await self._async_control()
        except HomeAssistantError:
            _LOGGER.exception("Source heater service failed; waiting for the next state update")

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the virtual target without touching native temperature mode."""
        if self._removed:
            return
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            temperature = self._finite_number(temperature)
            if temperature is None or not self.min_temp <= temperature <= self.max_temp:
                raise HomeAssistantError("Target temperature is outside the supported range")
            self._target_temperature = temperature
            self.async_write_ha_state()
            await self._async_control()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Change virtual heating demand."""
        if hvac_mode not in (HVACMode.HEAT, HVACMode.OFF):
            raise HomeAssistantError("Only Heat and Off are supported")
        if self._removed:
            return
        self._hvac_mode = hvac_mode
        self.async_write_ha_state()
        if hvac_mode == HVACMode.OFF:
            async with self._control_lock:
                await self._turn_source_off("virtual thermostat turned off")
        else:
            await self._async_control()
        if not self._removed:
            self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Enable virtual heating demand."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Disable virtual heating demand."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def _async_control(self) -> None:
        """Apply current demand; recheck it after every awaited level write."""
        task = asyncio.current_task()
        self._control_tasks.add(task)
        try:
            async with self._control_lock:
                if self._removed or self._hvac_mode != HVACMode.HEAT:
                    return
                mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
                if not mode or not self._is_level_mode(mode.state):
                    return
                if self._safety_blocked():
                    await self._turn_source_off("configured safety input is active or unavailable")
                    return
                temperature = self.current_temperature
                if temperature is None:
                    await self._turn_source_off(
                        "room temperature feedback is unavailable or invalid"
                    )
                    return
                if not self._source_ready():
                    await self._turn_source_off("source level feedback is invalid or unavailable")
                    return
                if self._target_temperature is None:
                    return
                control_temperature = self._to_celsius(temperature)
                control_target = self._to_celsius(self._target_temperature)
                if control_temperature >= control_target:
                    await self._turn_source_off("target temperature reached")
                    return
                demand_level = target_level(
                    current_temperature=control_temperature,
                    target_temperature=control_target,
                    min_level=DEFAULT_MIN_LEVEL,
                    max_level=self._source_level_count(),
                    proportional_band=DEFAULT_PROPORTIONAL_BAND,
                )
                # A service call yields to Off, sensor updates, mode changes and
                # removal. Its old demand is never permission to start later.
                snapshot = (temperature, self._target_temperature)
                await self._set_source_level(demand_level)
                if self._removed or self._hvac_mode != HVACMode.HEAT:
                    return
                mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
                if not mode or not self._is_level_mode(mode.state):
                    return
                if self._safety_blocked() or self.current_temperature is None:
                    await self._turn_source_off("feedback changed during control")
                    return
                if not self._source_ready() or snapshot != (
                    self.current_temperature,
                    self._target_temperature,
                ):
                    return
                if self._is_source_on():
                    self._power_on_pending = False
                elif not self._power_on_pending:
                    self._power_on_pending = True
                    try:
                        await self._call_source("switch", "turn_on", self.config[CONF_HEATER_POWER])
                    except HomeAssistantError:
                        self._power_on_pending = False
                        raise
                    if not self._removed and self._hvac_mode == HVACMode.OFF:
                        await self._turn_source_off("virtual thermostat turned off during startup")
                if not self._removed:
                    self.async_write_ha_state()
        finally:
            self._control_tasks.discard(task)

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

    @staticmethod
    def _finite_number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if math.isfinite(number) else None

    def _source_level_count(self) -> int | None:
        """Use the public fan's discrete speed count (legacy fans default to ten)."""
        source = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        if source is None:
            return None
        step = self._finite_number(source.attributes.get("percentage_step", 10))
        if step is None or not 100 / DEFAULT_MAX_LEVEL <= step <= 100:
            return None
        count = round(100 / step)
        return count if 1 <= count <= DEFAULT_MAX_LEVEL else None

    def _source_level(self) -> int | None:
        source = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        count = self._source_level_count()
        percentage = self._finite_number(source.attributes.get("percentage")) if source else None
        if percentage is None or percentage <= 0 or count is None:
            return None
        return max(1, min(count, math.ceil(percentage * count / 100)))

    async def _set_source_level(self, level: int) -> None:
        count = self._source_level_count()
        if count is None or self._source_level() == level:
            return
        await self._call_source(
            "fan",
            "set_percentage",
            self.config[CONF_HEATER_LEVEL],
            percentage=max(1, int(level * 100 / count)),
        )

    def _can_request_source_shutdown(self) -> bool:
        """Keep burn-off/cooldown ownership with the source integration."""
        level = self.hass.states.get(self.config[CONF_HEATER_LEVEL])
        return bool(level and level.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN))

    async def _turn_source_off(self, reason: str) -> None:
        """Request normal heater shutdown without interrupting burn-off."""
        if (
            self._removed
            or not (self._is_source_on() or self._power_on_pending)
            or not self._can_request_source_shutdown()
        ):
            return
        _LOGGER.info("Requesting source heater shutdown: %s", reason)
        await self._call_source("switch", "turn_off", self.config[CONF_HEATER_POWER])
        self._power_on_pending = False

    async def _call_source(self, domain: str, service: str, entity_id: str, **data: Any) -> None:
        """Call a source entity through Home Assistant's public service API."""
        if self._removed:
            return
        await self.hass.services.async_call(
            domain, service, {"entity_id": entity_id, **data}, blocking=True
        )

    def _safety_blocked(self) -> bool:
        """Return true for a selected fault or configured telemetry limit."""
        fault_sensor = self.config.get(CONF_FAULT_SENSOR)
        if fault_sensor and (
            not (state := self.hass.states.get(fault_sensor))
            or state.state in (STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN)
        ):
            return True
        return any(
            (
                self._above_limit(CONF_CASE_TEMPERATURE, CONF_MAX_CASE_TEMPERATURE),
                self._above_limit(CONF_EXHAUST_TEMPERATURE, CONF_MAX_EXHAUST_TEMPERATURE),
                self._below_limit(CONF_VOLTAGE_SENSOR, CONF_MIN_VOLTAGE),
            )
        )

    def _above_limit(self, entity_key: str, limit_key: str) -> bool:
        """Return true when an enabled temperature safety limit is exceeded."""
        limit = self._finite_number(self.config.get(limit_key, 0))
        if limit is None:
            return True
        value = self._temperature_from_entity(self.config.get(entity_key))
        return limit > 0 and (value is None or self._to_celsius(value) >= limit)

    def _below_limit(self, entity_key: str, limit_key: str) -> bool:
        """Return true when an enabled voltage limit is breached."""
        limit = self._finite_number(self.config.get(limit_key, 0))
        if limit is None:
            return True
        entity_id = self.config.get(entity_key)
        if not limit or not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        value = self._finite_number(state.state) if state else None
        return value is None or value < limit

    def _temperature_from_entity(self, entity_id: str | None) -> float | None:
        """Read a temperature entity in Home Assistant's configured unit."""
        if not entity_id or not (state := self.hass.states.get(entity_id)):
            return None
        value = self._finite_number(state.state)
        source_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if value is None or source_unit not in TemperatureConverter.VALID_UNITS:
            return None
        converted = TemperatureConverter.convert(value, source_unit, self._attr_temperature_unit)
        return converted if math.isfinite(converted) else None

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
