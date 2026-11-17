"""Safety repair is optional to submit but cannot redirect the heater's protections."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.diesel_level_thermostat.config_flow import DieselLevelThermostatOptionsFlow
from tests.source_fixtures import source_hass, source_row
from tests.test_climate import entity
from tests.test_setup_and_bindings import entry, registry


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "motion",
        "foreign_device",
        "foreign_owner",
        "foreign_platform",
        "wrong_stem",
        "disabled",
        "orphan",
        "no_state",
    ],
)
async def test_fault_repair_rejects_unsafe_replacement(monkeypatch, mutation):
    e = entry()
    e.data["fault_sensor"] = "binary_sensor.deleted"
    e.data["entity_registry_ids"]["fault_sensor"] = "deleted"
    r = registry()
    candidate = source_row(
        id="new-fault",
        entity_id="binary_sensor.replacement",
        domain="binary_sensor",
        unique_id="aa_problem",
        device_id="source",
        disabled_by=None,
    )
    if mutation != "missing":
        r.entities[candidate.entity_id] = candidate
    if mutation == "foreign_device":
        candidate.device_id = "other"
    if mutation == "foreign_owner":
        candidate.config_entry_id = "other"
    if mutation == "foreign_platform":
        candidate.platform = "other"
    if mutation == "wrong_stem":
        candidate.unique_id = "bb_problem"
    if mutation == "disabled":
        candidate.disabled_by = "user"
    if mutation == "orphan":
        candidate.orphaned_timestamp = "old"
    monkeypatch.setattr(DieselLevelThermostatOptionsFlow, "config_entry", property(lambda _: e))
    f = DieselLevelThermostatOptionsFlow()
    f.hass = source_hass(
        states=SimpleNamespace(
            get=lambda _: (
                None
                if mutation == "no_state"
                else entity("off", device_class="motion" if mutation == "motion" else "problem")
            )
        )
    )
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=r):
        result = await f.async_step_init(
            {
                "room_temperature": "sensor.new_room",
                "room_temperature_offset": 0,
                "fault_sensor": candidate.entity_id,
            }
        )
    assert result["type"] == "form"
    assert result["errors"]["fault_sensor"] == "invalid_safety_sensor"
    assert e.data["entity_registry_ids"]["fault_sensor"] == "deleted"


@pytest.mark.parametrize(
    "key,unit,kind",
    [
        ("voltage_sensor", "°C", "temperature"),
        ("case_temperature", "%", "humidity"),
        ("exhaust_temperature", "V", "voltage"),
    ],
)
async def test_sensor_repair_rejects_wrong_quantity(monkeypatch, key, unit, kind):
    e = entry()
    e.data[key] = "sensor.deleted"
    e.data["entity_registry_ids"][key] = "deleted"
    r = registry()
    suffix = {
        "voltage_sensor": "supply_voltage",
        "case_temperature": "case_temp",
        "exhaust_temperature": "outlet_temperature",
    }[key]
    r.entities["sensor.replacement"] = source_row(
        id="new",
        entity_id="sensor.replacement",
        domain="sensor",
        unique_id="aa_" + suffix,
        device_id="source",
        disabled_by=None,
    )
    monkeypatch.setattr(DieselLevelThermostatOptionsFlow, "config_entry", property(lambda _: e))
    f = DieselLevelThermostatOptionsFlow()
    f.hass = source_hass(
        states=SimpleNamespace(
            get=lambda _: entity("12", unit_of_measurement=unit, device_class=kind)
        )
    )
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=r):
        result = await f.async_step_init(
            {
                "room_temperature": "sensor.new_room",
                "room_temperature_offset": 0,
                key: "sensor.replacement",
            }
        )
    assert result["errors"][key] == "invalid_safety_sensor"
