"""Exercise diagnostics through Home Assistant's actual entity state calculation."""

import pytest
from homeassistant.const import STATE_UNAVAILABLE

from tests.test_climate import controller, entity


def test_unavailable_state_retains_binding_diagnostics():
    thermostat, services = controller()
    thermostat.entity_id = "climate.test"
    thermostat.hass.states._states["fan.heater_level"] = entity("unavailable")
    result = thermostat._async_calculate_state()
    assert result.state == STATE_UNAVAILABLE
    assert result.attributes["unavailable_reason"] == "source_level_unavailable"
    assert result.attributes["source_level"] == "fan.heater_level"
    assert result.attributes["source_states"]["source_level"]["state"] == "unavailable"
    assert result.attributes["hvac_modes"] == ["heat", "off"]
    services.async_call.assert_not_called()


@pytest.mark.parametrize(
    "source,state,reason",
    [
        ("switch.heater_power", entity("unavailable"), "source_power_unavailable"),
        ("select.heater_mode", entity("Temperature"), "source_mode_not_level"),
        ("fan.heater_level", entity("off", percentage_step=0), "source_level_range_invalid"),
        ("sensor.room_temperature", entity("unknown"), "room_temperature_invalid"),
    ],
)
def test_each_failed_gate_is_visible_in_actual_ha_state(source, state, reason):
    thermostat, _ = controller()
    thermostat.entity_id = "climate.test"
    thermostat.hass.states._states[source] = state
    result = thermostat._async_calculate_state()
    assert result.state == STATE_UNAVAILABLE
    assert result.attributes["unavailable_reason"] == reason


def test_ready_state_preserves_climate_capabilities_and_clears_reason():
    thermostat, _ = controller()
    thermostat.entity_id = "climate.test"
    result = thermostat._async_calculate_state()
    assert result.state == "heat"
    assert result.attributes["unavailable_reason"] is None
    assert result.attributes["temperature"] == 20
    assert result.attributes["current_temperature"] == 18
