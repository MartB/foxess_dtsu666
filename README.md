# foxess_dtsu666

Home Assistant custom component for the Chint DTSU666 three-phase energy meter, as wired by FoxESS inverters on their RS485 bus.

![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)

> **Early stage / use at your own risk.**
> This project started as a debugging tool after FoxESS firmware 1.93 introduced intermittent *Meter Lost* faults. The goal was to tap the RS485 bus and see exactly what was happening between the inverter and the meter. It has since grown into a full Home Assistant integration, but it is still young and has only been tested on a handful of setups.
>
> The code was written with the help of [Claude](https://claude.ai) (Anthropic). Review it before running it in production.

## How it works

The DTSU666 sits on an RS485 bus where the inverter polls it periodically. This integration taps the same bus passively and decodes the responses as they go by — no extra polling, no bus contention. If the inverter goes offline and traffic stops, the sniffer detects the silence (default: 15 s) and switches to active polling mode, querying the meter directly until things resume.

## Requirements

You need an RS485 adapter on the same bus as the meter. A USB-to-RS485 stick plugged into your HA machine works, as does any RS485-to-TCP bridge (esp-link, ser2net, Waveshare devices, etc.). Default bus parameters are 9600 baud, 8N1 — match whatever your inverter uses.

## Installation

### HACS (recommended)

Add this repository as a custom repository in HACS (category: Integration), then install **FoxESS DTSU666 Sniffer** and restart Home Assistant.

### Manual

Copy `custom_components/foxess_dtsu666/` into your HA configuration directory and restart.

## Setup

**Settings → Integrations → Add integration → FoxESS DTSU666 Sniffer**

The wizard asks for connection type (serial or TCP) and bus parameters, briefly scans the bus to find active slave IDs, then asks about active polling.

**Slave ID** — which device to poll when the inverter goes silent. You can skip this and set it later via *Reconfigure*.

**Always poll** — drives the bus directly from the start, bypassing passive mode. Only enable this if nothing else is polling the meter; two bus masters running simultaneously will produce CRC errors.

## Sensors

| Sensor | Register group | Enabled by default |
|---|---|---|
| Voltage L1 / L2 / L3 | 0x1510 | yes |
| Current L1 / L2 / L3 | 0x1510 | yes |
| Active power total | 0x1510 | yes |
| Active power L1 / L2 / L3 | 0x151E | yes |
| Reactive power total / L1 / L2 / L3 | 0x151E | no |
| Apparent power total / L1 / L2 / L3 | 0x1510 | yes |
| Power factor total / L1 / L2 / L3 | 0x1510 | no |
| Frequency | 0x1510 | yes |
| Energy import total | 0x181E | yes |
| Energy export total | 0x181E | yes |
| Energy import / export L1 / L2 / L3 | 0x181E | no |
| Reactive energy Q1 / total | 0x181E | no |
| cos φ (computed from P and Q) | derived | yes |

Diagnostic sensors (on the bus device, always available): CRC errors, timeouts, resyncs, consecutive errors, average response time, per-address poll intervals, last bus error, last seen age, and a list of any register addresses seen on the bus that this integration does not handle.

A measurement sensor becomes *Unavailable* if its register group has not been seen within the staleness threshold (default 60 s, configurable).

## Firmware notes

FoxESS inverter firmware 1.91 only polls register address 0x1510. Addresses 0x151E and 0x181E are never requested, which means per-phase active power, all energy totals, and reactive power will be unavailable. The diagnostic sensors *Poll interval 0x151E* and *Poll interval 0x181E* will show *Unknown* when this is the case.

Workaround: set the slave ID in the integration options. When the inverter eventually goes quiet (or if you enable *Always poll*), the sniffer will fill in the missing register groups itself.

## Future work

**Per-address supplemental polling** — if specific register groups have not been seen for a while but the bus is otherwise active, poll only those addresses rather than taking over the whole bus. This would handle the firmware 1.91 case automatically without any manual configuration. The groundwork (per-address poll-interval sensors, unknown-endpoint tracking) is already in place.

## License

MIT — see [LICENSE](LICENSE).
