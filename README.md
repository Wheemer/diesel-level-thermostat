<div align="center">

<img src="https://raw.githubusercontent.com/Wheemer/diesel-level-thermostat/main/assets/diesel-level-thermostat-icon.svg" width="104" alt="Diesel Level Thermostat icon">

<h1>
  Diesel Level Thermostat
</h1>

### Staged Level-Mode climate control for diesel heaters in Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-CUSTOM-FD7E14?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://github.com/hacs/integration)
[![Home Assistant Custom Integration](https://img.shields.io/badge/HOME%20ASSISTANT-CUSTOM%20INTEGRATION-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/)
[![Latest release](https://img.shields.io/github/v/release/Wheemer/diesel-level-thermostat?style=for-the-badge&logo=github&logoColor=white&label=RELEASE&labelColor=555555&color=22C55E)](https://github.com/Wheemer/diesel-level-thermostat/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Wheemer/diesel-level-thermostat/total?style=for-the-badge&logo=github&logoColor=white&label=DOWNLOADS&labelColor=555555&color=8A2BE2)](https://github.com/Wheemer/diesel-level-thermostat/releases)
[![Climate platform](https://img.shields.io/badge/PLATFORM-CLIMATE-22C55E?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/integrations/climate/)

[Install](#install) - [What It Uses](#what-it-uses) - [Controller Coverage](#controller-coverage) - [Control Policy](#control-policy) - [Safety](#safety) - [Development](#development)

</div>

Diesel Level Thermostat is a companion Home Assistant custom integration for
[Diesel Heater](https://github.com/Spettacolo83/homeassistant-diesel-heater).
It adds a virtual thermostat on top of a heater already set up through that
integration; it does not communicate with the heater over BLE itself.

It deliberately controls the heater only in **Level Mode**. It does not send
the heater's native temperature-mode command. This keeps exactly one control
policy active at a time:

- **Level Mode:** this integration calculates and sends Levels 1 through 10.
- **Temperature Mode:** the heater's native controller remains in charge and
  this virtual thermostat becomes unavailable.

## What It Uses

The setup flow binds to an existing Diesel Heater device rather than talking to BLE itself:

- required: room temperature, power switch, heater-level fan, and running-mode
  entity;
- optional safety inputs: a fault binary sensor, supply voltage, case
  temperature, and an outlet/exhaust temperature sensor when the selected
  controller exposes one.

The first setup step chooses the **Diesel Heater** device. The integration then
derives its Power, Heater Level, Running Mode, interior temperature, fault,
supply voltage, case temperature, and any supported intake/outlet probe from
that device. The room-temperature field defaults to the heater's interior
sensor but can be changed to an external room sensor. Controls from different
heaters cannot be mixed. Physical telemetry is published in the climate
attributes; integration-local fuel and runtime estimates are excluded because
they are not hardware measurements and never affect control.

The underlying heater integration remains responsible for BLE transport,
protocol support, burn-off, and its own hardware protections. In particular,
the source level fan is unavailable during burn-off; that availability is a
hard gate for this controller.

Altitude is intentionally not fed into the control calculation. It is a
heater-side combustion configuration, not a changing measurement for room
temperature regulation. It remains visible through the source integration.

## Controller Coverage

This thermostat supports every source heater that exposes the public Level-Mode
contract: a Power switch, a ten-step Heater Level fan, and a Running Mode
select on one device. The setup is telemetry-aware rather than
protocol-hardcoded, so it follows the actual entities exposed by each
controller. That covers the established AA55/AA66, ABBA/HeaterCC,
CBFF/Sunster, Hcalory MVP, and HeatGenie/Boygu air-heater families when their
source entities expose that contract.

Webasto Cronus and DZ06 Neo are intentionally not selectable for staged-level
control: they do not expose the same ten-step Heater Level contract. Use a
Diesel Heater release that hides the Heater Level fan for those controllers;
older source releases can expose a legacy fan entity that is not safe for this
thermostat to drive. Water controllers are likewise excluded because a
room-air level loop would be the wrong control model.

## Control Policy

While heating is requested, the controller maps the temperature error into a
discrete level between the configured minimum and maximum. It selects the
calculated level immediately when starting or when demand increases, then
steps a running heater down one level at a time as demand falls. One configured
room temperature is the feedback input; it is never averaged with case, intake,
outlet, or exhaust probes because they measure different physical locations.

The controller:

- uses separate start and stop tolerances;
- schedules re-evaluation at exact minimum-on and minimum-off boundaries;
- serializes source commands;
- rate-limits level changes; and
- stops the source through its normal Power entity when an enabled safety input
  is active.

The default level-change interval is 60 seconds. Set it to `0` only to disable descent pacing.

The controller never writes the heater's native temperature mode.

The defaults are conservative starting points, not universal combustion
settings. Tune them for the installed heater and room. This integration is not
a replacement for the heater's firmware safety systems.

## Install

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wheemer&repository=diesel-level-thermostat&category=integration)

1. Install and configure
   [Diesel Heater](https://github.com/Spettacolo83/homeassistant-diesel-heater),
   then confirm its Power, Heater Level, and Running Mode entities work.
2. Install this repository as a custom HACS integration and restart Home
   Assistant.
3. Add **Diesel Level Thermostat** from Settings > Devices & services and choose
   the Diesel Heater device.
4. Choose the room-temperature sensor and, if needed, a local correction. The
   source device's physical telemetry is discovered automatically. Both choices
   remain editable from the thermostat's Options.
5. Put the heater in **Level Mode**, then use the new climate entity and adjust
   the controller options as needed.

Do not run the virtual thermostat while the heater is in its native Temperature
Mode. The virtual climate entity is intentionally unavailable in that mode.

## Safety

Diesel heaters require a controlled cooldown. Use the existing heater Power
entity rather than cutting mains/DC supply. Verify normal startup, shutdown,
burn-off, and fault behavior with the heater attended before relying on an
automation.

## Development

Run the focused controller test suite from the repository root:

```bash
python -m pytest tests
```

Run static checks before a release:

```bash
ruff check .
ruff format --check .
python -m compileall custom_components tests
```
