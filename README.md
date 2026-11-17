<div align="center">

<h1>
  <img src="https://raw.githubusercontent.com/Wheemer/diesel-level-thermostat/main/assets/diesel-level-thermostat-icon.png" width="56" alt="Diesel Level Thermostat icon" align="center">
  Diesel Level Thermostat
</h1>

### Staged Level-Mode climate control for diesel heaters in Home Assistant

[![HACS Custom Integration](https://img.shields.io/badge/HACS-Custom%20Integration-41BDF5?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](#install)
[![Latest release](https://img.shields.io/github/v/release/Wheemer/diesel-level-thermostat?style=for-the-badge&logo=github&logoColor=white&label=RELEASE&labelColor=555555&color=22C55E)](https://github.com/Wheemer/diesel-level-thermostat/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/Wheemer/diesel-level-thermostat/total?style=for-the-badge&logo=github&logoColor=white&label=DOWNLOADS&labelColor=555555&color=8A2BE2)](https://github.com/Wheemer/diesel-level-thermostat/releases)
[![Climate platform](https://img.shields.io/badge/PLATFORM-CLIMATE-22C55E?style=for-the-badge&logo=home-assistant&logoColor=white&labelColor=555555)](https://www.home-assistant.io/integrations/climate/)

[Install](#install) • [What It Does](#what-it-does) • [Controller Coverage](#controller-coverage) • [Control Policy](#control-policy) • [Troubleshooting](#troubleshooting) • [Safety](#safety) • [Contributing](CONTRIBUTING.md)

</div>

Diesel Level Thermostat is a companion Home Assistant custom integration for
[Diesel Heater](https://github.com/Spettacolo83/homeassistant-diesel-heater). It
adds a virtual thermostat on top of a heater already set up through that
integration; it does not communicate with the heater over BLE itself.

> [!IMPORTANT]
> This integration owns temperature control only while the heater is in
> **Level Mode**. It never sends the heater’s native Temperature Mode command.

## What It Does

| Capability | Behavior |
|---|---|
| **Level-mode climate control** | Calculates a heating level using the selected level control’s supported steps. |
| **Corrected feedback** | Uses any Home Assistant temperature sensor and an optional local Celsius offset. |
| **Stable control** | Selects the calculated demand directly and reduces it as the room approaches target. |
| **Safety gates** | Stops only through the heater’s normal Power entity and honors configured fault, voltage, and thermal limits. |
| **No protocol assumptions** | Binds to Diesel Heater’s public entities, not a hard-coded brand or Bluetooth protocol. |

## What It Uses

The setup flow binds to an existing Diesel Heater device rather than talking to
BLE itself:

- required: room temperature, power switch, running-mode selector, and either
  a compatible Heater Level fan or Level number;
- an automatically detected fault binary sensor, when provided by the source;
- physical telemetry attributes, including supply voltage, case temperature,
  and other supported physical sensors. Existing saved thermal/voltage limits are preserved;
  new setup does not add numerical safety tuning controls. A selected fault
  sensor blocks heating when it reports a fault or is unavailable.

The first setup step chooses the **Diesel Heater** device. The integration then
derives its Power, Heater Level, Running Mode, interior temperature, fault,
supply voltage, case temperature, and supported outlet temperature from that
device. Other enabled physical sensor readings are included as telemetry
attributes, not additional room-control inputs. The room-temperature field
defaults to the heater's interior sensor but can be changed to an external room
sensor. The same selector and a local Celsius offset remain editable from
Options. Use a negative offset when the sensor reads too high. This local
correction does not change the sensor or write calibration to the heater. Saving
options reloads the thermostat. Stable entity-registry references preserve
selected sensors across renames. Controls from different heaters cannot be
mixed. Physical telemetry is published in the climate attributes;
integration-local fuel and runtime estimates are excluded because they are not
hardware measurements and never affect control.

The underlying heater integration remains responsible for BLE transport,
protocol support, burn-off, and its own hardware protections. In particular,
source level controls are locked during burn-off when supported by the source;
selecting an alternate control never bypasses that protection.

Altitude is intentionally not fed into the control calculation. It is a
heater-side combustion configuration, not a changing measurement for room
temperature regulation. It remains visible through the source integration.

## Controller Coverage

This room-air thermostat accepts source heaters that expose the public
Level-Mode contract: a Power switch, a Running Mode select with Level mode, and
either a discrete Heater Level fan or an enabled Level number on the same
device. Levels must start at 1 and provide up to 10 discrete stages. A Level
number must use a step of 1. The fan is preferred when both controls are usable
during setup. An existing usable binding is preserved on reload. If the selected
control is unavailable or invalid, the thermostat can use the other enabled
control only when it belongs to the same heater, source integration entry, and
controller identity and passes its own availability and level checks. The
selected control stays in use while it remains valid. Selection follows the
capabilities exposed by each controller, including six-stage ABBA and ten-stage
level controls. That covers the established AA55/AA66, ABBA/HeaterCC,
CBFF/Sunster, Hcalory MVP, and HeatGenie/Boygu air-heater families when their
source entities expose that contract.

Warm Drive **air** controllers using the split DZ06 transport are supported when
their source integration exposes working Level controls. Source releases with
the pump-frequency-as-level bug need the upstream Warm Drive correction before
their feedback can be used reliably.

Generic Neo controllers currently expose no public staged-level output. Webasto
Cronus exposes numbered speeds for ventilation, not heating stages; its
Heating/Boost/Eco modes are not Level mode. Those controls cannot be used as
interchangeable heat levels. This thermostat does not provide a
water-temperature control model. Acceptance follows the available control
capabilities, rather than a heater-brand blacklist.

Disabled controls are not silently enabled. Orphan registry rows are ignored,
but genuinely ambiguous active controls require correction. If a configured
source loses its required controls, setup reports the missing/disabled source
rather than retaining unvalidated bindings. The thermostat's attributes include
`unavailable_reason`, both candidate states, and the selected source entity IDs
so a missing input is visible without guessing. Source-owned burn-off
unavailability remains a hard gate. An enabled source Burn-off Active sensor
also blocks control while active or unavailable, even if a level entity has
stale available state. Backend selection never bypasses these protections. Both
unavailable level controls leave the thermostat unavailable; it sends no level
or startup command. Saved feedback identities are never silently replaced by
another entity reusing the old name. If a selected sensor was deleted, open the
integration's options and explicitly select its replacement. Safety-input repair
fields appear there only when a saved identity no longer resolves; their limits
are preserved.

## Control Policy

While Heat is selected and the heater is in Level Mode, the controller compares
one room-temperature feedback sensor with the selected target. Merely switching
the heater to Level Mode does not enable this thermostat; select Heat as well.
Below target, it selects a level directly from the temperature gap using a fixed
5°C proportional band and the selected level control's actual stage count. A gap
at or above 5°C selects maximum output; a small positive gap selects level 1 (up
to 0.5°C below target with ten stages). All control math uses Celsius, including
on Fahrenheit installations.

At or above target it requests the source's normal Power-off command. The
virtual thermostat remains in Heat and can request startup again below target.
Normal cooldown may continue after shutdown is requested. Increasing or
decreasing demand selects the newly calculated level immediately. There is no
one-step ramp, hold timer, overshoot band, periodic control timer, or native
heater calibration write. The normal feedback options are the room sensor and
its local Celsius offset; deleted safety-sensor bindings can also be repaired.
Case, intake, outlet and exhaust probes are not averaged into room temperature.

Control is suspended outside Level Mode. A source-owned burn-off remains in
charge while its level entity is unavailable. Explicit virtual Off requests
normal shutdown; it does not cut electrical power. Missing or invalid room
feedback also requests normal shutdown when Level-mode ownership and the
source's shutdown gate permit it.

The virtual Heat/Off state and target are restored after a restart, with the
saved target converted if Home Assistant's temperature unit changes. An invalid
or unresolvable out-of-range legacy target leaves virtual heating Off. Restored
heating demand waits for valid source inputs. Pending control tasks are
cancelled on unload.

The source integration and heater firmware remain responsible for combustion,
altitude compensation, cooldown and hardware protections.

## Install

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Wheemer&repository=diesel-level-thermostat&category=integration)

1. Install and configure
   [Diesel Heater](https://github.com/Spettacolo83/homeassistant-diesel-heater),
   then confirm its Power, Running Mode, and at least one supported level control work.
2. Add this repository to HACS with category **Integration**, then download
   **Diesel Level Thermostat** and restart Home Assistant. Adding a custom
   repository alone does not install it.
3. Add **Diesel Level Thermostat** from Settings > Devices & services and choose
   the Diesel Heater device.
4. Choose the room-temperature sensor and, if needed, a local correction.
   These two feedback settings remain available in Options.
5. Put the heater in **Level Mode**, then select **Heat** and a target on the
   new climate entity. Adjust the room-feedback options as needed.

The thermostat appears under its own **Diesel Level Thermostat** integration and
device. Its climate entity is separate from the original Diesel Heater climate
entity.

Do not run the virtual thermostat while the heater is in its native Temperature
Mode. The virtual climate entity is intentionally unavailable in that mode.

## Troubleshooting

If the thermostat is unavailable, open **Developer Tools → States** and select
its climate entity. These attributes remain visible while it is unavailable:

| Attribute | Meaning |
|---|---|
| `unavailable_reason` | The required input or source guard blocking availability. |
| `source_level` | The level control currently selected. |
| `configured_source_level` | The level control saved in configuration. |
| `source_candidate_states` | The states and attributes of the validated level-control candidates. |
| `source_power`, `source_mode`, `source_room_temperature` | The other selected source entities. |
| `source_states` | The selected entities' current reported states and attributes. |

Check that the heater is in Level Mode, a valid level control is available, and
the selected room sensor has a numeric temperature with a supported unit. Wait
for active burn-off to finish rather than bypassing its lock.

## Safety

Diesel heaters require a controlled cooldown. Use the existing heater Power
entity rather than cutting mains/DC supply. Verify normal startup, shutdown,
burn-off, and fault behavior with the heater attended before relying on an
automation.

For development information, see [CONTRIBUTING.md](CONTRIBUTING.md).
