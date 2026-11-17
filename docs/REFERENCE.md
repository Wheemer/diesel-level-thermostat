# Controller and control reference

[Back to README](../README.md)

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

For development information, see [CONTRIBUTING.md](../CONTRIBUTING.md).

