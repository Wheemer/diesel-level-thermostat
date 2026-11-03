"""Actual flow/setup methods apply options and preserve stable entity bindings."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.diesel_level_thermostat import async_setup_entry, async_unload_entry
from custom_components.diesel_level_thermostat.config_flow import (
    ConfigFlow,
    DieselLevelThermostatOptionsFlow,
)
from custom_components.diesel_level_thermostat.source import (
    entity_registry_ids,
    resolve_entity_references,
    source_telemetry_entity_ids,
)


def registry():
    class Entries(dict):
        def get_entries_for_device_id(self, device_id, include_disabled_entities=False):
            return [entry for entry in self.values() if entry.device_id == device_id]

    entries = Entries()
    for key, domain, suffix in [
        ("power", "switch", "power"),
        ("level", "fan", "heater_level"),
        ("mode", "select", "running_mode"),
        ("room", "sensor", "cab_temp"),
    ]:
        entry = SimpleNamespace(
            id=key,
            entity_id=f"{domain}.new_{key}",
            domain=domain,
            platform="diesel_heater",
            unique_id=f"aa_{suffix}",
            device_id="source",
            disabled_by=None,
        )
        entries[entry.entity_id] = entry
    return SimpleNamespace(entities=entries, async_get=entries.get)


def entry(data=None):
    return SimpleNamespace(
        entry_id="test",
        data=data
        or {
            "source_device_id": "source",
            "heater_power": "switch.old_power",
            "heater_level": "fan.old_level",
            "running_mode": "select.old_mode",
            "room_temperature": "sensor.old_room",
            "room_temperature_offset": -5,
            "entity_registry_ids": {"room_temperature": "room"},
        },
        options={},
        async_on_unload=MagicMock(),
        add_update_listener=MagicMock(return_value=lambda: None),
    )


async def test_setup_rebinds_renamed_controls_and_room_then_reloads_on_options():
    e = entry()
    h = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(), async_reload=AsyncMock()
        ),
        bus=SimpleNamespace(async_listen=MagicMock(return_value=lambda: None)),
    )

    def update(updated, **values):
        for key, value in values.items():
            setattr(updated, key, value)

    h.config_entries.async_update_entry = MagicMock(side_effect=update)
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
    ):
        assert await async_setup_entry(h, e)
    assert e.data["heater_power"] == "switch.new_power"
    assert e.data["room_temperature"] == "sensor.new_room"
    assert e.data["room_temperature_offset"] == -5
    listener = e.add_update_listener.call_args.args[0]
    e.options = {"room_temperature": "sensor.external", "room_temperature_offset": -2}
    await listener(h, e)
    h.config_entries.async_reload.assert_awaited_once_with("test")
    h.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    assert await async_unload_entry(h, e)


async def test_registry_rename_requests_reload_only_for_selected_device():
    e = entry()
    reg = registry()
    h = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_reload=AsyncMock(),
            async_update_entry=MagicMock(),
        ),
        bus=SimpleNamespace(async_listen=MagicMock(return_value=lambda: None)),
    )
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        await async_setup_entry(h, e)
        handler = h.bus.async_listen.call_args.args[1]
        await handler(
            SimpleNamespace(
                data={
                    "action": "update",
                    "entity_id": "sensor.new_room",
                    "changes": {"entity_id": "sensor.old_room"},
                }
            )
        )
        h.config_entries.async_reload.assert_awaited_once_with("test")
        h.config_entries.async_reload.reset_mock()
        await handler(SimpleNamespace(data={"action": "create", "entity_id": "sensor.unrelated"}))
        h.config_entries.async_reload.assert_not_awaited()


async def test_options_form_preserves_offset_from_initial_setup(monkeypatch):
    e = entry()
    monkeypatch.setattr(DieselLevelThermostatOptionsFlow, "config_entry", property(lambda _: e))
    flow = DieselLevelThermostatOptionsFlow()
    result = await flow.async_step_init()
    schema = result["data_schema"]
    marker = next(k for k in schema.schema if str(k) == "room_temperature_offset")
    assert marker.default() == -5


async def test_options_rebind_external_sensor_by_registry_id(monkeypatch):
    e = entry()
    reg = registry()
    external = SimpleNamespace(id="external-id", entity_id="sensor.external")
    reg.entities[external.entity_id] = external
    monkeypatch.setattr(DieselLevelThermostatOptionsFlow, "config_entry", property(lambda _: e))
    flow = DieselLevelThermostatOptionsFlow()
    flow.hass = object()
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        result = await flow.async_step_init(
            {"room_temperature": "sensor.external", "room_temperature_offset": -2}
        )
    assert result["data"]["entity_registry_ids"]["room_temperature"] == "external-id"
    assert result["data"]["room_temperature_offset"] == -2


async def test_complete_user_flow_autopopulates_and_persists_registry_bindings():
    flow = ConfigFlow()
    flow.hass = object()
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    with (
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
        ),
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_entries_for_device",
            side_effect=lambda r, d: list(r.entities.values()),
        ),
    ):
        result = await flow.async_step_user({"source_device_id": "source"})
        assert result["step_id"] == "telemetry"
        result = await flow.async_step_telemetry(
            {
                "name": "Greenhouse",
                "room_temperature": "sensor.new_room",
                "room_temperature_offset": -5,
            }
        )
    assert result["data"]["heater_power"] == "switch.new_power"
    assert result["data"]["entity_registry_ids"]["room_temperature"] == "room"


def test_renamed_estimates_do_not_become_physical_telemetry():
    reg = registry()
    estimate = SimpleNamespace(
        id="estimate",
        entity_id="sensor.arbitrary_name",
        domain="sensor",
        unique_id="aa_fuel_remaining",
        platform="diesel_heater",
        disabled_by=None,
    )
    physical = SimpleNamespace(
        id="physical",
        entity_id="sensor.total_fuel_custom_label",
        domain="sensor",
        unique_id="aa_case_temp",
        platform="diesel_heater",
        disabled_by=None,
    )
    reg.entities.update({e.entity_id: e for e in [estimate, physical]})
    with (
        patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg),
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_entries_for_device",
            return_value=list(reg.entities.values()),
        ),
    ):
        ids = source_telemetry_entity_ids(object(), "source")
    assert estimate.entity_id not in ids
    assert physical.entity_id in ids


def test_external_registry_id_resolves_new_name():
    reg = registry()
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        config = {"room_temperature": "sensor.new_room"}
        config["entity_registry_ids"] = entity_registry_ids(object(), config)
        reg.entities["sensor.new_room"].entity_id = "sensor.renamed"
        assert resolve_entity_references(object(), config)["room_temperature"] == "sensor.renamed"


@pytest.mark.parametrize("offset", [float("nan"), float("inf"), -21, 21, "invalid"])
async def test_options_reject_invalid_offset_without_saving(monkeypatch, offset):
    e = entry()
    monkeypatch.setattr(DieselLevelThermostatOptionsFlow, "config_entry", property(lambda _: e))
    flow = DieselLevelThermostatOptionsFlow()
    result = await flow.async_step_init(
        {"room_temperature": "sensor.room", "room_temperature_offset": offset}
    )
    assert result["type"] == "form"
    assert result["errors"]["room_temperature_offset"] == "invalid_offset"
