# Lessons learned

Findings from building and operating the system on the reference installation
(GoodWe GW10KN-ET, BYD HVS 7.7, Home Assistant OS). They are not all specific to
this hardware.

## Measurements of the inverter

* **Battery power is mis-measured at low power.** `pbattery1` reads about
  110–150 W towards *charging* when the real battery current is ~0, quantised
  in ~31 W steps. The BMS's own current (`bms_bat_current`) does not show this.
  Check your system with `tools/bms_compare.py`.
* **`house_consumption` is computed, not measured.** The goodwe library
  derives it as PV + battery power − grid; it inherits the battery error
  (≈110 W too low at night) and adds conversion losses to the house (≈150 W too
  high at high PV). Over the day both errors roughly cancel. Use
  `sensor.load` (inverter register "Load"), which is measured and matches
  output − grid − backup load within a few watts.
* Consequently, apparent "standby losses" of ~110 W derived from these values
  were a measurement artefact. Real values on the reference system: discharge
  efficiency cells → AC 85–87 %, standby at the discharge floor ~34 W.
* **SOC below the floor:** while the battery idles at the discharge floor, the
  reported SOC keeps falling about 1 % every 6–7 hours (≈12 W equivalent), with
  0.0 A current and an essentially constant voltage. Once it fell 1 %/h for a
  night (15 → 7 %) at constant voltage – an estimator correction, not real
  energy. The voltage of LFP is too flat in this range to tell estimator drift
  from real BMS self-consumption with certainty. Harmless for the control (low
  SOC → 25 A), but worth watching in long dark periods.
* **BMS float sag:** after reaching 100 % the reported SOC drops to 96–98 %
  without real discharge; the inverter only recharges below ~95 %. Hence the
  dead band around "almost full".

## Forecasts

* Forecast.Solar (free tier) under-forecast systematically: day-ahead it was
  too low in 11 of 11 comparison days (bias −26 %, MAE 26 %), Solcast scattered
  in both directions (MAE 15 %, bias +8 %). A correction factor fixes
  Forecast.Solar's bias, not its scatter (MAE stays ~29 %). Solcast is the
  better control source; Forecast.Solar stays as automatic fallback.
* A REST sum of Solcast's future periods is only valid at fetch time and then
  freezes – around noon it keeps counting energy already produced. Compute the
  rest minute by minute from the raw periods (see `gfc_forecast.yaml`).
* Solcast hobbyist: 10 calls per day (reset 00:00 UTC). Every HA restart and
  every `rest.reload` costs a call. On some days Solcast returned empty replies
  from a morning fetch until the evening – the fallback handles it.

## Home Assistant behaviour that bit us

* `shell_command` has a hard 60 s timeout and does not raise on exit != 0.
* A template trigger that is already true never fires again (edge only) – use
  a binary sensor with `delay_on` as a state instead.
* `mode: single` silently drops triggers while running.
* `for:` timers and trigger edges do not survive a reload/restart; restored
  states do not produce a new edge. Use time-pattern watchdogs for anything
  safety-relevant.
* `this.state` is `unknown` after a `template.reload` – hysteresis needs a
  fallback (here: the last read-back value).
* `automation.trigger` skips conditions by default.
* Statistics sensors mirror the unavailability of their source – do not put
  them into another template's `availability`.
* `input_number` with `initial:` resets on every restart.
* `core.restore_state` is written every 15 minutes; a hard restart shortly
  after a change can bring back the old state.
* The recorder keeps history for 10 days by default – long-term analysis needs
  its own cache (the dashboard has one) or a longer retention.
* `homeassistant.update_entity` on a GoodWe *setting* entity does not re-read
  the value from the inverter.
* The entity ID is derived from the `name`, not from `unique_id`.

## Operations

* WiFi drop-outs of the Home Assistant host happened several times. The system
  stays safe (the inverter keeps the last value, the reconcile heals), but a
  wired connection or a network watchdog is recommended.
* An adversarial review of the write path (several independent reviewers
  trying to break it) found blockers three times before the final, simpler
  architecture passed. For anything that writes to hardware this was worth it.
