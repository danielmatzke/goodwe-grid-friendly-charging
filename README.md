# GoodWe grid-friendly charging

Forecast-based, grid-friendly battery charging for **GoodWe ET hybrid inverters**
with **Home Assistant** – no cloud, no extra hardware.

A home battery that charges "greedily" is full by late morning on a sunny day
and then pushes the entire midday PV peak into the grid – exactly when the grid
is most loaded and many PV systems are curtailed. This project keeps headroom
in the morning and charges the battery **over the midday hours instead**, so the
midday peak goes into the battery, while still reaching 100 % before sunset.
This is the strategy recommended by the research group of HTW Berlin
("Dein Stromspeicher kann mehr!"); several inverter brands offer it natively,
GoodWe was not among them in their overview.

> **Status:** running in daily operation on the reference system since
> September 2026. Read the [safety notice](#safety-notice) before you install
> anything.

## How it works

```
Solcast (live) -------------+
                            +--> PV forecast, rest of today (kWh)
Forecast.Solar (fallback) --+    |
                                 v
                                 usable energy = forecast - expected house load
                                 |
                                 v
SOC, PV, load, grid -----------> target stage  0 / 6 / 12 / 18 / 25 A
(reserve, dark day, safeguard)   |
                                 v
                                 charge_control.py
                                 write only on change, read-back, daily budget
                                 |
                                 v  local UDP, port 8899
                                 inverter setting battery_charge_current
```

* The only lever is the inverter setting **`battery_charge_current`** (A),
  written over the local UDP protocol with the open-source
  [`goodwe`](https://github.com/marcelblijleven/goodwe) library – the same
  library the official Home Assistant GoodWe integration uses. Discharging is
  never touched.
* The current is a **cap, not a setpoint**: when clouds make the surplus
  flutter, the charging power follows by itself without any write. A new stage
  is only written when the state of charge leaves the band needed to be full
  by sunset. This needs about **3 writes per day**.
* Hard rules open the limit (25 A = normal inverter behaviour): night, SOC below
  the reserve, dark days (detected from measurement), forecast too low, almost
  full. In the morning, if the forecast covers the missing energy three times
  over, charging is paused (0 A) to keep headroom.
* A **safeguard** watches for grid import while charging and sets an emergency
  stop flag (0 A). There is exactly one write path and one master switch.

Details: [docs/architecture.md](docs/architecture.md).

## Results on the reference system

68 complete days (September – November 2026), measured operation compared with
a simulation of the same days with greedy charging (same PV, same house load):

| Metric (mean per day) | Grid-friendly | Greedy (simulated) |
|---|---|---|
| Export 11:00–15:00 | 8.6 kWh | 10.4 kWh |
| Export 11:00–15:00, sunny days only (34) | 14.9 kWh | 18.4 kWh |
| Battery full at (days where both got full) | 13:55 | 11:44 |
| Daily export peak | 5.9 kW | 6.2 kW |
| Inverter writes | 2.9 (max 10) | – |

* In total **124 kWh (−18 %) less export in the midday window**.
* At the moment of the simulated greedy neighbourhood's peak, the system fed in
  on average 1.1 kW less (53 days with relevant export; higher on only 2 days).
* 202 verified writes, **0 failed read-backs**, 0 write errors.
* On 7 of 68 days the controlled system exported slightly more than greedy at
  midday (bursty cloudy days, where a single sunny spell hides a dark day).

Reference hardware: GoodWe GW10KN-ET (firmware 04029-10-S11 / 02041-33-S00),
BYD Battery-Box Premium HVS 7.7 (BMU 3.27, BMS 3.32), 9.7 kWp south 45°,
Home Assistant OS on a Raspberry Pi, goodwe 0.4.10.

## Requirements

* GoodWe **ET-family** hybrid inverter (ET/EH/BT/BH) reachable on the LAN; the
  inverter must expose the setting `battery_charge_current`
  (`tools/inventory.py` checks this read-only).
* Home Assistant with the **GoodWe** integration (local), the
  **Forecast.Solar** integration, and optionally a free
  [Solcast](https://solcast.com) hobbyist rooftop site (recommended – much
  better intraday forecasts).
* Shell access to the Home Assistant config directory (e.g. the SSH add-on).

## Installation

The recommended way is to let a coding agent walk you through it:
**[AGENTS.md](AGENTS.md)** is a step-by-step bootstrap guide written for AI
coding agents (and humans). It covers discovery (read-only), the reduced
polling, a supervised test of the write path, installation, a dry run and the
arming procedure.

Short version for experienced users:

1. Disable polling on the GoodWe config entry, install the packages from
   `homeassistant/packages/` (`homeassistant: packages: !include_dir_named packages`),
   copy `homeassistant/scripts/charge_control.py` to `/config/scripts/` and set
   `INVERTER_HOST`, fill `secrets.yaml` from `homeassistant/secrets.example.yaml`.
2. Restart, set the helper start values, watch `sensor.battery_charge_target_a`
   for a few days.
3. Arm as described in AGENTS.md (the control ships disarmed).

## Repository layout

| Path | Content |
|---|---|
| `homeassistant/packages/` | Home Assistant packages: parameters, forecast, decision logic, write path, polling |
| `homeassistant/scripts/charge_control.py` | The only code that writes to the inverter |
| `tools/` | Bootstrap tools: read-only inventory, supervised write tests, BMS comparison |
| `dashboard/` | Optional observation dashboard: actual vs. simulated greedy operation |
| `tests/` | Tests for the decision logic and the write path (no hardware needed) |
| `docs/` | Architecture, UDP communication rules, lessons learned |

Run the tests with `pip install -r requirements-dev.txt && python -m pytest tests`.

## Safety notice

* This project writes a setting of your inverter over an **unofficial local
  interface**. GoodWe does not document or support this. It may affect
  manufacturer support or warranty. **You use it at your own risk** (see the
  [license](LICENSE): no warranty of any kind).
* It only ever writes `battery_charge_current` and only the values
  0/6/12/18/25 A. **Never** use it (or let an agent use it) to change grid-code,
  export-limit, protection or any other setting.
* Writes are kept to a minimum (write only on change, daily budget with a hard
  stop) because it is unknown whether the setting is stored in flash.
* If anything looks wrong: run the script **"GFC: release charging (25 A)"** –
  it restores normal inverter behaviour – and turn the control automation off.

Not affiliated with GoodWe, BYD, Solcast or Home Assistant. Product names are
used only to describe compatibility. The Solcast hobbyist tier is for
non-commercial use; respect its terms and call limits.

## License

[MIT](LICENSE)
