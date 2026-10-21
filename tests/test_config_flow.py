"""Tests for Diesel Level Thermostat configuration schemas."""

from homeassistant.helpers import selector

from custom_components.diesel_level_thermostat.config_flow import (
    advanced_options_schema,
    options_schema,
)


def _number_selectors(schema):
    """Return the numeric selectors from a voluptuous schema."""
    return [
        value
        for value in schema.schema.values()
        if isinstance(value, selector.NumberSelector)
    ]


def test_room_feedback_schema_exposes_sensor_and_boxed_offset() -> None:
    """The primary options keep the temperature source obvious and editable."""
    values = list(options_schema("sensor.room_temperature").schema.values())

    assert any(isinstance(value, selector.EntitySelector) for value in values)

    assert [
        number.config["mode"]
        for number in _number_selectors(options_schema("sensor.room_temperature"))
    ] == ["box"]


def test_advanced_options_use_boxed_number_selectors() -> None:
    """Advanced tuning avoids a wall of slider controls."""
    numbers = _number_selectors(advanced_options_schema())

    assert len(numbers) == 11
    assert all(number.config["mode"] == "box" for number in numbers)
