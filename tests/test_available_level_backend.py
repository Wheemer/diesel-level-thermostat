"""Exercise validated alternate controls and public-service safety races."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.diesel_level_thermostat import async_setup_entry
from custom_components.diesel_level_thermostat.source import source_control_entity_ids
from tests.source_fixtures import source_hass, source_row
from tests.test_climate import controller, entity


@pytest.fixture
def source():
    h, services = controller(mode="Level")
    h.config["source_device_id"] = h._source_device_id = "source"
    rows = {}
    for entity_id, suffix in (
        ("switch.heater_power", "power"),
        ("select.heater_mode", "running_mode"),
        ("fan.heater_level", "heater_level"),
        ("number.level", "level"),
        ("binary_sensor.burnoff", "burnoff_active"),
    ):
        rows[entity_id] = source_row(
            id=entity_id,
            entity_id=entity_id,
            domain=entity_id.split(".")[0],
            unique_id=f"aa_{suffix}",
            device_id="source",
            disabled_by=None,
        )

    class Entries(dict):
        def get_entries_for_device_id(self, device_id, include_disabled_entities=False):
            return [
                row
                for row in self.values()
                if row.device_id == device_id and (include_disabled_entities or not row.disabled_by)
            ]

    rows = Entries(rows)
    registry = SimpleNamespace(entities=rows, async_get=rows.get)
    h.hass.config_entries = source_hass().config_entries
    states = h.hass.states._states
    states["fan.heater_level"] = entity("unavailable")
    states["number.level"] = entity("1", min=1, max=10, step=1)
    states["binary_sensor.burnoff"] = entity("off")
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry
    ):
        yield h, services, rows


async def test_available_number_drives_level_and_power_without_raw_calls(source):
    h, services, _ = source
    assert h.available
    assert h._source_level_count() == 10
    assert h._source_level() == 1
    await h._async_control()
    assert [c.args for c in services.async_call.await_args_list] == [
        ("number", "set_value", {"entity_id": "number.level", "value": 4}),
        ("switch", "turn_on", {"entity_id": "switch.heater_power"}),
    ]
    assert h.capability_attributes["source_level"] == "number.level"


def test_new_setup_selects_available_number_and_reload_keeps_it(source):
    h, _, _ = source
    controls = source_control_entity_ids(h.hass, "source")
    assert controls["heater_level"] == "number.level"
    h.hass.states._states["fan.heater_level"] = entity("on", percentage=10, percentage_step=10)
    assert (
        source_control_entity_ids(
            h.hass,
            "source",
            preferred_level="number.level",
        )["heater_level"]
        == "number.level"
    )


async def test_actual_setup_persists_currently_available_control(source):
    h, _, _ = source
    config_entry = SimpleNamespace(
        data=dict(h.config),
        options={},
        entry_id="test",
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(),
    )
    ce = h.hass.config_entries
    ce.async_forward_entry_setups = AsyncMock()
    ce.async_update_entry = MagicMock(
        side_effect=lambda e, **kw: [setattr(e, k, v) for k, v in kw.items()]
    )
    h.hass.bus = SimpleNamespace(async_listen=MagicMock())
    assert await async_setup_entry(h.hass, config_entry)
    assert config_entry.data["heater_level"] == "number.level"
    assert config_entry.data["entity_registry_ids"]["heater_level"] == "number.level"


async def test_selected_backend_stays_usable_then_falls_back_to_fan(source):
    h, services, _ = source
    assert h.available  # selects Number
    h.hass.states._states["fan.heater_level"] = entity("on", percentage=10, percentage_step=10)
    assert h._level_entity_id() == "number.level"
    h.hass.states._states["number.level"] = entity("unavailable")
    assert h.available
    await h._async_control()
    assert services.async_call.await_args_list[0].args[:2] == ("fan", "set_percentage")


@pytest.mark.parametrize("count", [6, 10])
async def test_number_count_and_normal_shutdown_use_effective_backend(source, count):
    h, services, _ = source
    states = h.hass.states._states
    states["number.level"] = entity("1", min=1, max=count, step=1)
    states["sensor.room_temperature"] = entity("0", unit_of_measurement="°C")
    await h._async_control()
    assert services.async_call.await_args_list[0].args[2]["value"] == count
    states["switch.heater_power"] = entity("on")
    states["sensor.room_temperature"] = entity("20", unit_of_measurement="°C")
    await h._async_control()
    assert services.async_call.await_args.args[:2] == ("switch", "turn_off")


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_both_unavailable_never_write_or_interfere_with_shutdown(source, state):
    h, services, _ = source
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["number.level"] = entity(state)
    h.entity_id = "climate.test"
    result = h._async_calculate_state()
    assert result.state == "unavailable"
    assert (
        result.attributes["source_candidate_states"]["fan.heater_level"]["state"] == "unavailable"
    )
    assert result.attributes["source_candidate_states"]["number.level"]["state"] == state
    await h._async_control()
    with pytest.raises(HomeAssistantError, match="shutdown is pending"):
        await h.async_turn_off()
    services.async_call.assert_not_awaited()


@pytest.mark.parametrize(
    "mode", ["Temperature", "Ventilation", "Heating", "unknown", "unavailable"]
)
async def test_number_availability_never_replaces_explicit_level_mode(source, mode):
    h, services, _ = source
    h.hass.states._states["select.heater_mode"] = entity(mode)
    assert not h.available
    await h._async_control()
    services.async_call.assert_not_awaited()


@pytest.mark.parametrize("guard", ["on", "unavailable", "unknown", None])
async def test_burnoff_guard_blocks_stale_available_alternate(source, guard):
    h, services, _ = source
    if guard is None:
        h.hass.states._states.pop("binary_sensor.burnoff")
    else:
        h.hass.states._states["binary_sensor.burnoff"] = entity(guard)
    h.hass.states._states["switch.heater_power"] = entity("on")
    assert not h.available
    await h._async_control()
    with pytest.raises(HomeAssistantError, match="shutdown is pending"):
        await h.async_turn_off()
    services.async_call.assert_not_awaited()


async def test_older_source_without_burnoff_sensor_uses_own_public_availability(source):
    h, services, rows = source
    del rows["binary_sensor.burnoff"]
    assert h.available
    await h._async_control()
    assert services.async_call.await_args_list[0].args[:2] == ("number", "set_value")


@pytest.mark.parametrize(
    "change",
    [
        {"config_entry_id": "other"},
        {"platform": "other"},
        {"device_id": "other"},
        {"unique_id": "bb_level"},
        {"disabled_by": "user"},
        {"orphaned_timestamp": "old"},
    ],
)
async def test_foreign_disabled_or_orphan_alternate_never_controls(source, change):
    h, services, rows = source
    for key, value in change.items():
        setattr(rows["number.level"], key, value)
    h.hass.config_entries.async_get_entry = lambda _: SimpleNamespace(domain="diesel_heater")
    assert not h.available
    await h._async_control()
    services.async_call.assert_not_awaited()


@pytest.mark.parametrize(
    "value,attributes",
    [
        ("1", {"min": 0, "max": 10, "step": 1}),
        ("1", {"min": 1, "max": 11, "step": 1}),
        ("1", {"min": 1, "max": 6.5, "step": 1}),
        ("1", {"min": 1, "max": 10, "step": 0.5}),
        ("nan", {"min": 1, "max": 10, "step": 1}),
        ("inf", {"min": 1, "max": 10, "step": 1}),
        ("1.5", {"min": 1, "max": 10, "step": 1}),
        ("11", {"min": 1, "max": 10, "step": 1}),
        ("1", {}),
    ],
)
async def test_invalid_number_is_not_a_usable_alternate(source, value, attributes):
    h, services, _ = source
    h.hass.states._states["number.level"] = entity(value, **attributes)
    assert not h.available
    await h._async_control()
    services.async_call.assert_not_awaited()


@pytest.mark.parametrize(
    "mutation",
    [
        "burnoff",
        "mode",
        "unavailable",
        "range",
        "owner",
        "identity",
        "backend",
        "room",
        "off",
        "remove",
    ],
)
async def test_changes_during_number_await_never_power_on(source, mutation):
    h, services, rows = source
    started, release = asyncio.Event(), asyncio.Event()

    async def call(domain, action, data, blocking):
        if domain == "number":
            started.set()
            await release.wait()

    services.async_call.side_effect = call
    task = asyncio.create_task(h._async_control())
    await started.wait()
    states = h.hass.states._states
    if mutation == "burnoff":
        states["binary_sensor.burnoff"] = entity("on")
    elif mutation == "mode":
        states["select.heater_mode"] = entity("Temperature")
    elif mutation == "unavailable":
        states["number.level"] = entity("unavailable")
    elif mutation == "range":
        states["number.level"] = entity("1", min=1, max=6, step=1)
    elif mutation == "owner":
        rows["number.level"].config_entry_id = "other"
    elif mutation == "identity":
        rows["number.level"].id = "replacement"
    elif mutation == "backend":
        states["number.level"] = entity("unavailable")
        states["fan.heater_level"] = entity("on", percentage=10, percentage_step=10)
    elif mutation == "room":
        states["sensor.room_temperature"] = entity("unavailable")
    elif mutation == "off":
        h._hvac_mode = HVACMode.OFF
    elif mutation == "remove":
        h._removed = True
    release.set()
    await task
    assert len(services.async_call.await_args_list) == 1
    assert services.async_call.await_args.args[:2] == ("number", "set_value")


async def test_burnoff_during_number_await_never_sends_shutdown_even_if_room_lost(source):
    h, services, _ = source
    states = h.hass.states._states
    states["switch.heater_power"] = entity("on")

    async def call(domain, action, data, blocking):
        if domain == "number":
            states["binary_sensor.burnoff"] = entity("on")
            states["sensor.room_temperature"] = entity("unavailable")

    services.async_call.side_effect = call
    await h._async_control()
    assert [c.args[:2] for c in services.async_call.await_args_list] == [("number", "set_value")]


async def test_startup_subscribes_to_absent_candidate_state_and_resumes_on_arrival(source):
    h, services, _ = source
    states = h.hass.states._states
    del states["number.level"]
    h.async_get_last_state = AsyncMock(return_value=None)
    h.async_on_remove = MagicMock()
    h.hass.async_create_task = asyncio.create_task
    callback = None

    def track(hass, watched, handler):
        nonlocal callback
        assert "fan.heater_level" in watched
        assert "number.level" in watched
        assert "binary_sensor.burnoff" in watched
        callback = handler
        return lambda: None

    with patch(
        "custom_components.diesel_level_thermostat.climate.async_track_state_change_event",
        side_effect=track,
    ):
        await h.async_added_to_hass()
    services.async_call.assert_not_awaited()
    states["number.level"] = entity("1", min=1, max=10, step=1)
    callback(SimpleNamespace())
    await h._event_task
    assert services.async_call.await_args_list[0].args[:2] == ("number", "set_value")
    assert services.async_call.await_args_list[1].args[:2] == ("switch", "turn_on")


async def test_pending_off_waits_for_burnoff_and_valid_public_control(source):
    h, services, _ = source
    states = h.hass.states._states
    h.hass.async_create_task = asyncio.create_task
    states["switch.heater_power"] = entity("on")
    states["binary_sensor.burnoff"] = entity("on")
    with pytest.raises(HomeAssistantError, match="shutdown is pending"):
        await h.async_turn_off()
    assert h.capability_attributes["shutdown_pending"]
    h._async_source_changed(None)
    await h._event_task
    services.async_call.assert_not_awaited()
    states["binary_sensor.burnoff"] = entity("off")
    states["number.level"] = entity("unavailable")
    h._async_source_changed(None)
    await h._event_task
    services.async_call.assert_not_awaited()
    states["number.level"] = entity("1", min=1, max=10, step=1)
    h._async_source_changed(None)
    await h._event_task
    assert [c.args[:2] for c in services.async_call.await_args_list] == [("switch", "turn_off")]
    assert h.hvac_mode == HVACMode.OFF


async def test_removal_never_dispatches_deferred_off(source):
    h, services, _ = source
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["binary_sensor.burnoff"] = entity("on")
    with pytest.raises(HomeAssistantError):
        await h.async_turn_off()
    await h.async_will_remove_from_hass()
    h.hass.states._states["binary_sensor.burnoff"] = entity("off")
    await h._async_control()
    services.async_call.assert_not_awaited()


async def test_disappearing_previously_seen_burnoff_guard_cannot_unlock_controls(source):
    h, services, rows = source
    h.hass.states._states["switch.heater_power"] = entity("on")
    h.hass.states._states["binary_sensor.burnoff"] = entity("on")
    assert not h.available
    del rows["binary_sensor.burnoff"]
    assert not h.available
    await h._async_control()
    with pytest.raises(HomeAssistantError):
        await h.async_turn_off()
    services.async_call.assert_not_awaited()


async def test_disabled_known_guard_cannot_use_stale_off_state(source):
    h, services, rows = source
    assert h.available
    rows["binary_sensor.burnoff"].disabled_by = "user"
    assert not h.available
    await h._async_control()
    services.async_call.assert_not_awaited()


async def test_known_guard_requirement_survives_reload_and_recovers_when_guard_returns(source):
    from custom_components.diesel_level_thermostat.climate import DieselLevelThermostat

    h, services, rows = source
    config_entry = SimpleNamespace(
        data=dict(h.config),
        options={},
        entry_id="test",
        title="Test",
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(),
    )
    ce = h.hass.config_entries
    ce.async_forward_entry_setups = AsyncMock()
    ce.async_update_entry = MagicMock(
        side_effect=lambda e, **kw: [setattr(e, k, v) for k, v in kw.items()]
    )
    h.hass.bus = SimpleNamespace(async_listen=MagicMock())
    await async_setup_entry(h.hass, config_entry)
    assert config_entry.data["source_burnoff_guard_required"] is True
    guard = rows.pop("binary_sensor.burnoff")
    h.hass.states._states["switch.heater_power"] = entity("on")
    restored = DieselLevelThermostat(h.hass, config_entry)
    restored.async_write_ha_state = lambda: None
    assert not restored.available
    with pytest.raises(HomeAssistantError):
        await restored.async_turn_off()
    services.async_call.assert_not_awaited()
    rows[guard.entity_id] = guard
    assert restored.available
    await restored._async_control()
    assert services.async_call.await_args.args[:2] == ("switch", "turn_off")


@pytest.mark.parametrize("change", ["fault", "temperature", "burnoff", "native_mode"])
async def test_changes_during_power_on_respect_latest_safety_and_ownership(source, change):
    h, services, _ = source
    states = h.hass.states._states
    h.config["fault_sensor"] = "binary_sensor.problem"
    states["binary_sensor.problem"] = entity("off")

    async def send(domain, action, data, blocking):
        if domain == "switch" and action == "turn_on":
            states["switch.heater_power"] = entity("on")
            if change == "fault":
                states["binary_sensor.problem"] = entity("on")
            elif change == "temperature":
                states["sensor.room_temperature"] = entity("20", unit_of_measurement="°C")
            elif change == "burnoff":
                states["binary_sensor.burnoff"] = entity("on")
                states["sensor.room_temperature"] = entity("20", unit_of_measurement="°C")
            else:
                states["select.heater_mode"] = entity("Temperature")

    services.async_call.side_effect = send
    await h._async_control()
    calls = [c.args[:2] for c in services.async_call.await_args_list]
    assert calls[:2] == [("number", "set_value"), ("switch", "turn_on")]
    if change in {"fault", "temperature"}:
        assert calls[2:] == [("switch", "turn_off")]
    else:
        assert len(calls) == 2  # Never interrupt burn-off or commandeer native mode.
