"""Switching band boundaries, delayed feedback, restoration and safety."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.diesel_level_thermostat.climate import DieselLevelThermostat
from custom_components.diesel_level_thermostat.config_flow import (
    ConfigFlow,
    DieselLevelThermostatOptionsFlow,
    feedback_errors,
    options_schema,
)
from tests.source_fixtures import source_hass
from tests.test_available_level_backend import source as source  # noqa: F401
from tests.test_climate import controller, entity
from tests.test_setup_and_bindings import entry, registry


def room(h, value, unit="°C"):
    h.hass.states._states["sensor.room_temperature"] = entity(str(value), unit_of_measurement=unit)


def calls(services):
    return [c.args[:2] for c in services.async_call.await_args_list]


@pytest.mark.parametrize("explicit", [False, True])
async def test_zero_preserves_exact_target_switching(explicit):
    h, services = controller()
    if explicit:
        h.config["temperature_hysteresis"] = 0
    room(h, 19.99)
    await h._async_control()
    assert ("switch", "turn_on") in calls(services)
    h.hass.states._states["switch.heater_power"] = entity("on")
    services.async_call.reset_mock()
    room(h, 20)
    await h._async_control()
    assert calls(services) == [("switch", "turn_off")]


@pytest.mark.parametrize(
    "unit,target,lower,upper,middle",
    [
        ("°C", 20, 19, 21, 20),
        ("°F", 68, 66.2, 69.8, 68),
    ],
)
async def test_complete_cycle_and_delayed_power_report(unit, target, lower, upper, middle):
    h, services = controller()
    h._attr_temperature_unit = unit
    h._target_temperature = target
    h.config["temperature_hysteresis"] = 1
    room(h, middle, unit)
    await h._async_control()
    services.async_call.assert_not_awaited()
    room(h, lower, unit)
    await h._async_control()
    assert ("switch", "turn_on") in calls(services)
    h.hass.states._states["switch.heater_power"] = entity("on")
    services.async_call.reset_mock()
    room(h, middle, unit)
    await h._async_control()
    assert ("switch", "turn_off") not in calls(services)
    assert h._heating_cycle is True
    services.async_call.reset_mock()
    room(h, upper, unit)
    await h._async_control()
    assert calls(services) == [("switch", "turn_off")]
    # Still reporting On after accepted shutdown must not relatch heating.
    services.async_call.reset_mock()
    room(h, middle, unit)
    await h._async_control()
    assert ("switch", "turn_on") not in calls(services)
    assert h._heating_cycle is False
    h.hass.states._states["switch.heater_power"] = entity("off")
    services.async_call.reset_mock()
    await h._async_control()
    services.async_call.assert_not_awaited()


@pytest.mark.parametrize("powered,expected", [(True, True), (False, False)])
async def test_initial_cycle_uses_confirmed_power_inside_band(powered, expected):
    h, services = controller()
    h.config["temperature_hysteresis"] = 1
    h.hass.states._states["switch.heater_power"] = entity("on" if powered else "off")
    room(h, 20)
    await h._async_control()
    assert h._heating_cycle is expected
    assert ("switch", "turn_on") not in calls(services)
    assert ("switch", "turn_off") not in calls(services)


@pytest.mark.parametrize("count", range(2, 11))
async def test_levels_still_follow_original_target_and_actual_stage_count(count):
    h, services = controller()
    h.config["temperature_hysteresis"] = 1
    h.hass.states._states["fan.heater_level"] = entity(
        "on", percentage=100, percentage_step=100 / count
    )
    h.hass.states._states["switch.heater_power"] = entity("on")
    room(h, 10)
    await h._async_control()
    assert h._heating_cycle is True
    services.async_call.reset_mock()
    room(h, 20.5)
    await h._async_control()
    assert calls(services) == [("fan", "set_percentage")]
    assert services.async_call.await_args.args[2]["percentage"] == int(100 / count)


@pytest.mark.parametrize("bad", [-0.1, 5.1, "bad", None, float("inf"), float("nan")])
async def test_invalid_band_rejected_by_flow_and_runtime(bad):
    h, services = controller()
    h.config["temperature_hysteresis"] = bad
    h.hass.states._states["switch.heater_power"] = entity("on")
    assert (
        feedback_errors(
            {"room_temperature": "sensor.room_temperature", "temperature_hysteresis": bad}, h.hass
        )["temperature_hysteresis"]
        == "invalid_hysteresis"
    )
    assert not h.available
    await h._async_control()
    assert calls(services) == [("switch", "turn_off")]


@pytest.mark.parametrize("cause", ["off", "fault", "missing_room"])
async def test_band_never_delays_explicit_off_or_feedback_shutdown(cause):
    h, services = controller()
    h.config["temperature_hysteresis"] = 5
    h.hass.states._states["switch.heater_power"] = entity("on")
    room(h, 20)
    if cause == "fault":
        h.config["fault_sensor"] = "binary_sensor.fault"
        h.hass.states._states["binary_sensor.fault"] = entity("on")
    elif cause == "missing_room":
        room(h, "unavailable")
    if cause == "off":
        await h.async_turn_off()
    else:
        await h._async_control()
    assert calls(services) == [("switch", "turn_off")]
    assert h._heating_cycle is False


@pytest.mark.parametrize("guard", ["on", "unavailable", "missing", "disabled"])
async def test_hysteresis_never_bypasses_burnoff_with_available_number(source, guard):
    h, services, rows = source
    h.config["temperature_hysteresis"] = 1
    h._heating_cycle = True
    h.hass.states._states["switch.heater_power"] = entity("on")
    room(h, 22)
    if guard == "disabled":
        rows["binary_sensor.burnoff"].disabled_by = "user"
    elif guard == "missing":
        del h.hass.states._states["binary_sensor.burnoff"]
    else:
        h.hass.states._states["binary_sensor.burnoff"] = entity(guard)
    await h._async_control()
    with pytest.raises(HomeAssistantError, match="shutdown is pending"):
        await h.async_turn_off()
    services.async_call.assert_not_awaited()
    if guard == "disabled":
        rows["binary_sensor.burnoff"].disabled_by = None
    h.hass.states._states["binary_sensor.burnoff"] = entity("off")
    await h._async_control()
    assert calls(services) == [("switch", "turn_off")]


@pytest.mark.parametrize("during", ["level", "power"])
async def test_crossing_upper_threshold_during_write_does_not_leave_stale_start(during):
    h, services = controller()
    h.config["temperature_hysteresis"] = 1
    room(h, 18)

    async def send(domain, service, *args, **kwargs):
        if (during == "level" and domain == "fan") or (during == "power" and service == "turn_on"):
            room(h, 21)

    services.async_call.side_effect = send
    await h._async_control()
    if during == "level":
        assert ("switch", "turn_on") not in calls(services)
    else:
        assert calls(services)[-1] == ("switch", "turn_off")


@pytest.mark.parametrize("cycle", [True, False])
async def test_restart_restores_cycle_even_while_unavailable(cycle):
    h, services = controller()
    h.config["temperature_hysteresis"] = 1
    h._heating_cycle = cycle
    h.hass.states._states["fan.heater_level"] = entity("unavailable")
    saved = entity("unavailable", **h.capability_attributes)
    restored = DieselLevelThermostat(h.hass, h.entry)
    restored.config["temperature_hysteresis"] = 1
    restored.async_write_ha_state = lambda: None
    restored.async_get_last_state = AsyncMock(return_value=saved)
    restored.async_on_remove = MagicMock()
    h.hass.states._states["fan.heater_level"] = entity("on", percentage=10)
    room(h, 20)
    with (
        patch(
            "custom_components.diesel_level_thermostat.climate.source_device_id", return_value=None
        ),
        patch(
            "custom_components.diesel_level_thermostat.climate.source_telemetry_entity_ids",
            return_value=(),
        ),
        patch(
            "custom_components.diesel_level_thermostat.climate.async_track_state_change_event",
            return_value=lambda: None,
        ),
    ):
        await restored.async_added_to_hass()
    assert restored._heating_cycle is cycle
    assert (("switch", "turn_on") in calls(services)) is cycle


def test_setup_and_options_show_zero_default_and_boxed_band():
    flow = ConfigFlow()
    flow._source_data = {"room_temperature": "sensor.room_temperature"}
    for schema in (flow._telemetry_schema(), options_schema("sensor.room_temperature")):
        marker = next(k for k in schema.schema if str(k) == "temperature_hysteresis")
        assert marker.default() == 0
        assert schema.schema[marker].config == {"min": 0, "max": 5, "step": 0.1, "mode": "box"}


async def test_options_save_band_preserves_protection_metadata(monkeypatch):
    e = entry()
    e.data["source_burnoff_guard_required"] = True
    monkeypatch.setattr(DieselLevelThermostatOptionsFlow, "config_entry", property(lambda _: e))
    flow = DieselLevelThermostatOptionsFlow()
    flow.hass = source_hass()
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
    ):
        result = await flow.async_step_init(
            {
                "room_temperature": "sensor.new_room",
                "room_temperature_offset": 0,
                "temperature_hysteresis": 0.7,
            }
        )
    assert result["type"] == "create_entry"
    e.options = result["data"]
    e.title = "Test Heater"
    base, _ = controller()
    h = DieselLevelThermostat(base.hass, e)
    assert h._hysteresis() == 0.7
    assert h._burnoff_guard_required


async def test_actual_source_events_retain_cycle_without_timers():
    h, services = controller()
    h.config["temperature_hysteresis"] = 1
    h.hass.async_create_task = asyncio.create_task
    for temperature, power in ((19, "off"), (20.5, "on"), (21, "on"), (20, "off")):
        room(h, temperature)
        h.hass.states._states["switch.heater_power"] = entity(power)
        h._async_source_changed(None)
        await h._event_task
    assert [c for c in calls(services) if c[0] == "switch"] == [
        ("switch", "turn_on"),
        ("switch", "turn_off"),
    ]
    assert not h._control_dirty


async def test_change_target_rechecks_current_cycle():
    h, services = controller()
    h.config["temperature_hysteresis"] = 1
    room(h, 20)
    await h._async_control()
    services.async_call.assert_not_awaited()
    await h.async_set_temperature(temperature=22)
    assert ("switch", "turn_on") in calls(services)
    h.hass.states._states["switch.heater_power"] = entity("on")
    services.async_call.reset_mock()
    await h.async_set_temperature(temperature=19)
    assert calls(services) == [("switch", "turn_off")]


async def test_options_reload_reapplies_new_band_to_current_reading():
    h, services = controller()
    h.entry.options = {"temperature_hysteresis": 1}
    h.entry.data["temperature_hysteresis"] = 0
    fresh = DieselLevelThermostat(h.hass, h.entry)
    fresh._hvac_mode = HVACMode.HEAT
    fresh._target_temperature = 20
    fresh._heating_cycle = False
    fresh.async_write_ha_state = lambda: None
    room(h, 19.5)
    await fresh._async_control()
    services.async_call.assert_not_awaited()
    assert fresh._hysteresis() == 1


@pytest.mark.parametrize("bad", [-1, 6, "nan"])
async def test_setup_and_options_do_not_persist_invalid_band(monkeypatch, bad):
    e = entry()
    monkeypatch.setattr(DieselLevelThermostatOptionsFlow, "config_entry", property(lambda _: e))
    options = DieselLevelThermostatOptionsFlow()
    options.hass = source_hass()
    setup = ConfigFlow()
    setup.hass = options.hass
    setup._source_data = {"room_temperature": "sensor.new_room"}
    values = {"name": "Test", "room_temperature": "sensor.new_room", "temperature_hysteresis": bad}
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
    ):
        for result in (
            await options.async_step_init(values),
            await setup.async_step_telemetry(values),
        ):
            assert result["type"] == "form"
            assert result["errors"]["temperature_hysteresis"] == "invalid_hysteresis"


async def test_burnoff_prevents_start_below_lower_threshold(source):
    h, services, _ = source
    h.config["temperature_hysteresis"] = 1
    h.hass.states._states["binary_sensor.burnoff"] = entity("on")
    room(h, 15)
    await h._async_control()
    services.async_call.assert_not_awaited()
    h.hass.states._states["binary_sensor.burnoff"] = entity("off")
    await h._async_control()
    assert calls(services) == [("number", "set_value"), ("switch", "turn_on")]


async def test_native_mode_takeover_during_level_write_still_suspends():
    h, services = controller()
    h.config["temperature_hysteresis"] = 1

    async def send(*args, **kwargs):
        h.hass.states._states["select.heater_mode"] = entity("Temperature")

    services.async_call.side_effect = send
    await h._async_control()
    assert calls(services) == [("fan", "set_percentage")]


@pytest.mark.parametrize("change", ["off", "removed", "burnoff"])
async def test_positive_band_does_not_authorize_late_start_after_level_write(source, change):
    h, services, _ = source
    h.config["temperature_hysteresis"] = 1

    async def send(domain, service, *args, **kwargs):
        if domain == "number":
            if change == "off":
                h._hvac_mode = HVACMode.OFF
            elif change == "removed":
                h._removed = True
            else:
                h.hass.states._states["binary_sensor.burnoff"] = entity("on")

    services.async_call.side_effect = send
    await h._async_control()
    assert calls(services) == [("number", "set_value")]


async def test_setup_persists_selected_hysteresis():
    flow = ConfigFlow()
    flow.hass = source_hass()
    flow._source_data = {"source_device_id": "source"}
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
    ):
        result = await flow.async_step_telemetry(
            {
                "name": "Test",
                "room_temperature": "sensor.new_room",
                "room_temperature_offset": 0,
                "temperature_hysteresis": 1.2,
            }
        )
    assert result["type"] == "create_entry"
    assert result["data"]["temperature_hysteresis"] == 1.2
