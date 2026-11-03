"""Regression scenarios reproduced by the offline end-to-end audit."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.diesel_level_thermostat.control import target_level

from .test_climate import controller, entity


@pytest.mark.parametrize("value", ["unavailable", "unknown", "nan", "inf", "-inf", "bad"])
async def test_lost_or_invalid_room_feedback_requests_normal_shutdown(value):
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["sensor.room_temperature"] = entity(value, unit_of_measurement="°C")
    assert not h.available
    await h._async_control()
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


async def test_kelvin_room_feedback_and_celsius_offset():
    h, _ = controller()
    h.hass.states._states["sensor.room_temperature"] = entity("293.15", unit_of_measurement="K")
    h.config["room_temperature_offset"] = -5
    assert h.current_temperature == pytest.approx(15)


@pytest.mark.parametrize("target", [float("nan"), float("inf"), -100, 100])
async def test_invalid_target_is_rejected_without_heater_commands(target):
    h, services = controller()
    with pytest.raises(HomeAssistantError):
        await h.async_set_temperature(temperature=target)
    services.async_call.assert_not_awaited()


@pytest.mark.parametrize("count", [6, 10])
def test_all_actual_heater_levels_are_reachable(count):
    for level in range(1, count + 1):
        gap = (level - 0.5) * 5 / count
        assert (
            target_level(
                current_temperature=20 - gap,
                target_temperature=20,
                min_level=1,
                max_level=count,
                proportional_band=5,
            )
            == level
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_level_math_rejects_nonfinite_values(value):
    with pytest.raises(ValueError):
        target_level(
            current_temperature=value,
            target_temperature=20,
            min_level=1,
            max_level=10,
            proportional_band=5,
        )


async def test_six_level_abba_feedback_matches_demand_without_repeat_writes():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["sensor.room_temperature"] = entity("19.9", unit_of_measurement="°C")
    h.hass.states._states["fan.heater_level"] = entity("on", percentage=16, percentage_step=100 / 6)
    assert h._source_level() == 1
    await h._async_control()
    await h._async_control()
    services.async_call.assert_not_awaited()


async def test_six_level_output_uses_source_percentage_steps():
    h, services = controller()
    h.hass.states._states["fan.heater_level"] = entity("off", percentage=0, percentage_step=100 / 6)
    await h._set_source_level(2)
    assert services.async_call.await_args.args[2]["percentage"] == 33


async def test_virtual_off_during_level_write_prevents_late_start():
    h, services = controller()
    started, release = asyncio.Event(), asyncio.Event()

    async def service(domain, action, data, blocking):
        if domain == "fan":
            started.set()
            await release.wait()

    services.async_call.side_effect = service
    control = asyncio.create_task(h._async_control())
    await started.wait()
    off = asyncio.create_task(h.async_turn_off())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(control, off)
    assert all(c.args[1] != "turn_on" for c in services.async_call.await_args_list)


async def test_off_during_power_on_handles_delayed_switch_feedback():
    h, services = controller()
    started, release = asyncio.Event(), asyncio.Event()

    async def service(domain, action, data, blocking):
        if action == "turn_on":
            started.set()
            await release.wait()

    services.async_call.side_effect = service
    control = asyncio.create_task(h._async_control())
    await started.wait()
    off = asyncio.create_task(h.async_turn_off())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(control, off)
    assert [c.args[1] for c in services.async_call.await_args_list] == [
        "set_percentage",
        "turn_on",
        "turn_off",
    ]


async def test_removal_cancels_queued_control_and_prevents_new_callbacks():
    h, services = controller()
    await h._control_lock.acquire()
    task = asyncio.create_task(h._async_control())
    await asyncio.sleep(0)
    await h.async_will_remove_from_hass()
    h._control_lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    h._async_source_changed(None)
    await h._async_control()
    services.async_call.assert_not_awaited()


async def test_feedback_change_during_level_write_cannot_start_heater():
    h, services = controller()

    async def service(domain, action, data, blocking):
        if domain == "fan":
            h.hass.states._states["sensor.room_temperature"] = entity(
                "22", unit_of_measurement="°C"
            )

    services.async_call.side_effect = service
    await h._async_control()
    assert all(c.args[1] != "turn_on" for c in services.async_call.await_args_list)


async def test_no_automatic_commands_outside_level_mode_even_with_fault():
    h, services = controller(mode="Temperature Mode")
    h.config["fault_sensor"] = "binary_sensor.fault"
    h.hass.states._states["binary_sensor.fault"] = entity("on")
    h.hass.states._states["switch.heater_power"] = entity("on")
    await h._async_control()
    services.async_call.assert_not_awaited()


async def test_no_duplicate_power_on_while_switch_feedback_is_pending():
    h, services = controller()
    await h._async_control()
    await h._async_control()
    assert sum(c.args[1] == "turn_on" for c in services.async_call.await_args_list) == 1


@pytest.mark.parametrize(
    "stored_unit,target,current_unit,expected",
    [("°F", 68, "°C", 20), ("°C", 20, "°F", 68), ("K", 293.15, "°C", 20)],
)
async def test_restore_converts_persisted_native_unit(stored_unit, target, current_unit, expected):
    h, _ = controller()
    h._attr_temperature_unit = current_unit
    if current_unit == "°F":
        h._attr_min_temp, h._attr_max_temp = 41, 95
    h.async_get_last_state = AsyncMock(
        return_value=entity("off", temperature=target, control_temperature_unit=stored_unit)
    )
    h.async_on_remove = lambda remove: None
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
        await h.async_added_to_hass()
    assert h.target_temperature == pytest.approx(expected)


async def test_legacy_restore_uses_recorded_display_bounds():
    h, _ = controller()
    h.async_get_last_state = AsyncMock(
        return_value=entity("off", temperature=68, min_temp=41, max_temp=95)
    )
    h.async_on_remove = lambda remove: None
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
        await h.async_added_to_hass()
    assert h.target_temperature == 20


@pytest.mark.parametrize("target", [float("nan"), "bad", 1000])
async def test_invalid_restore_does_not_resume_heat(target):
    h, services = controller()
    h.async_get_last_state = AsyncMock(
        return_value=entity("heat", temperature=target, control_temperature_unit="°C")
    )
    h.async_on_remove = lambda remove: None
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
        await h.async_added_to_hass()
    assert h.hvac_mode == HVACMode.OFF
    services.async_call.assert_not_awaited()


async def test_one_actual_ha_temperature_event_schedules_one_control(tmp_path):
    h, _ = controller()
    h.hass = HomeAssistant(str(tmp_path))
    h._hvac_mode = HVACMode.OFF
    h.async_get_last_state = AsyncMock(return_value=None)
    h._async_control = AsyncMock()
    removers = []
    h.async_on_remove = removers.append
    with (
        patch(
            "custom_components.diesel_level_thermostat.climate.source_device_id",
            return_value="source",
        ),
        patch(
            "custom_components.diesel_level_thermostat.climate.source_telemetry_entity_ids",
            return_value=("sensor.room_temperature",),
        ),
    ):
        await h.async_added_to_hass()
    h._async_control.reset_mock()
    h.hass.states.async_set("sensor.room_temperature", "18", {"unit_of_measurement": "°C"})
    await h.hass.async_block_till_done()
    h._async_control.assert_awaited_once()
    await h.async_will_remove_from_hass()
    for remove in removers:
        remove()


async def test_burst_events_coalesce_without_timers():
    h, _ = controller()
    started, release = asyncio.Event(), asyncio.Event()

    async def control():
        started.set()
        await release.wait()

    h._async_control = AsyncMock(side_effect=control)
    h.hass.async_create_task = asyncio.create_task
    h._async_source_changed(None)
    await started.wait()
    for _ in range(20):
        h._async_source_changed(None)
    release.set()
    await h._event_task
    assert h._async_control.await_count == 2
    await h.async_will_remove_from_hass()


async def test_native_restore_value_is_independent_of_display_conversion():
    h, _ = controller()
    h.async_get_last_state = AsyncMock(
        return_value=entity(
            "off", temperature=20, control_target_temperature=68, control_temperature_unit="°F"
        )
    )
    h.async_on_remove = lambda remove: None
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
        await h.async_added_to_hass()
    assert h.target_temperature == 20


@pytest.mark.parametrize("step", [float("nan"), float("inf"), 0, -1, 1e-320])
async def test_invalid_speed_steps_cannot_drive_heater(step):
    h, services = controller()
    h.hass.states._states["fan.heater_level"] = entity("off", percentage=0, percentage_step=step)
    assert not h.available
    await h._async_control()
    services.async_call.assert_not_awaited()


async def test_lost_feedback_during_burnoff_preserves_source_shutdown_gate():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["fan.heater_level"] = entity("unavailable")
    h.hass.states._states["sensor.room_temperature"] = entity("unavailable")
    await h._async_control()
    services.async_call.assert_not_awaited()


async def test_restored_heat_resumes_with_same_valid_target_and_live_feedback():
    h, services = controller()
    h.async_get_last_state = AsyncMock(
        return_value=entity(
            "heat", temperature=21, control_target_temperature=21, control_temperature_unit="°C"
        )
    )
    h.async_on_remove = lambda remove: None
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
        await h.async_added_to_hass()
    assert h.hvac_mode == HVACMode.HEAT
    assert h.target_temperature == 21
    assert services.async_call.await_args.args[1] == "turn_on"


async def test_heat_demand_restores_even_if_old_entity_state_was_unavailable():
    h, services = controller()
    h.async_get_last_state = AsyncMock(
        return_value=entity(
            "unavailable",
            control_hvac_mode="heat",
            temperature=21,
            control_target_temperature=21,
            control_temperature_unit="°C",
        )
    )
    h.async_on_remove = lambda remove: None
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
        await h.async_added_to_hass()
    assert h.hvac_mode == HVACMode.HEAT
    assert h.target_temperature == 21
    assert services.async_call.await_args.args[1] == "turn_on"


async def test_invalid_level_feedback_requests_shutdown_when_source_gate_is_live():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["fan.heater_level"] = entity("on", percentage=float("nan"))
    await h._async_control()
    services.async_call.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.heater_power"}, blocking=True
    )


async def test_queued_off_does_not_write_state_after_removal():
    h, services = controller()
    writes = []
    h.async_write_ha_state = lambda: writes.append(h._removed)
    started, release = asyncio.Event(), asyncio.Event()

    async def service(domain, action, data, blocking):
        started.set()
        await release.wait()

    services.async_call.side_effect = service
    control = asyncio.create_task(h._async_control())
    await started.wait()
    off = asyncio.create_task(h.async_turn_off())
    await asyncio.sleep(0)
    await h.async_will_remove_from_hass()
    await asyncio.gather(control, off, return_exceptions=True)
    await h.async_set_temperature(temperature=22)
    assert not any(writes)


async def test_mode_change_during_level_write_cannot_trigger_automatic_off():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.config["fault_sensor"] = "binary_sensor.fault"
    h.hass.states._states["binary_sensor.fault"] = entity("off")

    async def service(domain, action, data, blocking):
        if domain == "fan":
            h.hass.states._states["select.heater_mode"] = entity("Temperature Mode")
            h.hass.states._states["binary_sensor.fault"] = entity("on")

    services.async_call.side_effect = service
    await h._async_control()
    assert [call.args[:2] for call in services.async_call.await_args_list] == [
        ("fan", "set_percentage")
    ]


async def test_confirmed_on_clears_start_pending_while_native_mode_is_selected():
    h, services = controller(mode="Temperature Mode")
    h._power_on_pending = True
    h.hass.async_create_task = asyncio.create_task
    h.hass.states._states["switch.heater_power"] = entity("on")
    h._async_source_changed(None)
    await h._event_task
    assert not h._power_on_pending
    services.async_call.assert_not_awaited()
    h.hass.states._states["switch.heater_power"] = entity("off")
    h.hass.states._states["select.heater_mode"] = entity("Level Mode")
    await h._async_control()
    assert sum(call.args[1] == "turn_on" for call in services.async_call.await_args_list) == 1


@pytest.mark.parametrize("count", [6, 10])
@pytest.mark.parametrize("unit", ["°C", "°F"])
async def test_full_temperature_trace_levels_shutdown_restart_and_explicit_off(count, unit):
    from homeassistant.util.percentage import (
        ordered_list_item_to_percentage,
        percentage_to_ordered_list_item,
    )
    from homeassistant.util.unit_conversion import TemperatureConverter

    h, services = controller()
    h._attr_temperature_unit = unit
    h.hass.config.units.temperature_unit = unit
    h._target_temperature = TemperatureConverter.convert(20, "°C", unit)
    h.hass.states._states["fan.heater_level"] = entity(
        "off", percentage=0, percentage_step=100 / count
    )
    levels = [str(i) for i in range(1, count + 1)]

    async def service(domain, action, data, blocking):
        if domain == "fan":
            actual = percentage_to_ordered_list_item(levels, data["percentage"])
            h.hass.states._states["fan.heater_level"] = entity(
                "on",
                percentage=ordered_list_item_to_percentage(levels, actual),
                percentage_step=100 / count,
            )
        else:
            h.hass.states._states["switch.heater_power"] = entity(
                "on" if action == "turn_on" else "off"
            )

    services.async_call.side_effect = service

    def room(celsius):
        h.hass.states._states["sensor.room_temperature"] = entity(
            str(TemperatureConverter.convert(celsius, "°C", unit)), unit_of_measurement=unit
        )

    room(0)
    await h._async_control()
    assert h._source_level() == count and h._is_source_on()
    room(19.8)
    await h._async_control()
    assert h._source_level() == 1 and h._is_source_on()
    room(20)
    await h._async_control()
    assert not h._is_source_on()
    room(21)
    before = services.async_call.await_count
    await h._async_control()
    assert services.async_call.await_count == before
    room(19.9)
    await h._async_control()
    assert h._source_level() == 1 and h._is_source_on()
    await h.async_turn_off()
    assert not h._is_source_on()
    room(0)
    before = services.async_call.await_count
    await h._async_control()
    assert services.async_call.await_count == before
