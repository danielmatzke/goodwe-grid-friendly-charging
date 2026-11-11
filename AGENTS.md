# AGENTS.md – bootstrapping grid-friendly charging

This file tells a coding agent (Claude Code, Codex, Cursor, …) how to set this
project up on a user's Home Assistant installation. It is also a complete
manual installation guide for humans.

You are working on **real energy hardware**. Follow the phases in order. Each
phase ends with a check. Do not skip a check, and do not start the next phase
when a check fails – report to the user instead.

---

## 0. Ground rules (non-negotiable)

1. **One UDP session at a time.** The inverter's UDP interface (port 8899) does
   not tolerate parallel sessions; the Home Assistant GoodWe integration uses
   the same interface. Never run two tools at once, never poll in a loop.
   Read [docs/udp-rules.md](docs/udp-rules.md) before the first inverter access.
2. **The only setting that may ever be written is `battery_charge_current`,
   and only the values 0, 6, 12, 18, 25.** Never write any other setting –
   in particular nothing grid-related (export limit, grid code, protection,
   power factor, operation mode, depth of discharge). If the user wants to
   change the depth of discharge, they do it themselves in the Home Assistant
   UI (`number.*depth_of_discharge*`).
3. **Write only on change, verify every write by a read-back.** All tools in
   this repo already do that – use them, do not write ad-hoc scripts that write.
4. **Every write needs the user's explicit, specific consent** in the
   conversation ("yes, run the pause test now"). A human must be present and
   able to watch the system during the supervised tests in phase 3.
5. **Safety before optimisation.** When in doubt, restore normal inverter
   behaviour: `battery_charge_current = 25` (via
   `script.gfc_release_charging_25a` once installed) and stop.
6. **No secrets or personal data in the repository.** Tokens, API keys, the
   Solcast site id, IP addresses, the inverter serial number, baseline dumps
   and logs stay on the user's machine (`local/` is git-ignored). Never commit
   or print the user's Home Assistant token.
7. **The control ships disarmed** (`initial_state: false`). Arming is the last
   step and needs the user's go.

---

## 1. Gather information (ask the user)

Ask for, or find out together with the user:

| Item | Why | Example |
|---|---|---|
| How you can access the HA config directory | install files | SSH add-on (`ssh root@homeassistant.local`), Samba share, Studio Code Server |
| A long-lived access token (HA profile → Security) | REST API checks | keep it in an env var, never in a file inside the repo |
| Inverter IP address (fixed DHCP lease!) | UDP access | `192.168.1.50` |
| Inverter model and battery model | compatibility | GW10K-ET + BYD HVS 7.7 |
| Usable battery capacity (kWh) | `input_number.battery_usable_kwh` | 7.68 |
| Typical battery voltage while charging (V) | stage power | read `sensor.battery_voltage` around 50 % SOC |
| Current discharge floor | reserve setting | DoD 85 % on-grid = floor 15 % |
| Solcast hobbyist rooftop site (optional, recommended) | forecast | site id + API key |
| Time zone | dashboard | `Europe/Berlin` |

Store connection details in environment variables for this session, e.g.
`HA_URL`, `HA_TOKEN`, `GFC_INVERTER_HOST`. Do not write them into tracked files.

**Check 1:** `curl -s -H "Authorization: Bearer $HA_TOKEN" $HA_URL/api/` returns
`{"message": "API running."}` and you can list `/config` on the HA host.

---

## 2. Discovery (read-only)

### 2.1 Home Assistant side

1. HA version: `GET $HA_URL/api/config` → `version`. Tested with 2026.x.
2. The GoodWe integration is set up (local, not the SEMS cloud). Find its
   entities and map them to the names used in this project:

   | Used in packages | GoodWe sensor (goodwe lib id) | Meaning / sign |
   |---|---|---|
   | `sensor.battery_state_of_charge` | `battery_soc` | % |
   | `sensor.battery_power` | `pbattery1` | W, **negative = charging** |
   | `sensor.pv_power` | `ppv` | W |
   | `sensor.load` | `load_ptotal` ("Load") | W, measured house load |
   | `sensor.meter_active_power_total` | `meter_active_power_total` | W, **positive = export** |

   Entity IDs differ between installations (e.g. a device-name prefix like
   `sensor.goodwe_load`). Query `$HA_URL/api/states` and compare names and
   current values. Verify the signs with the user (e.g. at night: battery
   power positive = discharging, meter negative = import).
3. Forecast.Solar is set up; find the entity "Estimated energy production –
   remaining today" (`sensor.energy_production_today_remaining`, a second
   instance gets a `_2` suffix).
4. If any entity ID differs, plan to replace it consistently in
   `homeassistant/packages/*.yaml` **and** in `EXTERNAL` in
   `tests/test_config.py`. Do this in the user's copy, not in the upstream repo.

### 2.2 Inverter side

Install the tools' only dependency on the user's machine (same version as
the HA integration ships):

```sh
python3 -m venv .venv && .venv/bin/pip install -r tools/requirements.txt
mkdir -p local
.venv/bin/python tools/inventory.py --host $GFC_INVERTER_HOST --out local/baseline.json
```

The tool waits for the safe window (odd minute, seconds 5–30) by itself.

**Check 2:** the inventory prints `battery_charge_current = 25` (or another
value) without errors. If the setting is missing, **stop**: this inverter or
firmware is not supported. Keep `local/baseline.json` – it documents the
original settings.

---

## 3. Reduced polling and supervised write tests

### 3.1 Polling every 2 minutes

1. User action in the UI: Settings → Devices & services → GoodWe → ⋮ →
   System options → **disable polling** ("Enable polling for changes" off).
2. Install only `homeassistant/packages/gfc_goodwe_polling.yaml` for now (see
   4.1 for how packages are enabled), or create the same automation in the UI.

**Check 3.1:** over 10 minutes, `sensor.pv_power` changes only on even minutes
(`/api/history/period/...`). All timing rules depend on this grid.

### 3.2 Supervised tests (user present, sunny late morning, battery not full)

Before each test: turn **off** the automation "GFC GoodWe polling (every 2 min)"
(exclusive UDP session). After the test: turn it back **on**.

1. Dry run first (checks preconditions, writes nothing):
   `.venv/bin/python tools/test_pause.py --host $GFC_INVERTER_HOST --log local/pause.jsonl`
2. With the user's explicit consent, run it for real with `--yes`.
   Expected: at 0 A charging stops within seconds, the surplus goes to the
   grid, **no grid import**, discharging still possible; restore to 25 A is
   verified.
3. Same for intermediate stages:
   `.venv/bin/python tools/test_stages.py --host $GFC_INVERTER_HOST --log local/stages.jsonl --out local/stages.json [--yes]`
   Expected: at 12 A and 6 A the charging power ≈ A × battery voltage
   (`limited: true`).

**Check 3.2:** both tests end with `"restore", "ok": true` and the stage test
shows limited charging power. If 0 A causes grid import or stages are not
honoured, **stop** – the control strategy does not work on this system.
If a restore ever fails, tell the user immediately and have them check
`battery_charge_current` (re-run `inventory.py`).

---

## 4. Installation

### 4.1 Files

On the HA host (paths relative to `/config`):

1. Enable packages in `configuration.yaml` (merge with an existing
   `homeassistant:` block, do not duplicate it):
   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```
2. Copy `homeassistant/packages/*.yaml` to `/config/packages/`.
3. Copy `homeassistant/scripts/charge_control.py` to
   `/config/scripts/charge_control.py` and set `INVERTER_HOST` in the
   CONFIGURE block. (It runs inside the HA container and uses the goodwe
   library of the GoodWe integration; no installation needed.)
4. Add the entries from `homeassistant/secrets.example.yaml` to
   `/config/secrets.yaml` (Solcast URL with the site id, and
   `Bearer <api key>`). Without Solcast: remove the `rest:` block and the
   fetch automation from `gfc_forecast.yaml`, and leave
   `input_boolean.gfc_use_solcast` off.
5. If entity IDs differ (step 2.1), apply the replacements now.

Back up every file you overwrite. Copy with checksums and compare them.

### 4.2 Validate and restart

1. Locally: `python -m pytest tests` (after any adaptation).
2. On HA: `POST $HA_URL/api/config/core/check_config` → `"valid"`.
3. Restart Home Assistant (new integrations such as `command_line` and
   `input_boolean` need a restart, a reload is not enough).

### 4.3 Start values

Helpers have no `initial:` on purpose. Set them once:

```sh
for kv in battery_usable_kwh=7.68 battery_nominal_voltage_v=327 pv_correction_factor=1.3 \
          min_reserve_soc=20 dark_day_surplus_w=2500; do
  curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
    $HA_URL/api/services/input_number/set_value \
    -d "{\"entity_id\":\"input_number.${kv%%=*}\",\"value\":${kv#*=}}"
done
```

Use the user's values. `min_reserve_soc` should be about 5 points above the
inverter's discharge floor. Turn `input_boolean.gfc_use_solcast` on if Solcast
is configured, and trigger one Solcast fetch (`homeassistant.update_entity` on
`sensor.solcast_forecast_raw`; each call costs one of the 10 daily calls).

**Check 4:** all of these have valid states (not `unavailable`) during
daylight: `sensor.pv_remaining_corrected`, `sensor.pv_remaining_usable`,
`sensor.battery_missing_kwh`, `sensor.battery_charge_target_a`,
`sensor.pv_forecast_source`. `automation.gfc_control_write_charge_current_stage`
is **off**. `sensor.charge_control_result` is `unknown` (no run yet).

### 4.4 No-op test of the write path (reads only)

Ask the user, then call the shell command with the value the inverter already
has (normally 25) in an **odd minute**:

```sh
curl -s -X POST -H "Authorization: Bearer $HA_TOKEN" -H "Content-Type: application/json" \
  $HA_URL/api/services/shell_command/gfc_set_charge_current -d '{"ampere": 25}'
```

**Check 4.4:** `/config/charge_control_log.jsonl` ends with
`{"event": "skipped_unchanged", "value": 25.0, …}` and
`sensor.charge_control_result` becomes `ok_unchanged` (update it with
`homeassistant.update_entity`). If you see `config_error`, `INVERTER_HOST` is
not set; `connection_error` means the HA container cannot reach the inverter.

---

## 5. Dry run (2–7 days, control still disarmed)

Let the logic run without writing. Each evening, look at the history of
`sensor.battery_charge_target_a` together with SOC, PV and grid power.

**Check 5:** on sunny days the stage is 0 A in the morning (headroom), then
6–18 A around midday, 25 A before the battery is full and at night. On dark
days it stays 25 A. Count stage changes that would be written (stable ≥ 4 min,
≥ 10 min apart): typically 2–5 per day. If you see many more, investigate
before arming (wrong entity, wrong sign, wrong capacity).

Optional: set up the [dashboard](dashboard/README.md) now.

---

## 6. Arming (only with the user's explicit go)

1. Pre-check: `input_boolean.gfc_emergency_stop` off;
   `sensor.battery_charge_target_a` in {0, 6, 12, 18, 25};
   `sensor.charge_control_result` not `ALARM_readback`; automations
   "GFC Safeguard" and "GFC Reconcile" on.
2. Delete the line `initial_state: false` in `/config/packages/gfc_control.yaml`
   (keep the rest unchanged) and call `automation.reload`.
3. Turn on `automation.gfc_control_write_charge_current_stage`.
4. **Then** trigger the initial sync: `automation.trigger` on
   `automation.gfc_reconcile_target_vs_inverter`. (Order matters:
   `automation.trigger` skips conditions; triggering before step 3 would
   bypass the master switch.)
5. Verify in `charge_control_log.jsonl`: `write_ok` (`attempt: 1`) or
   `skipped_unchanged`, and `sensor.charge_control_result` = `ok_written` /
   `ok_unchanged`.
6. Do not restart HA within 15 minutes after arming (HA saves restored states
   only every 15 minutes; a hard restart could bring back the old state).

**Evening check on day 1:** 2–6 `write_ok`, all with `attempt: 1`; about 12
`skipped_unchanged` from the reconcile; no error events; on a sunny day
`sensor.battery_power` ≈ 0 during the 0 A morning phase while exporting, no
grid import while charging, battery full in the afternoon.

---

## 7. Operation

* **Release / emergency:** script "GFC: release charging (25 A)" clears the
  emergency stop flag and opens the limit. It works even on budget stop.
* **Disarm:** first run the release script, then turn the control automation
  off. Never just turn it off while the inverter is at 0 A – it would stay there.
* **Log events** (`charge_control_log.jsonl`): `write_ok`, `skipped_unchanged`
  are normal. `readback_mismatch`, `write_error`, `connection_error`,
  `ALARM_readback_failed`, `budget_warning` (≥ 25 writes/day),
  `budget_stop` (≥ 40) need attention.
* **Safeguard:** if `binary_sensor.gfc_grid_import_while_charging` fires
  (import > 300 W while charging > 200 W for 4 min), the flag stops charging.
  Find the cause before releasing.
* **Recorder retention:** HA keeps history for 10 days by default. Run the
  dashboard at least every ~9 days or extend `recorder: purge_keep_days`.
* **Solcast** returns empty replies on some days for hours; the chain falls
  back to Forecast.Solar automatically (`sensor.pv_forecast_source` =
  `forecast_solar_fallback`). No action needed.

## 8. Adapting to other hardware

* **Stages:** 0/6/12/18/25 A assume a battery that accepts 25 A. If your
  battery's maximum charge current is lower, keep 25 as "open" (the BMS limits
  anyway) but check the stage powers. If you change the stage set, change it in
  all three places (template, automations, `VALID` in `charge_control.py`) –
  `tests/test_config.py` checks consistency.
* **Capacity / voltage:** only via the two helpers.
* **Other GoodWe families** (not ET): untested. Run the inventory and the
  supervised tests; do not arm if anything differs.
* The tuning constants in `gfc_logic.yaml` (1.3 / 3.0 / 2.4 / 0.35 / 0.55 /
  0.65 / reserve + 7 / 11:00) are documented in place; they worked on the
  reference system and are a sensible start. Change them only after a dry run
  shows a need, and run the tests afterwards.

## 9. Verification commands

```sh
pip install -r requirements-dev.txt
python -m pytest tests                 # logic + write path, no hardware needed
```

On the HA host after any change: `check_config`, then `template.reload` /
`automation.reload` (or a restart for new integrations).
