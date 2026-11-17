"""Source acceptance follows real Level controls rather than one entity domain."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.diesel_level_thermostat import async_setup_entry
from custom_components.diesel_level_thermostat.const import CONF_HEATER_LEVEL
from custom_components.diesel_level_thermostat.source import source_control_entity_ids
from tests.source_fixtures import source_hass
from tests.test_climate import controller, entity


def bindings(*, fan=True, number=True, preferred=None, fuel_only=False, extra=()):
    entries = [
        SimpleNamespace(entity_id="switch.power", domain="switch", unique_id="aa_power"),
        SimpleNamespace(entity_id="select.mode", domain="select", unique_id="aa_running_mode"),
    ]
    if fan:
        entries.append(
            SimpleNamespace(entity_id="fan.level", domain="fan", unique_id="aa_heater_level")
        )
    else:
        entries.append(
            SimpleNamespace(
                entity_id="fan.level", domain="fan", unique_id="aa_heater_level", disabled_by="user"
            )
        )
    if number:
        entries.append(
            SimpleNamespace(
                entity_id="number.level",
                domain="number",
                unique_id="aa_current_fuel_level" if fuel_only else "aa_level",
            )
        )
    for row in entries:
        row.config_entry_id = "source-entry"
        row.platform = "diesel_heater"
    entries.extend(extra)
    for row in extra:
        if not hasattr(row, "platform"):
            row.platform = "diesel_heater"
    with (
        patch("custom_components.diesel_level_thermostat.source.er.async_get"),
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_entries_for_device",
            return_value=entries,
        ),
    ):
        return source_control_entity_ids(source_hass(), "source", preferred_level=preferred)


def test_usable_level_number_is_not_rejected_when_fan_disabled():
    assert bindings(fan=False)[CONF_HEATER_LEVEL] == "number.level"


def test_fan_is_preferred_for_new_bindings():
    assert bindings()[CONF_HEATER_LEVEL] == "fan.level"


def test_existing_number_binding_survives_reload_when_fan_enabled():
    assert bindings(preferred="number.level")[CONF_HEATER_LEVEL] == "number.level"


def test_fuel_level_cannot_be_mistaken_for_heater_level():
    assert bindings(fan=False, fuel_only=True) is None


def test_missing_both_real_level_controls_remains_unsupported():
    assert bindings(fan=False, number=False) is None


@pytest.mark.parametrize("count", [6, 10])
@pytest.mark.asyncio
async def test_number_control_scales_then_stops_at_target(count):
    h, services = controller()
    h.config[CONF_HEATER_LEVEL] = "number.level"
    states = h.hass.states._states
    states["number.level"] = entity("1", min=1, max=count, step=1)
    states["sensor.room_temperature"] = entity("0", unit_of_measurement="°C")
    assert h.available
    await h._async_control()
    first = services.async_call.await_args_list[0].args
    assert first == ("number", "set_value", {"entity_id": "number.level", "value": count})
    states["switch.heater_power"] = entity("on")
    states["number.level"] = entity(str(count), min=1, max=count, step=1)
    states["sensor.room_temperature"] = entity("19.8", unit_of_measurement="°C")
    await h._async_control()
    assert any(
        call.args == ("number", "set_value", {"entity_id": "number.level", "value": 1})
        for call in services.async_call.await_args_list
    )
    states["sensor.room_temperature"] = entity("20", unit_of_measurement="°C")
    await h._async_control()
    assert services.async_call.await_args.args == (
        "switch",
        "turn_off",
        {"entity_id": "switch.heater_power"},
    )


@pytest.mark.parametrize("value", ["unavailable", "unknown", "nan", "11", "1.5"])
@pytest.mark.asyncio
async def test_invalid_number_feedback_never_starts_heater(value):
    h, services = controller()
    h.config[CONF_HEATER_LEVEL] = "number.level"
    h.hass.states._states["number.level"] = entity(value, min=1, max=10, step=1)
    assert not h.available
    await h._async_control()
    services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_number_source_burnoff_unavailability_still_blocks_control():
    h, services = controller()
    h.config[CONF_HEATER_LEVEL] = "number.level"
    h.hass.states._states["number.level"] = entity("unavailable", min=1, max=10, step=1)
    h.hass.states._states["switch.heater_power"] = entity("on")
    await h._async_control()
    services.async_call.assert_not_awaited()
    assert h.extra_state_attributes["unavailable_reason"] == "source_level_unavailable"


@pytest.mark.parametrize(
    "key,state,reason",
    [
        ("switch.heater_power", entity("unavailable"), "source_power_unavailable"),
        ("select.heater_mode", entity("Temperature"), "source_mode_not_level"),
        ("fan.heater_level", entity("unavailable"), "source_level_unavailable"),
        ("fan.heater_level", entity("off", percentage_step=0), "source_level_range_invalid"),
        ("sensor.room_temperature", entity("unavailable"), "room_temperature_invalid"),
    ],
)
def test_missing_input_is_explained_in_entity_attributes(key, state, reason):
    h, _ = controller()
    h.hass.states._states[key] = state
    assert not h.available
    assert h.extra_state_attributes["unavailable_reason"] == reason
    assert h.extra_state_attributes["source_room_temperature"] == "sensor.room_temperature"


@pytest.mark.parametrize(
    "ownership",
    [
        {"orphaned_timestamp": "2026-11-17", "config_entry_id": "old"},
        {"config_entry_id": None},
    ],
)
def test_orphan_registry_row_does_not_hide_valid_heater(ownership):
    orphan = SimpleNamespace(
        entity_id="fan.orphan", domain="fan", unique_id="old_heater_level", **ownership
    )
    assert bindings(extra=[orphan])[CONF_HEATER_LEVEL] == "fan.level"


def test_genuinely_ambiguous_active_controls_are_not_chosen_arbitrarily():
    other = SimpleNamespace(
        entity_id="fan.other",
        domain="fan",
        unique_id="other_heater_level",
        config_entry_id="source-entry",
    )
    assert bindings(extra=[other]) is None


@pytest.mark.asyncio
async def test_invalid_source_setup_cannot_keep_old_unvalidated_bindings():
    from tests.test_setup_and_bindings import entry, registry

    e = entry()
    reg = registry()
    reg.entities["fan.new_level"].disabled_by = "user"
    h = source_hass(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(), async_update_entry=MagicMock()
        )
    )
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(h, e)
    h.config_entries.async_forward_entry_setups.assert_not_awaited()
    h.config_entries.async_update_entry.assert_not_called()


@pytest.mark.parametrize("percentage,step", [(150, 10), (-1, 10), (10, 30)])
@pytest.mark.asyncio
async def test_invalid_fan_metadata_never_starts_heater(percentage, step):
    h, services = controller()
    h.hass.states._states["fan.heater_level"] = entity(
        "off", percentage=percentage, percentage_step=step
    )
    assert not h.available
    await h._async_control()
    services.async_call.assert_not_awaited()
