"""Recover trusted bindings and ignore unrelated registry churn without timers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.diesel_level_thermostat import async_setup_entry
from custom_components.diesel_level_thermostat.config_flow import feedback_errors
from custom_components.diesel_level_thermostat.source import source_telemetry_defaults
from tests.source_fixtures import source_hass, source_row
from tests.test_climate import controller, entity
from tests.test_setup_and_bindings import entry, registry


def host():
    return source_hass(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_update_entry=MagicMock(
                side_effect=lambda e, **kw: [setattr(e, key, value) for key, value in kw.items()]
            ),
            async_reload=AsyncMock(),
        ),
        bus=SimpleNamespace(async_listen=MagicMock()),
    )


async def test_stale_device_id_recovers_only_from_surviving_registry_identities():
    e = entry()
    e.data["source_device_id"] = "obsolete"
    e.data["entity_registry_ids"].update(
        heater_power="power", heater_level="level", running_mode="mode"
    )
    h = host()
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
    ):
        assert await async_setup_entry(h, e)
    assert (
        h.config_entries.async_update_entry.call_args.kwargs["data"]["source_device_id"] == "source"
    )


async def test_stale_device_never_recovers_from_reused_control_names():
    e = entry()
    e.data.update(
        source_device_id="obsolete",
        heater_power="switch.new_power",
        heater_level="fan.new_level",
        running_mode="select.new_mode",
    )
    e.data["entity_registry_ids"].update(
        heater_power="gone-power", heater_level="gone-level", running_mode="gone-mode"
    )
    h = host()
    with patch(
        "custom_components.diesel_level_thermostat.source.er.async_get", return_value=registry()
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(h, e)
    h.config_entries.async_forward_entry_setups.assert_not_awaited()


async def test_registry_churn_is_ignored_and_relevant_reload_overlap_is_coalesced():
    e = entry()
    r = registry()
    h = host()
    started = asyncio.Event()
    release = asyncio.Event()

    async def reload(_):
        started.set()
        await release.wait()

    h.config_entries.async_reload.side_effect = reload
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=r):
        await async_setup_entry(h, e)
        handler = h.bus.async_listen.call_args.args[1]
        for i in range(10):
            name = "sensor.unrelated_" + str(i)
            r.entities[name] = source_row(
                entity_id=name,
                domain="sensor",
                unique_id="aa_probe_" + str(i),
                device_id="source",
                disabled_by=None,
            )
            await handler(SimpleNamespace(data={"action": "create", "entity_id": name}))
        await handler(SimpleNamespace(data={}))
        await handler(
            SimpleNamespace(
                data={
                    "action": "update",
                    "entity_id": "sensor.new_room",
                    "changes": {"name": "New name"},
                }
            )
        )
        h.config_entries.async_reload.assert_not_awaited()
        event = SimpleNamespace(
            data={
                "action": "update",
                "entity_id": "fan.new_level",
                "changes": {"disabled_by": "user"},
            }
        )
        first = asyncio.create_task(handler(event))
        await asyncio.wait_for(started.wait(), 2)
        await handler(event)
        release.set()
        await first
    h.config_entries.async_reload.assert_awaited_once()


@pytest.mark.parametrize(
    "state,attrs",
    [
        (None, {}),
        ("nan", {"unit_of_measurement": "°C"}),
        ("20", {}),
        ("20", {"unit_of_measurement": "%", "device_class": "humidity"}),
        ("unknown", {"unit_of_measurement": "°C"}),
    ],
)
def test_room_feedback_validation_rejects_missing_or_wrong_quantity(state, attrs):
    h = source_hass(
        states=SimpleNamespace(get=lambda _: None if state is None else entity(state, **attrs))
    )
    assert (
        feedback_errors({"room_temperature": "sensor.room"}, h)["room_temperature"]
        == "invalid_room_temperature"
    )


@pytest.mark.parametrize("unit", ["°C", "°F", "K"])
def test_valid_external_room_feedback_remains_supported(unit):
    h = source_hass(states=SimpleNamespace(get=lambda _: entity("20", unit_of_measurement=unit)))
    assert not feedback_errors({"room_temperature": "sensor.external"}, h)


def test_missing_voltage_binding_with_enabled_limit_blocks_heating():
    h, services = controller()
    h.config["min_voltage"] = 11.5
    assert h._safety_blocked()


def test_telemetry_defaults_cannot_borrow_another_heaters_sensor():
    r = registry()
    r.entities["sensor.new_room"].config_entry_id = "other"
    h = host()
    h.config_entries.async_get_entry = lambda _: SimpleNamespace(domain="diesel_heater")
    with patch("custom_components.diesel_level_thermostat.source.er.async_get", return_value=r):
        assert "room_temperature" not in source_telemetry_defaults(h, "source")
