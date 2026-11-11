"""Scenario tests for the decision logic in homeassistant/packages.

Run with:  python -m pytest tests
They render the real templates from the package files in a small emulation of
the Home Assistant template engine (tests/ha_template.py).
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ha_template import World, render_entity, template_entities

TZ = ZoneInfo("Europe/Berlin")
ENT = template_entities()


def world(hour=13, minute=0, **overrides):
    now = datetime(2026, 6, 15, hour, minute, tzinfo=TZ)
    sunset = now.replace(hour=19, minute=0)
    if now >= sunset:
        sunset += timedelta(days=1)
    sunrise = now.replace(hour=6, minute=0)
    if now >= sunrise:
        sunrise += timedelta(days=1)
    states = {
        "sun.sun": "above_horizon" if 6 <= hour < 19 else "below_horizon",
        "sensor.battery_state_of_charge": "50",
        "sensor.battery_missing_kwh": "3.0",
        "sensor.pv_remaining_usable": "6.0",
        "input_number.min_reserve_soc": "20",
        "input_number.battery_usable_kwh": "7.68",
        "input_number.battery_nominal_voltage_v": "327",
        "input_number.dark_day_surplus_w": "2500",
        "input_boolean.gfc_emergency_stop": "off",
        "binary_sensor.gfc_dark_day": "off",
        "binary_sensor.gfc_reserve_mode": "off",
    }
    attrs = {"sun.sun": {"next_setting": sunset.isoformat(), "next_rising": sunrise.isoformat()},
             "sensor.charge_control_result": {"last_value": 25.0}}
    for k, v in overrides.items():
        if k.startswith("attr__"):
            ent, attr = k[6:].split("__")
            attrs.setdefault(ent.replace("_", ".", 1), {})[attr] = v
        else:
            states[k.replace("__", ".", 1)] = v
    return World(now, states, attrs)


def stage(w, this="unknown"):
    return render_entity(w, ENT["sensor.battery_charge_target_a"], "state", this)


# ── battery_charge_target_a ──────────────────────────────────────────────────

def test_emergency_stop_forces_0_even_at_night():
    assert stage(world(hour=23, input_boolean__gfc_emergency_stop="on")) == "0"


def test_night_opens_limit():
    assert stage(world(hour=22)) == "25"


def test_below_reserve_opens_limit():
    assert stage(world(sensor__battery_state_of_charge="15")) == "25"


def test_dark_day_opens_limit():
    assert stage(world(binary_sensor__gfc_dark_day="on")) == "25"


def test_forecast_barely_covers_missing_opens_limit():
    # usable 3.9 <= missing 3.0 × 1.3
    assert stage(world(sensor__pv_remaining_usable="3.9")) == "25"


def test_almost_full_dead_band():
    w = world(sensor__battery_missing_kwh="0.45", sensor__pv_remaining_usable="10")
    assert stage(w, this="25") == "25"      # stays open until > 0.55
    assert stage(w, this="12") == "6"       # enters only at <= 0.35


def test_morning_headroom_and_its_dead_band():
    w = world(hour=9, sensor__pv_remaining_usable="9.5")    # 3.17 × missing
    assert stage(w, this="6") == "0"
    w = world(hour=9, sensor__pv_remaining_usable="8.0")    # 2.67 × missing
    assert stage(w, this="0") == "0"        # leaves only below 2.4 ×
    assert stage(w, this="6") != "0"        # enters only at 3.0 ×


def test_headroom_only_before_11():
    assert stage(world(hour=12, sensor__pv_remaining_usable="20")) != "0"


def test_morning_hold_keeps_25_below_reserve_plus_7():
    w = world(hour=9, sensor__battery_state_of_charge="25", sensor__pv_remaining_usable="20")
    assert stage(w, this="25") == "25"      # 25 < 20 + 7
    assert stage(w, this="6") == "0"        # never switches TO 25 by itself


def test_trajectory_picks_smallest_sufficient_stage():
    # 13:00, sunset 19:00 -> 6 h × 0.65; p_need = missing / 0.96 / 3.9
    assert stage(world(sensor__battery_missing_kwh="3.0", sensor__pv_remaining_usable="6")) == "6"
    assert stage(world(sensor__battery_missing_kwh="6.0", sensor__pv_remaining_usable="12")) == "6"
    w = world(hour=16, sensor__battery_missing_kwh="6.0", sensor__pv_remaining_usable="12")
    assert stage(w) == "12"                 # 3 h left: p_need = 6 / 0.96 / 1.95 = 3.2 kW
    w = world(hour=17, sensor__battery_missing_kwh="6.0", sensor__pv_remaining_usable="12")
    assert stage(w) == "18"                 # 2 h left: 4.8 kW
    w = world(hour=18, sensor__battery_missing_kwh="6.0", sensor__pv_remaining_usable="12")
    assert stage(w) == "25"                 # 1 h left: 9.6 kW


def test_stage_thresholds_follow_battery_voltage():
    w = world(hour=16, sensor__battery_missing_kwh="4.0", sensor__pv_remaining_usable="10")
    # p_need = 4 / 0.96 / (3 × 0.65) = 2.14 kW
    assert stage(w) == "12"                                         # 6 A at 327 V = 1.96 kW
    w.states["input_number.battery_nominal_voltage_v"] = "400"
    assert stage(w) == "6"                                          # 6 A at 400 V = 2.40 kW


def test_hysteresis_survives_reload_via_read_back_value():
    w = world(hour=9, sensor__pv_remaining_usable="8.0",
              attr__sensor_charge_control_result__last_value=0.0)
    assert stage(w, this="unknown") == "0"


def test_unavailable_soc_makes_stage_unavailable():
    w = world(sensor__battery_state_of_charge="unavailable")
    assert render_entity(w, ENT["sensor.battery_charge_target_a"], "availability") == "False"


# ── binary sensors ───────────────────────────────────────────────────────────

def dark(w, this="off"):
    return render_entity(w, ENT["binary_sensor.gfc_dark_day"], "state", this)


def test_dark_day_grace_period_and_hysteresis():
    assert dark(world(hour=10, minute=15, sensor__pv_surplus_max_3h="500")) == "False"
    assert dark(world(hour=10, minute=30, sensor__pv_surplus_max_3h="500")) == "True"
    w = world(hour=12, sensor__pv_surplus_max_3h="2800")
    assert dark(w, "off") == "False"
    assert dark(w, "on") == "True"          # leaves only above 2500 × 1.2


def test_dark_day_fails_safe_when_statistics_missing():
    assert dark(world(hour=12, sensor__pv_surplus_max_3h="unavailable")) == "False"


def test_reserve_mode_hysteresis():
    cfg = ENT["binary_sensor.gfc_reserve_mode"]
    w = world(sensor__battery_state_of_charge="21")
    assert render_entity(w, cfg, "state", "off") == "False"
    assert render_entity(w, cfg, "state", "on") == "True"     # until reserve + 2


def test_grid_import_while_charging():
    cfg = ENT["binary_sensor.gfc_grid_import_while_charging"]
    w = world(sensor__meter_active_power_total="-400", sensor__battery_power="-300")
    assert render_entity(w, cfg) == "True"
    w = world(sensor__meter_active_power_total="-400", sensor__battery_power="300")
    assert render_entity(w, cfg) == "False"                   # discharging is fine


# ── forecast chain ───────────────────────────────────────────────────────────

def solcast_world(age_h=1.0, hour=13):
    w = world(hour=hour)
    first_end = w.now.replace(minute=30)                      # running period 13:00–13:30
    periods = [{"period_end": (first_end + timedelta(minutes=30 * i)).isoformat(), "pv_estimate": 2.0}
               for i in range(24)]                            # until 01:00 tomorrow
    w.states["sensor.solcast_forecast_raw"] = (w.now - timedelta(hours=age_h)).isoformat()
    w.attrs["sensor.solcast_forecast_raw"] = {"forecasts": periods}
    return w


def test_solcast_live_rest_counts_running_period_pro_rata_and_stops_at_midnight():
    w = solcast_world()
    # running period: 30 min left of 30 -> 1.0 kWh; then 13:30 … 24:00 = 21 periods × 1.0 kWh
    assert render_entity(w, ENT["sensor.solcast_remaining_today_live"]) == "22.0"
    w.now = w.now.replace(minute=15)
    assert render_entity(w, ENT["sensor.solcast_remaining_today_live"]) == "21.5"


def test_solcast_live_unavailable_when_stale():
    cfg = ENT["sensor.solcast_remaining_today_live"]
    assert render_entity(solcast_world(age_h=9.5), cfg, "availability") == "True"
    assert render_entity(solcast_world(age_h=10.5), cfg, "availability") == "False"


@pytest.mark.parametrize("switch,live,fs,expected_src,expected_val", [
    ("on", "12.5", "8.0", "solcast", "12.5"),
    ("on", "unavailable", "8.0", "forecast_solar_fallback", "10.4"),
    ("off", "12.5", "8.0", "forecast_solar", "10.4"),
    ("on", "unavailable", "unavailable", "none", None),
])
def test_forecast_switch_and_fallback(switch, live, fs, expected_src, expected_val):
    w = world(input_boolean__gfc_use_solcast=switch, sensor__solcast_remaining_today_live=live,
              sensor__energy_production_today_remaining=fs, input_number__pv_correction_factor="1.3")
    assert render_entity(w, ENT["sensor.pv_forecast_source"]) == expected_src
    cfg = ENT["sensor.pv_remaining_corrected"]
    if expected_val is None:
        assert render_entity(w, cfg, "availability") == "False"
    else:
        assert render_entity(w, cfg) == expected_val


def test_usable_subtracts_expected_daylight_load():
    w = world(sensor__pv_remaining_corrected="10", sensor__house_consumption_daylight_mean_7d="500")
    # 13:00 -> 19:00 = 6 h × 0.5 kW = 3 kWh
    assert render_entity(w, ENT["sensor.pv_remaining_usable"]) == "7.0"


def test_missing_kwh_uses_configured_capacity():
    w = world(sensor__battery_state_of_charge="75", input_number__battery_usable_kwh="10")
    assert render_entity(w, ENT["sensor.battery_missing_kwh"]) == "2.5"
