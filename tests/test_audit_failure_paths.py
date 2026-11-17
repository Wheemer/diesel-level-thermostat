"""Regression coverage for independently reproduced command/lifecycle failures."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from tests.test_climate import controller, entity


async def test_explicit_off_reports_block_and_recovers_on_source_event():
    h, services = controller()
    states = h.hass.states._states
    states["switch.heater_power"] = entity("on")
    states["fan.heater_level"] = entity("unavailable")
    with pytest.raises(HomeAssistantError, match="shutdown is pending"):
        await h.async_turn_off()
    assert h.hvac_mode == HVACMode.OFF
    assert h.capability_attributes["shutdown_pending"]
    assert h.capability_attributes["shutdown_block_reason"] == "source_level_unavailable"
    services.async_call.assert_not_awaited()
    h.hass.async_create_task = asyncio.create_task
    states["fan.heater_level"] = entity("on", percentage=40)
    h._async_source_changed(None)
    await h._event_task
    assert [c.args[:2] for c in services.async_call.await_args_list] == [("switch", "turn_off")]
    assert h._shutdown_pending  # A completed call is not an Off report.
    states["switch.heater_power"] = entity("off")
    h._async_source_changed(None)
    await h._event_task
    assert not h._shutdown_pending
    assert services.async_call.await_count == 1


async def test_event_during_failed_shutdown_is_drained_without_timer():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["sensor.room_temperature"] = entity("20", unit_of_measurement="°C")
    calls = 0

    async def send(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            h._control_dirty = True
            raise HomeAssistantError("temporary failure")
        h.hass.states._states["switch.heater_power"] = entity("off")

    services.async_call.side_effect = send
    h._control_dirty = True
    await h._async_event_control()
    assert calls == 2
    assert not h._control_dirty and not h._shutdown_pending
    assert h._last_control_error is None


async def test_persistent_failure_without_new_events_is_not_busy_retried():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["sensor.room_temperature"] = entity("20", unit_of_measurement="°C")
    services.async_call.side_effect = HomeAssistantError("still failing")
    h._control_dirty = True
    await h._async_event_control()
    for _ in range(5):
        await asyncio.sleep(0)
    assert services.async_call.await_count == 1
    assert h._last_control_error == "still failing"
    assert h._shutdown_pending


async def test_off_failure_remains_pending_but_never_restarts_heating():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    services.async_call.side_effect = HomeAssistantError("unreachable")
    with pytest.raises(HomeAssistantError, match="unreachable"):
        await h.async_turn_off()
    assert h.hvac_mode == HVACMode.OFF
    assert h.capability_attributes["shutdown_block_reason"] == "source_shutdown_failed"
    services.async_call.side_effect = None
    await h._async_control()
    assert all(c.args[:2] == ("switch", "turn_off") for c in services.async_call.await_args_list)


async def test_initial_failure_retains_subscriptions_and_cancellation_propagates():
    h, _ = controller()
    h.async_get_last_state = AsyncMock(return_value=None)
    h.async_on_remove = MagicMock()
    h._async_control = AsyncMock(side_effect=HomeAssistantError("source not ready"))
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
    assert h._last_control_error == "source not ready"
    h.async_on_remove.assert_called()
    h._async_control.side_effect = asyncio.CancelledError()
    h._control_dirty = True
    with pytest.raises(asyncio.CancelledError):
        await h._async_event_control()


async def test_real_ha_platform_keeps_entity_after_missing_startup_service(tmp_path):
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.setup import async_setup_component

    h, _ = controller()
    hass = HomeAssistant(str(tmp_path))
    h.hass = hass
    del h.async_write_ha_state
    h.entity_id = "climate.audit_failure"
    h._attr_device_info = None  # Core platform here; device registration has separate coverage.
    h.async_get_last_state = AsyncMock(return_value=None)
    from homeassistant import loader

    loader.async_setup(hass)
    from homeassistant.config_entries import ConfigEntries

    hass.config_entries = ConfigEntries(hass, {})
    dr.async_setup(hass)
    await dr.async_load(hass, load_empty=True)
    await er.async_load(hass, load_empty=True)
    assert await async_setup_component(hass, "climate", {})
    for name, value, attrs in [
        ("sensor.room_temperature", "18", {"unit_of_measurement": "°C"}),
        ("switch.heater_power", "off", {}),
        ("select.heater_mode", "Level", {}),
        ("fan.heater_level", "off", {"percentage": 10, "percentage_step": 10}),
    ]:
        hass.states.async_set(name, value, attrs)
    try:
        with patch(
            "custom_components.diesel_level_thermostat.climate.source_device_id", return_value=None
        ):
            await hass.data["climate"].async_add_entities([h])
        await hass.async_block_till_done()
        state = hass.states.get(h.entity_id)
        assert state is not None
        assert state.attributes["last_control_error"]
        commands = []

        async def level(call):
            commands.append("level")
            hass.states.async_set(
                "fan.heater_level",
                "off",
                {"percentage": call.data["percentage"], "percentage_step": 10},
            )

        async def power(call):
            commands.append("power")
            hass.states.async_set("switch.heater_power", "on")

        hass.services.async_register("fan", "set_percentage", level)
        hass.services.async_register("switch", "turn_on", power)
        hass.states.async_set("sensor.room_temperature", "17", {"unit_of_measurement": "°C"})
        await hass.async_block_till_done()
        assert commands == ["level", "power"]
        assert hass.states.get(h.entity_id).attributes["last_control_error"] is None
        assert hass.states.get("switch.heater_power").state == "on"
        await hass.data["climate"].async_remove_entity(h.entity_id)
    finally:
        await hass.async_stop()


async def test_pending_off_survives_restore_while_source_is_unavailable():
    h, _ = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["fan.heater_level"] = entity("unavailable")
    with pytest.raises(HomeAssistantError):
        await h.async_turn_off()
    h.entity_id = "climate.test"
    saved = h._async_calculate_state()
    from types import SimpleNamespace

    restored, services = controller()
    restored._hvac_mode = HVACMode.OFF
    restored.hass.states._states["switch.heater_power"] = entity("on")
    restored.hass.states._states["fan.heater_level"] = entity("unavailable")
    restored.async_get_last_state = AsyncMock(
        return_value=SimpleNamespace(state=saved.state, attributes=saved.attributes)
    )
    restored.async_on_remove = MagicMock()
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
    assert restored._shutdown_pending and restored.hvac_mode == HVACMode.OFF
    services.async_call.assert_not_awaited()
    restored.hass.states._states["fan.heater_level"] = entity("on", percentage=10)
    await restored._async_control()
    assert services.async_call.await_args.args[:2] == ("switch", "turn_off")


async def test_lost_power_feedback_during_shutdown_is_not_confirmation():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")

    async def send(*args, **kwargs):
        h.hass.states._states["switch.heater_power"] = entity("unavailable")

    services.async_call.side_effect = send
    await h.async_turn_off()
    assert h._shutdown_pending
    assert h.capability_attributes["unavailable_reason"] == "source_power_unavailable"


async def test_new_heat_request_cancels_old_off_demand_without_unlocking_source():
    h, services = controller()
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["fan.heater_level"] = entity("unavailable")
    with pytest.raises(HomeAssistantError):
        await h.async_turn_off()
    assert h._shutdown_pending
    await h.async_turn_on()
    assert h.hvac_mode == HVACMode.HEAT
    # Unavailable feedback can request a safe shutdown again, but no command
    # may pass the still-locked source, including any startup request.
    assert not h.available
    services.async_call.assert_not_awaited()
