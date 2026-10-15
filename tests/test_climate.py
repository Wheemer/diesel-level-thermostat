"""Behavior tests for the virtual climate controller."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.const import STATE_OFF, STATE_ON, UnitOfTemperature

from custom_components.diesel_level_thermostat.climate import DieselLevelThermostat
from custom_components.diesel_level_thermostat.const import (
    CONF_HEATER_LEVEL,
    CONF_HEATER_POWER,
    CONF_LEVEL_MODE_VALUE,
    CONF_ROOM_TEMPERATURE,
    CONF_RUNNING_MODE,
)


class FakeStates:
    """Minimal Home Assistant state registry for controller tests."""

    def __init__(self, states: dict[str, SimpleNamespace]) -> None:
        self._states = states

    def get(self, entity_id: str) -> SimpleNamespace | None:
        """Return the state for an entity."""
        return self._states.get(entity_id)


def entity(state: str, **attributes: object) -> SimpleNamespace:
    """Build a state-like object."""
    return SimpleNamespace(state=state, attributes=attributes)


def controller(*, mode: str = "Level Mode", voltage: str | None = None):
    """Build a controller backed by fake public source entities."""
    data = {
        CONF_ROOM_TEMPERATURE: "sensor.room_temperature",
        CONF_HEATER_POWER: "switch.heater_power",
        CONF_HEATER_LEVEL: "fan.heater_level",
        CONF_RUNNING_MODE: "select.heater_mode",
        CONF_LEVEL_MODE_VALUE: "Level Mode",
    }
    if voltage is not None:
        data.update({"voltage_sensor": "sensor.voltage"})
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
        config=SimpleNamespace(
            units=SimpleNamespace(temperature_unit=UnitOfTemperature.CELSIUS)
        ),
    )
    entry = SimpleNamespace(
        entry_id="test",
        title="Test Heater",
        data=data,
        options={"min_off_seconds": 0, "min_on_seconds": 0},
    )
    thermostat = DieselLevelThermostat(hass, entry)
    thermostat._target_temperature = 20
    thermostat._hvac_mode = HVACMode.HEAT
    thermostat.async_write_ha_state = lambda: None
    return thermostat, services


def test_converts_source_temperature_to_home_assistant_unit() -> None:
    """Room input is compared in the configured Home Assistant unit."""
    thermostat, _ = controller()
    thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT

    assert thermostat.current_temperature == 64.4


def test_requires_a_live_room_temperature() -> None:
    """A missing room temperature makes the virtual climate unavailable."""
    thermostat, _ = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity("unknown")

    assert not thermostat.available


@pytest.mark.asyncio
async def test_controls_level_and_power_only_in_level_mode() -> None:
    """A cold room requests staged output through public entity services."""
    thermostat, services = controller()

    await thermostat._async_control()

    assert services.async_call.await_args_list[0].args[:2] == ("fan", "set_percentage")
    assert services.async_call.await_args_list[0].kwargs["blocking"] is True
    assert services.async_call.await_args_list[0].args[2]["percentage"] == 50
    assert services.async_call.await_args_list[1].args[:2] == ("switch", "turn_on")


@pytest.mark.asyncio
async def test_never_controls_source_outside_level_mode() -> None:
    """Native temperature mode makes the virtual thermostat inert."""
    thermostat, services = controller(mode="Temperature Mode")

    await thermostat._async_control()

    services.async_call.assert_not_awaited()
    assert not thermostat.available


@pytest.mark.asyncio
async def test_configured_low_voltage_stops_source() -> None:
    """An enabled voltage threshold requests a normal source shutdown."""
    thermostat, services = controller(voltage="11.5")
    thermostat.config["min_voltage"] = 12.0
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)

    await thermostat._async_control()

    services.async_call.assert_awaited_once_with(
        "switch",
        "turn_off",
        {"entity_id": "switch.heater_power"},
        blocking=True,
    )
