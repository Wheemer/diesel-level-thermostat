# Diesel Level Thermostat

Diesel Level Thermostat is a Home Assistant custom integration that adds a
virtual thermostat on top of a diesel heater already controlled by the
`Diesel Heater` integration.

It deliberately controls the heater only in **Level Mode**. It does not send
the heater's native temperature-mode command. This keeps exactly one control
policy active at a time:

- **Level Mode:** this integration calculates and sends Levels 1 through 10.
- **Temperature Mode:** the heater's native controller remains in charge and
  this virtual thermostat becomes unavailable.

## What It Uses

The setup flow selects the existing Diesel Heater entities rather than talking
to BLE itself:

- required: room temperature, power switch, heater-level fan, and running-mode
  entity;
- optional safety inputs: a fault binary sensor, supply voltage, case
  temperature, and a second thermal sensor. These can be the exact physical
  sensors exposed by the selected controller, such as a case, intake, outlet,
  or exhaust probe.

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
contract: a Power switch, Heater Level fan, and Running Mode select on one
device. That covers the established AA55/AA66, ABBA/HeaterCC, CBFF/Sunster,
Hcalory MVP, and HeatGenie/Boygu air-heater families. The setup is
telemetry-aware rather than protocol-hardcoded, so it follows the actual
entities exposed by each controller.

Webasto Cronus is intentionally not selectable for staged-level control: the
source integration exposes a different air/water controller model and no
public Heater Level fan. Water controllers are likewise excluded because a
room-air level loop would be the wrong control model.

## Control Policy

While heating is requested, the controller maps the temperature error into a
discrete level between the configured minimum and maximum. It uses a
proportional band, hysteresis, and minimum on/off periods to avoid needless
level changes and short cycling. A configured safety input stops output when
it is unsafe.

The defaults are conservative starting points, not universal combustion
settings. Tune them for the installed heater and room. This integration is not
a replacement for the heater's firmware safety systems.

## Install

1. Install the Diesel Heater integration and confirm its Power, Heater Level,
   and Running Mode entities work.
2. Install this repository as a custom HACS integration and restart Home
   Assistant.
3. Add **Diesel Level Thermostat** from Settings > Devices & services.
4. Select the existing heater entities and optional safety sensors.
5. Put the heater in **Level Mode**, then use the new climate entity.

Do not run the virtual thermostat while the heater is in its native Temperature
Mode. The virtual climate entity is intentionally unavailable in that mode.

## Safety

Diesel heaters require a controlled cooldown. Use the existing heater Power
entity rather than cutting mains/DC supply. Verify normal startup, shutdown,
burn-off, and fault behavior with the heater attended before relying on an
automation.
