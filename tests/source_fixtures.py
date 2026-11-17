"""Registry ownership fixtures with an existing Diesel Heater config entry."""

from types import SimpleNamespace


def source_hass(**kwargs):
    config_entries = kwargs.pop("config_entries", SimpleNamespace())
    config_entries.async_get_entry = lambda owner: (
        SimpleNamespace(domain="diesel_heater") if owner == "source-entry" else None
    )
    return SimpleNamespace(config_entries=config_entries, **kwargs)


def source_row(**kwargs):
    kwargs.setdefault("config_entry_id", "source-entry")
    kwargs.setdefault("platform", "diesel_heater")
    return SimpleNamespace(**kwargs)
