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
from homeassistant.helpers.device_registry import DeviceEntryType
from homeassistant.helpers.entity import DeviceInfo
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
    CONF_SOURCE_BURNOFF_GUARD_REQUIRED,
    CONF_SOURCE_DEVICE_ID,
    CONF_TEMPERATURE_HYSTERESIS,
    CONF_VOLTAGE_SENSOR,
    DEFAULT_MIN_LEVEL,
    DEFAULT_PROPORTIONAL_BAND,
    DEFAULT_ROOM_TEMPERATURE_OFFSET,
    DEFAULT_TEMPERATURE_HYSTERESIS,
    DOMAIN,
    LEVEL_MODE_VALUES,
    OPTIONAL_ENTITY_KEYS,
)
from .control import target_level
from .level import current_level, level_count, level_unavailable_reason
from .source import (
    select_level_entity,
    source_binding_identity,
    source_control_contract,
    source_device_id,
    source_guards_enabled,
    source_telemetry_entity_ids,
)

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
        # Each configured thermostat owns a virtual service device. Keep its
        # existing entity unique ID and name; do not attach it to the heater.
        self._attr_name = None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Diesel Level Thermostat",
            model="Level Thermostat",
            entry_type=DeviceEntryType.SERVICE,
        )
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
        self._burnoff_guard_required = bool(self.config.get(CONF_SOURCE_BURNOFF_GUARD_REQUIRED))
        self._removed = False
        self._control_tasks: set[asyncio.Task] = set()
        self._event_task: asyncio.Task | None = None
        self._control_dirty = False
        self._power_on_pending = False
        self._power_on_in_flight = False
        self._start_recovery_reported = False
        self._selected_level_entity: str | None = None
        self._heating_cycle: bool | None = None
        self._shutdown_pending = False
        self._shutdown_block_reason: str | None = None
        self._last_control_error: str | None = None

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

    def _level_context(self):
        """Rediscover only the validated contract; never trust an alternate name."""
        if not self._source_device_id:
            if self._burnoff_guard_required:
                return None
            return (self.config[CONF_HEATER_LEVEL],), ()
        contract = source_control_contract(self.hass, self._source_device_id)
        if contract is None:
            return None
        controls, candidates, guards = contract
        self._burnoff_guard_required = self._burnoff_guard_required or bool(guards)
        if self._burnoff_guard_required and not guards:
            return None
        if (
            controls[CONF_HEATER_POWER] != self.config[CONF_HEATER_POWER]
            or controls[CONF_RUNNING_MODE] != self.config[CONF_RUNNING_MODE]
        ):
            return None
        return candidates, guards

    def _burnoff_blocked(self) -> bool:
        context = self._level_context()
        return (
            context is None
            or (bool(context[1]) and not source_guards_enabled(self.hass, context[1]))
            or any(
                not (state := self.hass.states.get(entity_id)) or state.state != STATE_OFF
                for entity_id in context[1]
            )
        )

    def _level_entity_id(self) -> str:
        context = self._level_context()
        if context is None:
            return self.config[CONF_HEATER_LEVEL]
        candidates, _ = context
        mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
        self._selected_level_entity = select_level_entity(
            self.hass,
            candidates,
            self._selected_level_entity or self.config[CONF_HEATER_LEVEL],
            allow_available=bool(mode and self._is_level_mode(mode.state))
            and not self._burnoff_blocked(),
        )
        return self._selected_level_entity

    def _source_unavailable_reason(self) -> str | None:
        """Explain the required source capability without hiding the failed gate."""
        if self._removed:
            return "removed"
        if self._hysteresis() is None:
            return "invalid_temperature_hysteresis"
        power = self.hass.states.get(self.config[CONF_HEATER_POWER])
        mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
        if not power or power.state not in (STATE_ON, STATE_OFF):
            return "source_power_unavailable"
        if not mode or not self._is_level_mode(mode.state):
            return "source_mode_not_level"
        if self._level_context() is None:
            return "source_control_contract_invalid"
        if self._burnoff_blocked():
            return "source_burnoff_active_or_unavailable"
        return level_unavailable_reason(self.hass, self._level_entity_id())

    def _source_ready(self) -> bool:
        """Require a writable Level-mode source, without assuming ten speeds."""
        return self._source_unavailable_reason() is None

    @property
    def available(self) -> bool:
        """Expose control only with valid room feedback and a Level-mode source."""
        return self._source_ready() and self.current_temperature is not None

    @property
    def capability_attributes(self) -> dict[str, Any]:
        """Keep binding diagnostics visible while HA marks this entity unavailable."""
        attributes = dict(super().capability_attributes or {})
        diagnostics = self.extra_state_attributes
        for key in (
            "source_power",
            "source_level",
            "configured_source_level",
            "source_mode",
            "source_room_temperature",
            "unavailable_reason",
            "shutdown_pending",
            "shutdown_waiting_for_start",
            "power_on_pending",
            "control_hvac_mode",
            "heating_cycle",
            "temperature_hysteresis_c",
            "control_target_temperature",
            "control_temperature_unit",
            "shutdown_block_reason",
            "last_control_error",
        ):
            attributes[key] = diagnostics[key]
        attributes["source_level_candidates"] = diagnostics["source_level_candidates"]
        attributes["source_states"] = {
            key: {
                "state": state.state if state else None,
                "attributes": dict(state.attributes) if state else {},
            }
            for key in ("source_power", "source_level", "source_mode", "source_room_temperature")
            if (entity_id := diagnostics[key])
            and (state := self.hass.states.get(entity_id)) is not None
        }
        attributes["source_candidate_states"] = {
            entity_id: {
                "state": state.state if state else None,
                "attributes": dict(state.attributes) if state else {},
            }
            for entity_id in diagnostics["source_level_candidates"]
            for state in (self.hass.states.get(entity_id),)
        }
        return attributes

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the source entities that drive this controller."""
        attributes: dict[str, Any] = {
            "control_hvac_mode": self._hvac_mode,
            "heating_cycle": self._heating_cycle,
            "temperature_hysteresis_c": self._hysteresis(),
            "control_temperature_unit": self._attr_temperature_unit,
            "control_target_temperature": self._target_temperature,
            "source_power": self.config[CONF_HEATER_POWER],
            "source_level": self._level_entity_id(),
            "configured_source_level": self.config[CONF_HEATER_LEVEL],
            "source_level_candidates": list(context[0])
            if (context := self._level_context())
            else [],
            "source_mode": self.config[CONF_RUNNING_MODE],
            "source_room_temperature": self.config[CONF_ROOM_TEMPERATURE],
            "unavailable_reason": self._source_unavailable_reason()
            or ("room_temperature_invalid" if self.current_temperature is None else None),
            "shutdown_pending": self._shutdown_pending,
            "shutdown_waiting_for_start": self._shutdown_pending and self._power_on_pending,
            "power_on_pending": self._power_on_pending,
            "shutdown_block_reason": self._shutdown_block_reason,
            "last_control_error": self._last_control_error,
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
            waiting_for_start = old_state.attributes.get("shutdown_waiting_for_start") is True
            self._shutdown_pending = old_state.attributes.get("shutdown_pending") is True and (
                restored_mode == HVACMode.OFF or waiting_for_start
            )
            self._power_on_pending = self._shutdown_pending and waiting_for_start
            if restored_mode in (HVACMode.HEAT, HVACMode.OFF):
                self._hvac_mode = HVACMode(restored_mode)
            cycle = old_state.attributes.get("heating_cycle")
            if self._hvac_mode == HVACMode.HEAT and isinstance(cycle, bool):
                self._heating_cycle = cycle
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
            elif (
                old_state.attributes.get(ATTR_TEMPERATURE) is not None
                or "control_target_temperature" in old_state.attributes
            ):
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
        if context := self._level_context():
            watched.extend(context[0])
            watched.extend(context[1])
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, sorted(set(watched)), self._async_source_changed
            )
        )
        try:
            await self._async_control()
        except HomeAssistantError as err:
            self._last_control_error = str(err)
            _LOGGER.exception(
                "Initial source control failed; thermostat remains registered for recovery"
            )

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
    def _async_source_changed(self, event: Event | None) -> None:
        """Coalesce state bursts without adding a timer or delaying control."""
        if self._removed:
            return
        if self._is_source_on():
            self._power_on_pending = False
            self._start_recovery_reported = False
        elif (
            self._power_on_pending
            and not self._power_on_in_flight
            and self._hvac_mode == HVACMode.HEAT
            and event is not None
            and event.data.get("entity_id") == self.config[CONF_HEATER_POWER]
            and (previous := event.data.get("old_state")) is not None
            and previous.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            and (current := event.data.get("new_state")) is not None
            and current.state == STATE_OFF
        ):
            # Allow a new request after the source recovers. Unrelated or
            # repeated Off reports may still predate the previous start.
            self._start_recovery_reported = True
        self.async_write_ha_state()
        self._control_dirty = True
        if self._event_task is None or self._event_task.done():
            self._event_task = self.hass.async_create_task(self._async_event_control())
            self._control_tasks.add(self._event_task)
            self._event_task.add_done_callback(self._control_tasks.discard)

    async def _async_event_control(self) -> None:
        while self._control_dirty and not self._removed:
            self._control_dirty = False
            try:
                await self._async_control()
                self._last_control_error = None
            except HomeAssistantError as err:
                self._last_control_error = str(err)
                _LOGGER.exception(
                    "Source heater service failed; pending state updates remain eligible"
                )
            # Only drain events that actually arrived during this pass. A
            # failure alone never schedules a retry or creates a busy loop.
            if not self._removed:
                self.async_write_ha_state()

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
        if hvac_mode != self._hvac_mode:
            self._heating_cycle = None if hvac_mode == HVACMode.HEAT else False
        self._hvac_mode = hvac_mode
        if hvac_mode == HVACMode.HEAT:
            self._shutdown_pending = False
            self._shutdown_block_reason = None
        self.async_write_ha_state()
        if hvac_mode == HVACMode.OFF:
            async with self._control_lock:
                if self._removed or self._hvac_mode != HVACMode.OFF:
                    return
                if not await self._turn_source_off("virtual thermostat turned off"):
                    self.async_write_ha_state()
                    raise HomeAssistantError(
                        "Heating demand is Off, but heater shutdown is pending: "
                        + (self._shutdown_block_reason or "source unavailable")
                    )
        else:
            await self._async_control(retry_start=True)
        if not self._removed:
            self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Enable virtual heating demand."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Disable virtual heating demand."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def _async_control(self, *, retry_start: bool = False) -> None:
        """Apply current demand; recheck it after every awaited level write."""
        task = asyncio.current_task()
        self._control_tasks.add(task)
        try:
            async with self._control_lock:
                if self._removed:
                    return
                if self._hvac_mode != HVACMode.HEAT:
                    if self._shutdown_pending:
                        await self._turn_source_off("pending virtual Off request")
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
                if not self._heat_required(temperature):
                    await self._turn_source_off("temperature switching threshold reached")
                    return
                self._shutdown_pending = False
                self._shutdown_block_reason = None
                demand_level = target_level(
                    current_temperature=control_temperature,
                    target_temperature=control_target,
                    min_level=DEFAULT_MIN_LEVEL,
                    max_level=self._source_level_count(),
                    proportional_band=DEFAULT_PROPORTIONAL_BAND,
                )
                # A service call yields to Off, sensor updates, mode changes and
                # removal. Its old demand is never permission to start later.
                snapshot = (temperature, self._target_temperature, self._hysteresis())
                written = await self._set_source_level(demand_level)
                if written is None:
                    return
                if self._removed or self._hvac_mode != HVACMode.HEAT:
                    return
                mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
                if not mode or not self._is_level_mode(mode.state):
                    return
                if self._safety_blocked() or self.current_temperature is None:
                    await self._turn_source_off("feedback changed during control")
                    return
                if not self._source_ready() or written != self._level_write_snapshot():
                    return
                if snapshot != (
                    self.current_temperature,
                    self._target_temperature,
                    self._hysteresis(),
                ):
                    return
                if self._is_source_on():
                    self._power_on_pending = False
                    self._start_recovery_reported = False
                elif not self._power_on_pending or retry_start or self._start_recovery_reported:
                    self._start_recovery_reported = False
                    self._power_on_pending = True
                    self._power_on_in_flight = True
                    try:
                        await self._call_source("switch", "turn_on", self.config[CONF_HEATER_POWER])
                    except HomeAssistantError:
                        self._power_on_pending = False
                        raise
                    finally:
                        self._power_on_in_flight = False
                    if self._is_source_on():
                        self._power_on_pending = False
                    if self._removed:
                        return
                    if self._hvac_mode == HVACMode.OFF:
                        await self._turn_source_off("virtual thermostat turned off during startup")
                    else:
                        mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
                        # A native mode takeover suspends our ownership. While
                        # still in Level mode, recheck demand and safety after
                        # the power service yields, just as for level writes.
                        if mode and self._is_level_mode(mode.state):
                            temperature = self.current_temperature
                            if self._safety_blocked() or temperature is None:
                                await self._turn_source_off("feedback changed during startup")
                            elif not self._heat_required(temperature):
                                await self._turn_source_off("target reached during startup")
                if not self._removed:
                    self.async_write_ha_state()
        finally:
            self._control_tasks.discard(task)

    def _hysteresis(self) -> float | None:
        """Return the configured Celsius delta, rejecting invalid saved values."""
        value = self._finite_number(
            self.config.get(CONF_TEMPERATURE_HYSTERESIS, DEFAULT_TEMPERATURE_HYSTERESIS)
        )
        return value if value is not None and 0 <= value <= 5 else None

    def _heat_required(self, temperature: float) -> bool:
        """Latch demand between thresholds, independently of delayed power reports."""
        band = self._hysteresis()
        if band is None or self._target_temperature is None:
            self._heating_cycle = False
            return False
        current = self._to_celsius(temperature)
        target = self._to_celsius(self._target_temperature)
        # Unit conversion can put an exact Fahrenheit boundary a few ulps
        # below its Celsius equivalent. Only absorb numerical roundoff.
        at_upper = band > 0 and math.isclose(current, target + band, rel_tol=0, abs_tol=1e-9)
        at_lower = band > 0 and math.isclose(current, target - band, rel_tol=0, abs_tol=1e-9)
        if current >= target + band or at_upper:
            self._heating_cycle = False
        elif current < target - band or at_lower:
            self._heating_cycle = True
        elif self._heating_cycle is None:
            self._heating_cycle = self._is_source_on() or self._power_on_pending
        return bool(self._heating_cycle)

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
        return level_count(self.hass, self._level_entity_id())

    def _source_level(self) -> int | None:
        return current_level(self.hass, self._level_entity_id())

    def _level_write_snapshot(self) -> tuple:
        entity_id = self._level_entity_id()
        identity = (
            source_binding_identity(
                self.hass,
                (self.config[CONF_HEATER_POWER], self.config[CONF_RUNNING_MODE], entity_id),
            )
            if self._source_device_id
            else ()
        )
        return entity_id, level_count(self.hass, entity_id), identity

    async def _set_source_level(self, level: int) -> tuple | None:
        """Revalidate the selected public backend immediately before its write."""
        if not self._source_ready() or self._safety_blocked() or self.current_temperature is None:
            return None
        entity_id = self._level_entity_id()
        count = level_count(self.hass, entity_id)
        if count is None or not 1 <= level <= count:
            return None
        snapshot = self._level_write_snapshot()
        if current_level(self.hass, entity_id) != level:
            if entity_id.startswith("number."):
                await self._call_source("number", "set_value", entity_id, value=level)
            else:
                await self._call_source(
                    "fan",
                    "set_percentage",
                    entity_id,
                    percentage=max(1, int(level * 100 / count)),
                )
        return snapshot

    def _can_request_source_shutdown(self) -> bool:
        """Keep burn-off/cooldown ownership with the public source integration."""
        mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
        if not mode or not self._is_level_mode(mode.state) or self._burnoff_blocked():
            return False
        level = self.hass.states.get(self._level_entity_id())
        return bool(level and level.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN))

    async def _turn_source_off(self, reason: str) -> bool:
        """Request normal shutdown, retaining a blocked demand for source events."""
        if self._removed:
            return False
        self._heating_cycle = False
        # A temporary source lock can block a queued retry without canceling
        # Heat. Preserve the recovery signal until demand can be rechecked.
        if self._hvac_mode == HVACMode.OFF:
            self._start_recovery_reported = False
        mode = self.hass.states.get(self.config[CONF_RUNNING_MODE])
        if (
            mode
            and mode.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
            and not self._is_level_mode(mode.state)
        ):
            # Native heating or ventilation owns this controller now.
            self._power_on_pending = False
            self._start_recovery_reported = False
            self._shutdown_pending = False
            self._shutdown_block_reason = None
            return True
        power = self.hass.states.get(self.config[CONF_HEATER_POWER])
        if power and power.state == STATE_OFF:
            if not self._power_on_pending:
                self._shutdown_pending = False
                self._shutdown_block_reason = None
                return True
            if self._shutdown_pending:
                # Keep watching for a delayed ignition without repeating Off
                # against the same reported Off state.
                return True
        self._shutdown_pending = True
        if not self._can_request_source_shutdown():
            self._shutdown_block_reason = (
                self._source_unavailable_reason() or "source_shutdown_locked"
            )
            _LOGGER.warning("Shutdown deferred (%s): %s", reason, self._shutdown_block_reason)
            return False
        power = self.hass.states.get(self.config[CONF_HEATER_POWER])
        if not power or power.state not in (STATE_ON, STATE_OFF):
            self._shutdown_block_reason = "source_power_unavailable"
            _LOGGER.warning("Shutdown deferred (%s): source Power is unavailable", reason)
            return False
        self._shutdown_block_reason = None
        if power.state == STATE_ON:
            self._power_on_pending = False
        _LOGGER.info("Requesting source heater shutdown: %s", reason)
        try:
            await self._call_source("switch", "turn_off", self.config[CONF_HEATER_POWER])
        except HomeAssistantError:
            self._shutdown_block_reason = "source_shutdown_failed"
            raise
        # An Off state we already had before an unconfirmed start cannot
        # confirm cancellation. Keep the intent until the late On is handled.
        reported_power = self.hass.states.get(self.config[CONF_HEATER_POWER])
        if reported_power and reported_power.state == STATE_ON:
            self._power_on_pending = False
        self._shutdown_pending = (
            self._power_on_pending or not reported_power or reported_power.state != STATE_OFF
        )
        return True

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
        if not limit:
            return False
        if not entity_id:
            return True
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
