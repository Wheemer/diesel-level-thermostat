"""Tests for source-device telemetry discovery."""

from unittest.mock import patch

from custom_components.diesel_level_thermostat.source import (
    source_control_entity_ids,
    source_device_id,
    source_telemetry_defaults,
    source_telemetry_entity_ids,
)
from tests.source_fixtures import source_hass, source_row


def test_source_device_requires_all_control_entities_on_one_device() -> None:
    """A partial or mixed control selection cannot create a source binding."""
    registry = source_row(
        async_get=lambda entity_id: {
            "switch.power": source_row(device_id="heater"),
            "fan.level": source_row(device_id="heater"),
            "select.mode": source_row(device_id="heater"),
            "select.other_mode": source_row(device_id="other"),
        }.get(entity_id)
    )
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get",
        return_value=registry,
    ):
        assert (
            source_device_id(source_hass(), ("switch.power", "fan.level", "select.mode"))
            == "heater"
        )
        assert (
            source_device_id(source_hass(), ("switch.power", "fan.level", "select.other_mode"))
            is None
        )
        assert (
            source_device_id(source_hass(), ("switch.power", "fan.level", "select.missing")) is None
        )


def test_source_telemetry_excludes_local_estimates() -> None:
    """Only source-device physical telemetry is surfaced by the climate."""
    registry = object()
    entries = (
        source_row(
            entity_id="sensor.diesel_heater_interior_temperature",
            domain="sensor",
            unique_id="device_interior_temperature",
        ),
        source_row(
            entity_id="sensor.diesel_heater_case_temperature",
            domain="sensor",
            unique_id="device_case_temperature",
        ),
        source_row(
            entity_id="sensor.diesel_heater_estimated_fuel_remaining",
            domain="sensor",
            unique_id="device_estimated_fuel_remaining",
        ),
        source_row(
            entity_id="sensor.diesel_heater_total_runtime",
            domain="sensor",
            unique_id="device_total_runtime",
        ),
        source_row(
            entity_id="sensor.since_refuel",
            domain="sensor",
            unique_id="device_est_fuel_since_refuel",
        ),
        source_row(
            entity_id="binary_sensor.diesel_heater_problem",
            domain="binary_sensor",
            unique_id="device_problem",
        ),
    )
    with (
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_entries_for_device",
            return_value=entries,
        ),
    ):
        assert source_telemetry_entity_ids(source_hass(), "heater") == (
            "sensor.diesel_heater_interior_temperature",
            "sensor.diesel_heater_case_temperature",
            "binary_sensor.diesel_heater_problem",
        )


def test_source_control_entities_require_the_level_mode_contract() -> None:
    """Only a heater device with all public staged-control entities is valid."""
    registry = object()
    entries = (
        source_row(
            entity_id="switch.diesel_heater_power",
            domain="switch",
            unique_id="aa_power",
        ),
        source_row(
            entity_id="fan.diesel_heater_heater_level",
            domain="fan",
            unique_id="aa_heater_level",
        ),
        source_row(
            entity_id="select.diesel_heater_running_mode",
            domain="select",
            unique_id="aa_running_mode",
        ),
    )
    with (
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_entries_for_device",
            return_value=entries,
        ),
    ):
        assert source_control_entity_ids(source_hass(), "heater") == {
            "heater_power": "switch.diesel_heater_power",
            "heater_level": "fan.diesel_heater_heater_level",
            "running_mode": "select.diesel_heater_running_mode",
        }


def test_source_control_entities_reject_cronus_style_devices_without_a_level_fan() -> None:
    """Cronus air and water devices do not expose the staged-control contract."""
    registry = object()
    entries = (
        source_row(
            entity_id="switch.diesel_heater_power",
            domain="switch",
            unique_id="aa_power",
        ),
        source_row(
            entity_id="select.diesel_heater_mode",
            domain="select",
            unique_id="aa_cronus_mode",
        ),
    )
    with (
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_entries_for_device",
            return_value=entries,
        ),
    ):
        assert source_control_entity_ids(source_hass(), "cronus") is None


def test_source_telemetry_defaults_follow_available_heater_probes() -> None:
    """Heater telemetry is populated from supported source entities."""
    registry = object()
    entries = (
        source_row(entity_id="switch.power", domain="switch", unique_id="aa_power"),
        source_row(entity_id="sensor.interior", domain="sensor", unique_id="aa_cab_temp"),
        source_row(entity_id="sensor.case", domain="sensor", unique_id="aa_case_temp"),
        source_row(
            entity_id="sensor.outlet",
            domain="sensor",
            unique_id="aa_outlet_temperature",
        ),
        source_row(entity_id="sensor.voltage", domain="sensor", unique_id="aa_supply_voltage"),
        source_row(
            entity_id="binary_sensor.problem",
            domain="binary_sensor",
            unique_id="aa_problem",
        ),
    )
    with (
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_get",
            return_value=registry,
        ),
        patch(
            "custom_components.diesel_level_thermostat.source.er.async_entries_for_device",
            return_value=entries,
        ),
    ):
        assert source_telemetry_defaults(source_hass(), "heater") == {
            "room_temperature": "sensor.interior",
            "fault_sensor": "binary_sensor.problem",
            "voltage_sensor": "sensor.voltage",
            "case_temperature": "sensor.case",
            "exhaust_temperature": "sensor.outlet",
        }
