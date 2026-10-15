"""Tests for pure staged control calculations."""

from custom_components.diesel_level_thermostat.control import (
    should_start,
    should_stop,
    target_level,
)


def test_target_level_uses_minimum_at_target() -> None:
    """At or above target, retain the lowest stable level."""
    assert (
        target_level(
            current_temperature=20,
            target_temperature=20,
            min_level=1,
            max_level=10,
            proportional_band=5,
        )
        == 1
    )


def test_target_level_scales_to_maximum() -> None:
    """A large temperature error requests maximum output."""
    assert (
        target_level(
            current_temperature=14,
            target_temperature=20,
            min_level=2,
            max_level=8,
            proportional_band=5,
        )
        == 8
    )


def test_target_level_scales_discretely() -> None:
    """A partial error selects a discrete intermediate output."""
    assert (
        target_level(
            current_temperature=18,
            target_temperature=20,
            min_level=1,
            max_level=10,
            proportional_band=5,
        )
        == 5
    )


def test_hysteresis_boundaries() -> None:
    """Start and stop boundaries do not overlap."""
    assert should_start(
        current_temperature=19.4, target_temperature=20, cold_tolerance=0.5
    )
    assert not should_start(
        current_temperature=19.5, target_temperature=20, cold_tolerance=0.5
    )
    assert should_stop(
        current_temperature=20.5, target_temperature=20, hot_tolerance=0.5
    )
    assert not should_stop(
        current_temperature=20.4, target_temperature=20, hot_tolerance=0.5
    )
