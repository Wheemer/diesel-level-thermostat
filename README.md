<div align="center">

<h1>
  <img src="https://raw.githubusercontent.com/Wheemer/diesel-level-thermostat/main/assets/diesel-level-thermostat-icon.png" width="56" alt="Diesel Level Thermostat icon" align="center">
  Diesel Level Thermostat
</h1>

### Staged Level-Mode climate control for diesel heaters in Home Assistant

[![HACS Custom Integration](https://img.shields.io/badge/HACS-Custom%20Integration-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](#installation)
[![Latest release](https://img.shields.io/github/v/release/Wheemer/diesel-level-thermostat?style=for-the-badge&logo=github&logoColor=white&label=RELEASE&labelColor=555555&color=22C55E)](https://github.com/Wheemer/diesel-level-thermostat/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Wheemer/diesel-level-thermostat/total?style=for-the-badge&logo=github&logoColor=white&label=DOWNLOADS&labelColor=555555&color=8A2BE2)](https://github.com/Wheemer/diesel-level-thermostat/releases)
[![Climate platform](https://img.shields.io/badge/PLATFORM-CLIMATE-22C55E?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/integrations/climate/)

[Installation](#installation) • [Configuration](#configuration) • [Controller reference](docs/REFERENCE.md)



</div>

A companion integration for [Diesel Heater](https://github.com/Spettacolo83/homeassistant-diesel-heater)
that controls room temperature by adjusting the heater’s output in **Level Mode**.
It adds a separate climate entity under its own integration and device.

## Features

- 🌡️ Adjusts heating levels as the room approaches your target temperature.
- 🔄 Requests normal shutdown and startup using an optional temperature hysteresis.
- 📡 Uses the heater’s temperature sensor or an external room sensor.
- ⚙️ Offers an editable room sensor, local temperature offset, and hysteresis.
- 🛡️ Respects the source integration’s burn-off locks and normal shutdown controls.

## Installation

Requires a working [Diesel Heater](https://github.com/Spettacolo83/homeassistant-diesel-heater)
installation with Power, Running Mode, and a compatible Heater Level fan or Level number.

### HACS

[![Add this repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wheemer&repository=diesel-level-thermostat&category=integration)

1. Add `https://github.com/Wheemer/diesel-level-thermostat` as a custom repository
   with category **Integration**.
2. Download **Diesel Level Thermostat** and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**.
4. Select **Diesel Level Thermostat** and choose your heater and room sensor.

## Configuration

Put the heater in **Level Mode**, then select **Heat** and a target temperature
on the new thermostat. It selects the output from the temperature gap, reducing
levels toward the target. With the default 0°C hysteresis it requests normal shutdown at target; selecting
**Off** disables automatic heating. The heater remains responsible for cooldown.

Change the room sensor, offset, and hysteresis during setup or through configuration options.
The offset is in Celsius and affects only this thermostat’s feedback. Hysteresis defaults
to 0°C; see the [control reference](docs/REFERENCE.md#control-policy) for switching thresholds.

The heater’s native Temperature Mode remains under the original integration.
This thermostat is unavailable outside Level Mode.

## Compatibility

Works with heaters supported by the [Diesel Heater integration](https://github.com/Spettacolo83/homeassistant-diesel-heater)
that provide Level-mode heating control.

See the [control reference](docs/REFERENCE.md) for detailed behavior and troubleshooting.

Use normal heater shutdown rather than cutting electrical power. Check startup
and shutdown with the heater attended before relying on automatic control.

For development information, see [CONTRIBUTING.md](CONTRIBUTING.md).
