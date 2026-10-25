"""Tests for the direct level-selection calculation."""

from custom_components.diesel_level_thermostat.control import target_level


def test_target_level_uses_minimum_at_target() -> None:
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
    assert (
        target_level(
            current_temperature=14,
            target_temperature=20,
            min_level=1,
            max_level=10,
            proportional_band=5,
        )
        == 10
    )


def test_target_level_selects_an_intermediate_level() -> None:
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
