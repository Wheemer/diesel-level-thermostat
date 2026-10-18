"""Behavior tests for the virtual climate controller."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    UnitOfTemperature,
)

import custom_components.diesel_level_thermostat.climate as climate_module
from custom_components.diesel_level_thermostat.climate import DieselLevelThermostat
from custom_components.diesel_level_thermostat.const import (
    CONF_HEATER_LEVEL,
    CONF_HEATER_POWER,
    CONF_LEVEL_MODE_VALUE,
    CONF_MIN_LEVEL_CHANGE_SECONDS,
    CONF_ROOM_TEMPERATURE,
    CONF_ROOM_TEMPERATURE_OFFSET,
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
        async_create_task=lambda task: task.close(),
        config=SimpleNamespace(
            units=SimpleNamespace(temperature_unit=UnitOfTemperature.CELSIUS)
        ),
    )
    entry = SimpleNamespace(
        entry_id="test",
        title="Test Heater",
        data=data,
        options={
            "min_off_seconds": 0,
            "min_on_seconds": 0,
            CONF_MIN_LEVEL_CHANGE_SECONDS: 0,
        },
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


def test_applies_room_temperature_offset_in_celsius() -> None:
    """The configured correction applies after source-unit conversion."""
    thermostat, _ = controller()
    thermostat.config[CONF_ROOM_TEMPERATURE_OFFSET] = -5

    assert thermostat.current_temperature == 13


def test_converts_room_temperature_offset_for_fahrenheit() -> None:
    """A Celsius correction remains a Celsius correction in Fahrenheit HA."""
    thermostat, _ = controller()
    thermostat.hass.config.units.temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat._attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    thermostat.config[CONF_ROOM_TEMPERATURE_OFFSET] = -5

    assert thermostat.current_temperature == pytest.approx(55.4)


def test_requires_a_live_room_temperature() -> None:
    """A missing room temperature makes the virtual climate unavailable."""
    thermostat, _ = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity("unknown")

    assert not thermostat.available


def test_accepts_current_and_legacy_level_mode_labels() -> None:
    """Source integration releases use both Level and Level Mode labels."""
    thermostat, _ = controller(mode="Level")

    assert thermostat.available


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
async def test_large_cold_start_immediately_selects_maximum_level() -> None:
    """A large error starts at the required output instead of slowly ramping up."""
    thermostat, services = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "0", unit_of_measurement="°C"
    )

    await thermostat._async_control()

    assert services.async_call.await_args_list[0].args[:2] == (
        "fan",
        "set_percentage",
    )
    assert services.async_call.await_args_list[0].args[2]["percentage"] == 100
    assert services.async_call.await_args_list[1].args[:2] == ("switch", "turn_on")


@pytest.mark.asyncio
async def test_running_heater_downstages_inside_the_stop_deadband() -> None:
    """A running heater reduces output before the hot cutoff is reached."""
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
        {"entity_id": "fan.heater_level", "percentage": 40},
        blocking=True,
    )


@pytest.mark.asyncio
async def test_running_heater_downstages_one_level_per_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The controller schedules a gradual descent until demand is reached."""
    thermostat, services = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "19.6", unit_of_measurement="°C"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    thermostat.hass.states._states["fan.heater_level"] = entity(
        STATE_ON, percentage=100
    )
    thermostat.config[CONF_MIN_LEVEL_CHANGE_SECONDS] = 60
    scheduled: dict[str, object] = {}

    monkeypatch.setattr(
        climate_module,
        "async_call_later",
        lambda _hass, delay, callback: (
            scheduled.update({"delay": delay, "callback": callback}) or (lambda: None)
        ),
    )

    await thermostat._async_control()

    services.async_call.assert_awaited_once_with(
        "fan",
        "set_percentage",
        {"entity_id": "fan.heater_level", "percentage": 90},
        blocking=True,
    )
    assert scheduled["delay"] == 60


@pytest.mark.asyncio
async def test_stopped_heater_waits_for_the_start_threshold() -> None:
    """The start tolerance prevents a restart inside the deadband."""
    thermostat, services = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "19.6", unit_of_measurement="°C"
    )

    await thermostat._async_control()

    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_power_cutoff_is_scheduled_at_the_exact_minimum_on_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hot running heater is retried when its minimum run time expires."""
    thermostat, services = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "21", unit_of_measurement="°C"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    thermostat.config["min_on_seconds"] = 300
    thermostat._last_power_change = 100
    scheduled: dict[str, object] = {}

    monkeypatch.setattr(climate_module.time, "monotonic", lambda: 101)
    monkeypatch.setattr(
        climate_module,
        "async_call_later",
        lambda _hass, delay, callback: (
            scheduled.update({"delay": delay, "callback": callback}) or (lambda: None)
        ),
    )

    await thermostat._async_control()

    services.async_call.assert_not_awaited()
    assert scheduled["delay"] == 299


@pytest.mark.asyncio
async def test_running_level_change_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source level is not changed again before its configured interval."""
    thermostat, services = controller()
    thermostat.hass.states._states["sensor.room_temperature"] = entity(
        "19.6", unit_of_measurement="°C"
    )
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    thermostat.hass.states._states["fan.heater_level"] = entity(STATE_ON, percentage=50)
    thermostat.config[CONF_MIN_LEVEL_CHANGE_SECONDS] = 60
    thermostat._last_level_change = 100
    scheduled: dict[str, object] = {}

    monkeypatch.setattr(climate_module.time, "monotonic", lambda: 101)
    monkeypatch.setattr(
        climate_module,
        "async_call_later",
        lambda _hass, delay, callback: (
            scheduled.update({"delay": delay, "callback": callback}) or (lambda: None)
        ),
    )

    await thermostat._async_control()

    services.async_call.assert_not_awaited()
    assert scheduled["delay"] == 59


@pytest.mark.asyncio
async def test_running_heater_increases_output_without_waiting_for_descent_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rising demand is immediate; only output reductions are paced."""
    thermostat, services = controller()
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    thermostat.config[CONF_MIN_LEVEL_CHANGE_SECONDS] = 60
    thermostat._last_level_change = 100
    monkeypatch.setattr(climate_module.time, "monotonic", lambda: 101)

    await thermostat._async_control()

    services.async_call.assert_awaited_once_with(
        "fan",
        "set_percentage",
        {"entity_id": "fan.heater_level", "percentage": 50},
        blocking=True,
    )


def test_existing_source_is_guarded_after_controller_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restarting HA never assumes a running heater has met its timing guard."""
    thermostat, _ = controller()
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    monkeypatch.setattr(climate_module.time, "monotonic", lambda: 100)

    thermostat._initialize_running_source_timing()

    assert thermostat._last_power_change == 100
    assert thermostat._last_level_change == 100


def test_external_source_power_change_resets_transition_timing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual source changes receive the same timing safeguards as our own."""
    thermostat, _ = controller()
    monkeypatch.setattr(climate_module.time, "monotonic", lambda: 100)

    thermostat._async_source_changed(
        SimpleNamespace(data={"entity_id": "switch.heater_power"})
    )

    assert thermostat._last_power_change == 100


@pytest.mark.asyncio
async def test_virtual_off_cancels_deferred_control_and_shuts_down_source() -> None:
    """Virtual off owns its pending work and uses the source's normal shutdown."""
    thermostat, services = controller()
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    cancel = Mock()
    thermostat._control_callback = cancel
    thermostat._next_control_at = 500

    await thermostat.async_set_hvac_mode(HVACMode.OFF)

    cancel.assert_called_once_with()
    assert thermostat._control_callback is None
    assert thermostat._next_control_at is None
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


@pytest.mark.asyncio
async def test_never_controls_an_unavailable_source_power_entity() -> None:
    """An unavailable source Power entity makes the controller fail closed."""
    thermostat, services = controller()
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_UNAVAILABLE)

    await thermostat._async_control()

    services.async_call.assert_not_awaited()
    assert not thermostat.available


@pytest.mark.asyncio
async def test_virtual_off_does_not_interrupt_a_source_burnoff() -> None:
    """The unavailable source Level fan is a hard cooldown-control gate."""
    thermostat, services = controller()
    thermostat.hass.states._states["switch.heater_power"] = entity(STATE_ON)
    thermostat.hass.states._states["fan.heater_level"] = entity(STATE_UNAVAILABLE)

    await thermostat.async_set_hvac_mode(HVACMode.OFF)

    services.async_call.assert_not_awaited()
    assert not thermostat.available


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
