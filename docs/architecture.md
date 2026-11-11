# Architecture

## Goal

Keep headroom in the battery in the morning, absorb the midday PV peak into the
battery instead of exporting it, and still be full before sunset. Only the
**charging** current is limited; discharging (self-consumption) is untouched,
and the battery is never charged from the grid.

## The lever

GoodWe ET inverters have no official control API, and the regular Home
Assistant actuators (operation modes, export limit) cannot pause or throttle
charging without side effects. The inverter-side setting
`battery_charge_current` (Modbus register 45353, separate from the BMS limit)
does exactly that:

* 0 A pauses charging within seconds; the surplus goes to the grid; discharging
  keeps working; no grid import (verified on the reference system).
* Intermediate values are honoured precisely: 12 A → 3.91 kW measured
  (3.92 expected at 327 V), 6 A → 1.94 kW (1.96 expected).
* The value survived a whole night unchanged (the firmware does not reset it).
  Whether it is stored in flash or RAM is unknown – hence the write budget.

## Decision chain (gfc_forecast.yaml, gfc_logic.yaml)

```
forecast: Solcast live, or Forecast.Solar x factor    sensor.pv_remaining_corrected
  - mean daylight house load (7 d) x hours to sunset  sensor.pv_remaining_usable
missing energy to 100 %                               sensor.battery_missing_kwh
SOC, 3 h surplus max, grid and battery power          reserve mode, dark day, safeguard
                          |
                          v
          sensor.battery_charge_target_a, one of 0 / 6 / 12 / 18 / 25 A
```

Rule order of the target stage (first match wins):

| # | Condition | Stage | Why |
|---|---|---|---|
| 1 | emergency stop flag | 0 | safeguard executed through the normal write path |
| 2 | sun below horizon | 25 | nothing to steer at night; normal behaviour |
| 3 | SOC < reserve, reserve mode, dark day, usable ≤ missing × 1.3 | 25 | the forecast cannot be trusted to fill the battery |
| 4 | missing ≤ 0.35 kWh (leave > 0.55) | 25 | almost full; dead band against BMS float sag |
| 5 | before 11:00, was 25, SOC < reserve + 7 | 25 | morning hold: breaks a write loop around the reserve |
| 6 | before 11:00, usable ≥ missing × 3.0 (leave < 2.4) | 0 | headroom: plenty of PV ahead |
| 7 | otherwise | 6/12/18/25 | smallest stage covering missing / 0.96 / (hours left × 0.65) |

Why a **stage cap** instead of on/off: on/off control has to follow every cloud
(up to 18 switches on a cloudy day on the reference system). A cap does not –
when the surplus drops below the cap, charging simply follows the surplus. The
stage only changes when the SOC trajectory leaves the band, which on the same
cloudy days needed 2 changes.

Why the **dark-day detector uses measurement**, not forecast: forecast day
totals did not discriminate dark days (about the same total on real 12- and
33-kWh days), and an intraday actual/forecast ratio fails on day-shape errors.
The rolling 3 h maximum of the measured surplus is robust, with one known
weakness: a single short sunny burst on an otherwise dark day hides it.

Why **net** usable energy: without subtracting the expected daylight house
load the battery missed full charge on days with high daytime consumption;
with it, the factors in rules 3 and 6 are real safety margins.

## Write path (gfc_control.yaml, charge_control.py)

Three adversarial reviews of earlier versions found more than a dozen
blocking issues. All of them came from **several automations coupling their state**
(switching each other on/off, healing flags automatically). The final
architecture is therefore deliberately minimal:

* **One write path, one master switch.** Only the control automation calls
  `charge_control.py`. Off = nobody writes.
* **The safeguard does not write.** It sets `input_boolean.gfc_emergency_stop`;
  the target becomes 0; the control writes it via the emergency path
  (no window wait, budget ignored). The 4-minute persistence lives in a
  `binary_sensor` with `delay_on`, so the safeguard can fire again while the
  condition persists (a template trigger that is already true never fires again).
* **Reconcile without branches.** Every 2 h at minute 37 and on HA start it
  writes the current target. Thanks to write-only-on-change this costs zero
  writes in normal operation and heals any deviation.
* **Release only manually.** The flag is cleared only by
  `script.gfc_release_charging_25a`. Every automatic reset found in the reviews
  could heal into a wrong state (e.g. 0 A with the control off → battery never
  charges).

Details that matter:

* `mode: single` drops a trigger while a run is in progress. The emergency flag
  therefore bypasses all conditions, and two extra triggers re-check it: 5 min
  after the flag went on, and a restart-proof time pattern at minutes 3/23/43
  (`for:` timers and edges do not survive a reload/restart). Maximum gap
  between grid import and stop: about 20 minutes.
* HA kills `shell_command` after 60 s and then aborts the calling automation.
  `charge_control.py` ends itself after 45 s in all cases.
* `shell_command` does not raise on a non-zero exit code, and the inverter has
  no sensor for the configured charge current. The script therefore writes a
  state file after every run; `sensor.charge_control_result` makes result and
  last read-back value visible, and the target template uses that value to keep
  its hysteresis across reloads.
* A file lock prevents parallel UDP sessions between control, reconcile and
  the release script.
* Arming requires an initial sync (`automation.trigger` on the reconcile)
  because the control only reacts to *changes* of the target.

## Accepted residual risks

* A stage run in progress when the emergency flag goes on can write the old
  value for up to ~4 minutes before the stop follows (worst case ≈ 0.4 kWh
  grid import).
* The emergency path does not wait for the safe window and can collide with
  the poll (effect seen once: sensors frozen for ~12 minutes, no damage).
* After a hard crash, HA restores states from a dump made every 15 minutes;
  a flag or switch changed shortly before can come back in its old state. The
  safeguard re-sets the flag while the condition persists.
* While HA is offline the inverter keeps the last written value. With the
  reconcile and the nightly 25 A this means at most part of a day with a
  non-ideal cap.
