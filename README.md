<div align="center">

<h1>
  <img src="https://raw.githubusercontent.com/Wheemer/diesel-level-thermostat/main/assets/diesel-level-thermostat-icon.png" width="56" alt="Diesel Level Thermostat icon" align="center">
  Diesel Level Thermostat
</h1>

### Staged Level-Mode climate control for diesel heaters in Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-CUSTOM-FD7E14?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://github.com/hacs/integration)
[![Home Assistant Custom Integration](https://img.shields.io/badge/HOME%20ASSISTANT-CUSTOM%20INTEGRATION-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/)
[![Latest release](https://img.shields.io/github/v/release/Wheemer/diesel-level-thermostat?style=for-the-badge&logo=github&logoColor=white&label=RELEASE&labelColor=555555&color=22C55E)](https://github.com/Wheemer/diesel-level-thermostat/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Wheemer/diesel-level-thermostat/total?style=for-the-badge&logo=github&logoColor=white&label=DOWNLOADS&labelColor=555555&color=8A2BE2)](https://github.com/Wheemer/diesel-level-thermostat/releases)
[![Climate platform](https://img.shields.io/badge/PLATFORM-CLIMATE-22C55E?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/integrations/climate/)

[Install](#install) • [What It Does](#what-it-does) • [Controller Coverage](#controller-coverage) • [Control Policy](#control-policy) • [Safety](#safety) • [Development](#development)

</div>

Diesel Level Thermostat is a companion Home Assistant custom integration for
[Diesel Heater](https://github.com/Spettacolo83/homeassistant-diesel-heater).
It adds a virtual thermostat on top of a heater already set up through that
integration; it does not communicate with the heater over BLE itself.

> [!IMPORTANT]
> This integration owns temperature control only while the heater is in
> **Level Mode**. It never sends the heater’s native Temperature Mode command.

## What It Does

| Capability | Behavior |
|---|---|
| **Level-mode climate control** | Calculates a heating level using the source fan’s supported speed steps. |
| **Corrected feedback** | Uses any Home Assistant temperature sensor and an optional local Celsius offset. |
| **Stable control** | Selects the calculated demand directly and reduces it as the room approaches target. |
| **Safety gates** | Stops only through the heater’s normal Power entity and honors configured fault, voltage, and thermal limits. |
| **No protocol assumptions** | Binds to Diesel Heater’s public entities, not a hard-coded brand or Bluetooth protocol. |

## What It Uses

The setup flow binds to an existing Diesel Heater device rather than talking to BLE itself:

- required: room temperature, power switch, heater-level fan, and running-mode
  entity;
- an automatically detected fault binary sensor, when provided by the source;
- physical telemetry attributes, including supply voltage, case temperature,
  and supported probes. Existing saved thermal/voltage limits are preserved;
  new setup does not add numerical safety tuning controls.

The first setup step chooses the **Diesel Heater** device. The integration then
derives its Power, Heater Level, Running Mode, interior temperature, fault,
supply voltage, case temperature, and any supported intake/outlet probe from
that device. The room-temperature field defaults to the heater's interior
sensor but can be changed to an external room sensor. The same selector and a
local Celsius offset remain editable from Options. Options changes reload the controller so the selected feedback takes effect.
Stable entity-registry references preserve selected sensors across renames.
Controls from different heaters cannot be mixed. Physical telemetry is published in the climate
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
contract: a Power switch, a discrete Heater Level fan, and a Running Mode
select on one device. The setup is telemetry-aware rather than
protocol-hardcoded, so it follows the actual entities exposed by each
controller, including six-step ABBA and ten-step source fans. That covers the established AA55/AA66, ABBA/HeaterCC,
CBFF/Sunster, Hcalory MVP, and HeatGenie/Boygu air-heater families when their
source entities expose that contract.

Webasto Cronus and DZ06 Neo are intentionally not selectable for staged-level
control: they do not expose this Heater Level contract. Use a
Diesel Heater release that hides the Heater Level fan for those controllers;
older source releases can expose a legacy fan entity that is not safe for this
thermostat to drive. Water controllers are likewise excluded because a
room-air level loop would be the wrong control model.

## Control Policy

While Heat is selected and the heater is in Level Mode, the controller compares
one room-temperature feedback sensor with the selected target. Below target,
it selects a level directly from the temperature gap using a fixed 5°C
proportional band and the source fan's actual speed count. A gap at or above
5°C selects maximum output; a small positive gap selects level 1. All control
math uses Celsius, including on Fahrenheit installations.

At or above target it requests the source's normal Power-off command. Increasing
or decreasing demand selects the newly calculated level immediately. There is
no one-step ramp, hold timer, overshoot band, periodic control timer, or native
heater calibration write. The only feedback options are the room sensor and
its local Celsius offset. Case, intake, outlet and exhaust probes are not
averaged into room temperature.

Control is suspended outside Level Mode. A source-owned burn-off remains in
charge while its level entity is unavailable. Explicit virtual Off requests
normal shutdown; it does not cut electrical power. Missing or invalid room
feedback also requests normal shutdown when Level-mode ownership and the
source's shutdown gate permit it.

The virtual Heat/Off state and target are restored after a restart, with the
target's unit preserved. An invalid or unresolvable out-of-range legacy target
leaves virtual heating Off. Pending controller tasks are cancelled on unload.

The source integration and heater firmware remain responsible for combustion,
altitude compensation, cooldown and hardware protections.

## Install

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wheemer&repository=diesel-level-thermostat&category=integration)

1. Install and configure
   [Diesel Heater](https://github.com/Spettacolo83/homeassistant-diesel-heater),
   then confirm its Power, Heater Level, and Running Mode entities work.
2. Install this repository as a custom HACS integration and restart Home
   Assistant.
3. Add **Diesel Level Thermostat** from Settings > Devices & services and choose
   the Diesel Heater device.
4. Choose the room-temperature sensor and, if needed, a local correction.
   These two feedback settings remain available in Options.
5. Put the heater in **Level Mode**, then use the new climate entity and adjust
   the room-feedback options as needed.

Do not run the virtual thermostat while the heater is in its native Temperature
Mode. The virtual climate entity is intentionally unavailable in that mode.

## Safety

Diesel heaters require a controlled cooldown. Use the existing heater Power
entity rather than cutting mains/DC supply. Verify normal startup, shutdown,
burn-off, and fault behavior with the heater attended before relying on an
automation.

## Development

Development and CI use Python 3.14 and Home Assistant 2026.10.4. Install the
pinned test tools with `python -m pip install -r requirements-test.txt`.
Run the controller, lifecycle, configuration and release-asset tests from the repository root:

```bash
python -m pytest tests
```

Run static checks before a release:

```bash
ruff check .
ruff format --check .
python -m compileall custom_components tests
```

Release packaging details are in the [release asset check documentation](docs/release-asset-check.md).
