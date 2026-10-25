"""Tests for the simple room-feedback options schema."""

from homeassistant.helpers import selector

from custom_components.diesel_level_thermostat.config_flow import options_schema


def test_room_feedback_schema_exposes_sensor_and_boxed_offset() -> None:
    """Options expose only the feedback sensor and its local correction."""
    values = list(options_schema("sensor.room_temperature").schema.values())
    assert sum(isinstance(value, selector.EntitySelector) for value in values) == 1
    numbers = [value for value in values if isinstance(value, selector.NumberSelector)]
    assert len(numbers) == 1
    assert numbers[0].config["mode"] == "box"
