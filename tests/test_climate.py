"""Behavior tests for the direct staged climate controller."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    UnitOfTemperature,
)

from custom_components.diesel_level_thermostat.climate import DieselLevelThermostat
from custom_components.diesel_level_thermostat.const import (
    CONF_CASE_TEMPERATURE,
    CONF_FAULT_SENSOR,
    CONF_HEATER_LEVEL,
    CONF_HEATER_POWER,
    CONF_LEVEL_MODE_VALUE,
    CONF_MAX_CASE_TEMPERATURE,
    CONF_ROOM_TEMPERATURE,
    CONF_ROOM_TEMPERATURE_OFFSET,
    CONF_RUNNING_MODE,
)


class FakeStates:
    """Minimal Home Assistant state registry for controller tests."""

    def __init__(self, states: dict[str, SimpleNamespace]) -> None:
        self._states = states

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self._states.get(entity_id)


def entity(state: str, **attributes: object) -> SimpleNamespace:
    """Build a state-like object."""
    return SimpleNamespace(state=state, attributes=attributes)


def controller(*, mode: str = "Level Mode", voltage: str | None = None):
    """Build a controller backed by Diesel Heater's public entities."""
    data = {
        CONF_ROOM_TEMPERATURE: "sensor.room_temperature",
        CONF_HEATER_POWER: "switch.heater_power",
        CONF_HEATER_LEVEL: "fan.heater_level",
        CONF_RUNNING_MODE: "select.heater_mode",
        CONF_LEVEL_MODE_VALUE: "Level Mode",
    }
    if voltage is not None:
        data["voltage_sensor"] = "sensor.voltage"
    states = {
        "sensor.room_temperature": entity("18", unit_of_measurement="°C"),
        "switch.heater_power": entity(STATE_OFF),
        "fan.heater_level": entity(STATE_OFF, percentage=10),
        "select.heater_mode": entity(mode),
    }
    if voltage is not None:
        states["sensor.voltage"] = entity(voltage)
    services = SimpleNamespace(async_call=AsyncMock())
    hass = SimpleNamespace(
        states=FakeStates(states),
        services=services,
        async_create_task=lambda task: task.close(),
        config=SimpleNamespace(
            units=SimpleNamespace(temperature_unit=UnitOfTemperature.CELSIUS)
        ),
    )
    entry = SimpleNamespace(entry_id="test", title="Test Heater", data=data, options={})
    thermostat = DieselLevelThermostat(hass, entry)
    thermostat._target_temperature = 20
    thermostat._hvac_mode = HVACMode.HEAT
    thermostat.async_write_ha_state = lambda: None
    return thermostat, services


def test_converts_selected_temperature_and_offset() -> None:
    """The selected room sensor and local Celsius offset are the feedback."""
    thermostat, _ = controller()
    thermostat.config[CONF_ROOM_TEMPERATURE_OFFSET] = -5
    assert thermostat.current_temperature == 13


def test_converts_offset_for_fahrenheit_home_assistant() -> None:
    """A Celsius offset remains a Celsius offset in a Fahrenheit installation."""
    thermostat, _ = controller()
    thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat.config[CONF_ROOM_TEMPERATURE_OFFSET] = -5
    assert thermostat.current_temperature == pytest.approx(55.4)


def test_requires_level_mode_and_a_live_room_sensor() -> None:
    """The virtual thermostat is inert outside the supported source contract."""
    thermostat, _ = controller(mode="Temperature Mode")
    assert not thermostat.available
    thermostat, _ = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity("unknown")
    assert not thermostat.available


@pytest.mark.asyncio
async def test_starts_at_level_from_current_temperature_gap() -> None:
    """A 2°C deficit uses the calculated level, then powers the source on."""
    thermostat, services = controller()
    await thermostat._async_control()
    assert services.async_call.await_args_list[0].args[:2] == ("fan", "set_percentage")
    assert services.async_call.await_args_list[0].args[2]["percentage"] == 50
    assert services.async_call.await_args_list[1].args[:2] == ("switch", "turn_on")


@pytest.mark.asyncio
async def test_large_deficit_starts_at_maximum_level() -> None:
    """A large gap selects level 10 immediately."""
    thermostat, services = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "0", unit_of_measurement="°C"
    )
    await thermostat._async_control()
    assert services.async_call.await_args_list[0].args[2]["percentage"] == 100


@pytest.mark.asyncio
async def test_running_heater_directly_reduces_level_as_room_warms() -> None:
    """The next room-temperature update selects the lower demanded level."""
    thermostat, services = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "19.6", unit_of_measurement="°C"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    thermostat.hass.states._states["fan.heater_level"] = entity(STATE_ON, percentage=50)
    await thermostat._async_control()
    services.async_call.assert_awaited_once_with(
        "fan",
        "set_percentage",
        {"entity_id": "fan.heater_level", "percentage": 20},
        blocking=True,
    )


@pytest.mark.asyncio
async def test_fahrenheit_gap_uses_celsius_level_math() -> None:
    """A 5°F gap is not mistaken for a 5°C maximum-demand gap."""
    thermostat, services = controller()
    thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._target_temperature = 60
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "55", unit_of_measurement="°F"
    )
    await thermostat._async_control()
    services.async_call.assert_any_await(
        "fan",
        "set_percentage",
        {"entity_id": "fan.heater_level", "percentage": 60},
        blocking=True,
    )


@pytest.mark.asyncio
async def test_stops_at_target_with_normal_source_power_command() -> None:
    """At target, request normal heater shutdown without a hold or overshoot."""
    thermostat, services = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "20", unit_of_measurement="°C"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    await thermostat._async_control()
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


@pytest.mark.asyncio
async def test_level_mode_transition_shuts_down_when_already_above_target() -> None:
    """Entering Level Mode must immediately evaluate an active thermostat."""
    thermostat, services = controller(mode="Temperature Mode")
    thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._target_temperature = 57
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "61", unit_of_measurement="°F"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    thermostat.hass.states._states["select.heater_mode"] = entity("Level Mode")
    scheduled: list[object] = []
    thermostat.hass.async_create_task = scheduled.append

    thermostat._async_source_changed(MagicMock())

    assert len(scheduled) == 1
    await scheduled[0]
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


@pytest.mark.asyncio
async def test_turning_on_virtual_thermostat_above_target_requests_shutdown() -> None:
    """Enabling an already-Level-mode heater above target must shut it down."""
    thermostat, services = controller()
    thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._target_temperature = 57
    thermostat._hvac_mode = HVACMode.OFF
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "61", unit_of_measurement="°F"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)

    await thermostat.async_turn_on()

    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


@pytest.mark.asyncio
async def test_reported_fahrenheit_overshoot_requests_shutdown() -> None:
    """The reported 54°F to 61°F case must issue an off command at 57°F target."""
    thermostat, services = controller()
    thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._target_temperature = 57
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "61", unit_of_measurement="°F"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    await thermostat._async_control()
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


@pytest.mark.asyncio
async def test_virtual_off_uses_normal_power_command() -> None:
    """Turning the virtual climate off requests the source's normal shutdown."""
    thermostat, services = controller(mode="Temperature Mode")
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    await thermostat.async_turn_off()
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


@pytest.mark.asyncio
async def test_burnoff_and_unavailable_source_remain_hard_gates() -> None:
    """Never interrupt source-owned burn-off or command an unavailable source."""
    thermostat, services = controller()
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    thermostat.hass.states._states["fan.heater_level"] = entity(STATE_UNAVAILABLE)
    await thermostat.async_turn_off()
    assert not thermostat.available
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_configured_safety_input_uses_normal_shutdown() -> None:
    """An already configured safety input still uses the normal power entity."""
    thermostat, services = controller()
    thermostat.config[CONF_FAULT_SENSOR] = "binary_sensor.heater_fault"
    thermostat.hass.states._states["binary_sensor.heater_fault"] = entity(STATE_ON)
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    await thermostat._async_control()
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


@pytest.mark.asyncio
async def test_fahrenheit_safety_temperature_remains_celsius_based() -> None:
    """Existing Celsius safety thresholds still convert source readings correctly."""
    thermostat, services = controller()
    thermostat.config[CONF_CASE_TEMPERATURE] = "sensor.case_temperature"
    thermostat.config[CONF_MAX_CASE_TEMPERATURE] = 100
    thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat.hass.states._states["sensor.case_temperature"] = entity(
        "220", unit_of_measurement="°F"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    await thermostat._async_control()
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )
