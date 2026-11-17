"""The thermostat is listed as its own integration and virtual service."""

import json
from pathlib import Path

from homeassistant.helpers.device_registry import DeviceEntryType

from custom_components.diesel_level_thermostat.climate import DieselLevelThermostat
from custom_components.diesel_level_thermostat.const import DOMAIN
from tests.test_climate import controller


def test_manifest_uses_integrations_dashboard_type():
    manifest = json.loads(
        (Path(__file__).parents[1] / "custom_components" / DOMAIN / "manifest.json").read_text()
    )
    # subscribeConfigEntries on HA's Integrations dashboard excludes helpers.
    assert manifest["integration_type"] == "service"
    assert manifest["config_flow"] is True
    assert manifest["domain"] == DOMAIN


def test_thermostat_owns_service_device_and_preserves_entity_identity():
    thermostat, _ = controller()
    assert thermostat.unique_id == "test_thermostat"
    assert thermostat.device_info["identifiers"] == {(DOMAIN, "test")}
    assert thermostat.device_info["entry_type"] == DeviceEntryType.SERVICE
    assert thermostat.device_info["name"] == thermostat.entry.title
    assert "via_device" not in thermostat.device_info
    assert thermostat.name is None  # HA uses the service device name, without duplication.
    assert thermostat.available


def test_multiple_thermostats_have_separate_service_devices():
    first, _ = controller()
    first.entry.entry_id = "second"
    second = DieselLevelThermostat(first.hass, first.entry)
    assert first.device_info["identifiers"] != second.device_info["identifiers"]
    assert second.device_info["identifiers"] == {(DOMAIN, "second")}
