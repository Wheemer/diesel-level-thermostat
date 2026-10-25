"""Constants for Diesel Level Thermostat."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "diesel_level_thermostat"

CONF_ROOM_TEMPERATURE: Final = "room_temperature"
CONF_ROOM_TEMPERATURE_OFFSET: Final = "room_temperature_offset"
CONF_SOURCE_DEVICE_ID: Final = "source_device_id"
CONF_HEATER_POWER: Final = "heater_power"
CONF_HEATER_LEVEL: Final = "heater_level"
CONF_RUNNING_MODE: Final = "running_mode"
CONF_LEVEL_MODE_VALUE: Final = "level_mode_value"
CONF_FAULT_SENSOR: Final = "fault_sensor"
CONF_VOLTAGE_SENSOR: Final = "voltage_sensor"
CONF_MIN_VOLTAGE: Final = "min_voltage"
CONF_CASE_TEMPERATURE: Final = "case_temperature"
CONF_MAX_CASE_TEMPERATURE: Final = "max_case_temperature"
CONF_EXHAUST_TEMPERATURE: Final = "exhaust_temperature"
CONF_MAX_EXHAUST_TEMPERATURE: Final = "max_exhaust_temperature"

DEFAULT_LEVEL_MODE_VALUE: Final = "Level"
DEFAULT_ROOM_TEMPERATURE_OFFSET: Final = 0.0
LEVEL_MODE_VALUES: Final = frozenset({"Level", "Level Mode"})
DEFAULT_MIN_LEVEL: Final = 1
DEFAULT_MAX_LEVEL: Final = 10
DEFAULT_PROPORTIONAL_BAND: Final = 5.0
# Re-evaluate an active heating cycle even if the source emits no state event.
CONTROL_RECHECK_SECONDS: Final = 60
DISABLED_THRESHOLD: Final = 0.0

OPTIONAL_ENTITY_KEYS: Final = (
    CONF_FAULT_SENSOR,
    CONF_VOLTAGE_SENSOR,
    CONF_CASE_TEMPERATURE,
    CONF_EXHAUST_TEMPERATURE,
)
