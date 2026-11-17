"""Exercise source ownership and recoverable identity loss through real setup/flows."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.diesel_level_thermostat import async_setup_entry
from custom_components.diesel_level_thermostat.config_flow import DieselLevelThermostatOptionsFlow
from custom_components.diesel_level_thermostat.source import (
    entity_registry_ids,
    source_control_entity_ids,
)
from tests.source_fixtures import source_hass, source_row
from tests.test_setup_and_bindings import entry, registry


def setup_hass():
    def update(config_entry, **values):
        for key, value in values.items():
            setattr(config_entry, key, value)

    return source_hass(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_update_entry=MagicMock(side_effect=update),
        ),
        bus=SimpleNamespace(async_listen=MagicMock(return_value=lambda: None)),
    )


@pytest.mark.parametrize("owner", [None, "deleted", "foreign-domain"])
def test_number_requires_existing_diesel_owner(owner):
    reg = registry()
    reg.entities["fan.new_level"].disabled_by = "user"
    reg.entities["number.level"] = source_row(
        entity_id="number.level",
        domain="number",
        unique_id="aa_level",
        device_id="source",
        config_entry_id=owner,
    )
    hass = source_hass()
    hass.config_entries.async_get_entry = lambda key: (
        SimpleNamespace(domain="other" if key == "foreign-domain" else "diesel_heater")
        if key in ("source-entry", "foreign-domain")
        else None
    )
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        assert source_control_entity_ids(hass, "source") is None


@pytest.mark.parametrize("control", ["fan.new_level", "select.new_mode", "number.level"])
def test_controls_cannot_mix_existing_diesel_owners(control):
    reg = registry()
    if control == "number.level":
        reg.entities["fan.new_level"].disabled_by = "user"
        reg.entities[control] = source_row(
            entity_id=control,
            domain="number",
            unique_id="aa_level",
            device_id="source",
        )
    reg.entities[control].config_entry_id = "second"
    hass = source_hass()
    hass.config_entries.async_get_entry = lambda key: SimpleNamespace(domain="diesel_heater")
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        assert source_control_entity_ids(hass, "source") is None


@pytest.mark.parametrize("control", ["fan.new_level", "select.new_mode"])
def test_controls_cannot_mix_controller_stems(control):
    reg = registry()
    reg.entities[control].unique_id = reg.entities[control].unique_id.replace("aa_", "bb_")
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        assert source_control_entity_ids(source_hass(), "source") is None


async def test_unresolved_legacy_source_never_forwards_or_updates_entry():
    config_entry = entry()
    del config_entry.data["source_device_id"]
    hass = setup_hass()
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
    ):
        with pytest.raises(ConfigEntryNotReady, match="source identity"):
            await async_setup_entry(hass, config_entry)
    hass.config_entries.async_forward_entry_setups.assert_not_awaited()
    hass.config_entries.async_update_entry.assert_not_called()


async def test_legacy_source_without_registry_ids_migrates_by_valid_entities():
    reg = registry()
    config_entry = entry(
        {
            "heater_power": "switch.new_power",
            "heater_level": "fan.new_level",
            "running_mode": "select.new_mode",
            "room_temperature": "sensor.new_room",
        }
    )
    hass = setup_hass()
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        assert await async_setup_entry(hass, config_entry)
    assert config_entry.data["source_device_id"] == "source"
    assert config_entry.data["entity_registry_ids"]["room_temperature"] == "room"


@pytest.mark.parametrize(
    "key,domain",
    [
        ("room_temperature", "sensor"),
        ("fault_sensor", "binary_sensor"),
        ("voltage_sensor", "sensor"),
        ("case_temperature", "sensor"),
        ("exhaust_temperature", "sensor"),
    ],
)
async def test_deleted_identity_blocks_replacement_but_options_reselection_recovers(
    monkeypatch,
    key,
    domain,
):
    reg = registry()
    config_entry = entry()
    old_name = f"{domain}.reused"
    config_entry.data[key] = old_name
    config_entry.data["entity_registry_ids"][key] = "deleted"
    replacement = source_row(
        id="replacement",
        entity_id=old_name,
        domain=domain,
        unique_id="replacement",
        device_id="external",
    )
    reg.entities[old_name] = replacement
    hass = setup_hass()
    monkeypatch.setattr(
        DieselLevelThermostatOptionsFlow,
        "config_entry",
        property(lambda _: config_entry),
    )
    flow = DieselLevelThermostatOptionsFlow()
    flow.hass = hass
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        assert entity_registry_ids(hass, config_entry.data)[key] == "deleted"
        with pytest.raises(ConfigEntryNotReady, match="feedback entity identity"):
            await async_setup_entry(hass, config_entry)
        hass.config_entries.async_update_entry.assert_not_called()
        hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        form = await flow.async_step_init()
        assert key in {str(marker) for marker in form["data_schema"].schema}
        selected = {
            "room_temperature": old_name if key == "room_temperature" else "sensor.new_room",
            "room_temperature_offset": 0,
        }
        if key != "room_temperature":
            selected[key] = old_name
        result = await flow.async_step_init(selected)
        assert result["data"]["entity_registry_ids"][key] == "replacement"
        config_entry.options = result["data"]
        assert await async_setup_entry(hass, config_entry)
        assert config_entry.options["entity_registry_ids"][key] == "replacement"


async def test_changing_offset_does_not_rebind_unselected_missing_safety_sensor(monkeypatch):
    config_entry = entry()
    config_entry.data["fault_sensor"] = "binary_sensor.reused"
    config_entry.data["entity_registry_ids"]["fault_sensor"] = "deleted"
    reg = registry()
    reg.entities["binary_sensor.reused"] = source_row(
        id="replacement",
        entity_id="binary_sensor.reused",
    )
    monkeypatch.setattr(
        DieselLevelThermostatOptionsFlow,
        "config_entry",
        property(lambda _: config_entry),
    )
    flow = DieselLevelThermostatOptionsFlow()
    flow.hass = source_hass()
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        result = await flow.async_step_init(
            {
                "room_temperature": "sensor.new_room",
                "room_temperature_offset": -1,
            }
        )
    assert result["type"] == "form"
    assert result["errors"]["fault_sensor"] == "invalid_sensor"
    assert "fault_sensor" in {str(marker) for marker in result["data_schema"].schema}
    assert config_entry.data["entity_registry_ids"]["fault_sensor"] == "deleted"


async def test_legacy_source_cannot_infer_device_from_reused_control_names():
    reg = registry()
    config_entry = entry(
        {
            "heater_power": "switch.new_power",
            "heater_level": "fan.new_level",
            "running_mode": "select.new_mode",
            "room_temperature": "sensor.new_room",
            "entity_registry_ids": {
                "heater_power": "deleted-power",
                "heater_level": "deleted-level",
                "running_mode": "deleted-mode",
            },
        }
    )
    hass = setup_hass()
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        with pytest.raises(ConfigEntryNotReady, match="source identity"):
            await async_setup_entry(hass, config_entry)
    hass.config_entries.async_forward_entry_setups.assert_not_awaited()
    hass.config_entries.async_update_entry.assert_not_called()


async def test_normal_options_hide_resolved_optional_sensors(monkeypatch):
    config_entry = entry()
    config_entry.data["fault_sensor"] = "sensor.new_room"
    config_entry.data["entity_registry_ids"]["fault_sensor"] = "room"
    monkeypatch.setattr(
        DieselLevelThermostatOptionsFlow,
        "config_entry",
        property(lambda _: config_entry),
    )
    flow = DieselLevelThermostatOptionsFlow()
    flow.hass = source_hass()
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
    ):
        form = await flow.async_step_init()
    assert {str(marker) for marker in form["data_schema"].schema} == {
        "room_temperature",
        "room_temperature_offset",
    }


async def test_deleted_room_has_no_replacement_preselected_in_options(monkeypatch):
    config_entry = entry()
    config_entry.options = {"room_temperature": "sensor.reused"}
    config_entry.data["entity_registry_ids"]["room_temperature"] = "deleted"
    reg = registry()
    reg.entities["sensor.reused"] = source_row(id="replacement", entity_id="sensor.reused")
    monkeypatch.setattr(
        DieselLevelThermostatOptionsFlow,
        "config_entry",
        property(lambda _: config_entry),
    )
    flow = DieselLevelThermostatOptionsFlow()
    flow.hass = source_hass()
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        form = await flow.async_step_init()
    marker = next(k for k in form["data_schema"].schema if str(k) == "room_temperature")
    assert marker.default() is None
    assert not marker.description or "suggested_value" not in marker.description


async def test_repair_schema_survives_invalid_offset_and_keeps_limits(monkeypatch):
    config_entry = entry()
    config_entry.data.update(fault_sensor="binary_sensor.deleted", min_voltage=11.5)
    config_entry.data["entity_registry_ids"]["fault_sensor"] = "deleted"
    reg = registry()
    reg.entities["binary_sensor.new"] = source_row(id="new-fault", entity_id="binary_sensor.new")
    monkeypatch.setattr(
        DieselLevelThermostatOptionsFlow,
        "config_entry",
        property(lambda _: config_entry),
    )
    flow = DieselLevelThermostatOptionsFlow()
    flow.hass = source_hass()
    selected = {
        "room_temperature": "sensor.new_room",
        "room_temperature_offset": 99,
        "fault_sensor": "binary_sensor.new",
    }
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=reg):
        form = await flow.async_step_init(selected)
        assert form["errors"]["room_temperature_offset"] == "invalid_offset"
        marker = next(k for k in form["data_schema"].schema if str(k) == "fault_sensor")
        assert marker.default() is None
        selected["room_temperature_offset"] = 0
        result = await flow.async_step_init(selected)
    assert result["data"]["entity_registry_ids"]["fault_sensor"] == "new-fault"
    assert config_entry.data["min_voltage"] == 11.5
