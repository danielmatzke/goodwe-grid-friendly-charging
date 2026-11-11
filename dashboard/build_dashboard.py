#!/usr/bin/env python3
"""Observation dashboard: actual (grid-friendly control) vs. greedy simulation.

Fetches the Home Assistant history of every day since the control was armed,
reconstructs the limit actually written to the inverter from
charge_control_log.jsonl, and simulates a greedy world (charging without any
limit) with a simple battery model:

  charging:    efficiency 0.96, cap 5500 W, BMS taper from SOC 94 % (linear
               down to 150 W at 97 %)
  discharging: SOC drain = load / eta_discharge + standby_w, down to the floor

Coupling: sim == actual until the first real throttling takes effect (limit
< 25 A while there is surplus); re-coupled once |ΔSOC| ≤ 1 and the limit is
open again. Completed days are cached in the data file, because the HA
recorder purges history after 10 days by default — run this at least every
~9 days or days are lost for good.

Usage (see dashboard/README.md):
  HA_TOKEN=... python3 build_dashboard.py --ha-url http://HA:8123 \
      --start 2026-09-01 --log-ssh root@HA
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.html"
STEP_S = 120

CHARGE_ETA = 0.96
CHARGE_CAP_W, TAPER_START, TAPER_END, TAPER_MIN_W = 5500.0, 94.0, 97.0, 150.0


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ha-url", default=os.environ.get("HA_URL"), help="e.g. http://homeassistant.local:8123")
    p.add_argument("--token-file", help="file with a long-lived access token (default: env HA_TOKEN)")
    p.add_argument("--start", required=True, help="first day to show (YYYY-MM-DD), usually the arming day")
    p.add_argument("--tz", default="Europe/Berlin", help="IANA time zone of the installation")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--log-file", help="local copy of charge_control_log.jsonl")
    src.add_argument("--log-ssh", help="user@host to read the log via ssh")
    p.add_argument("--log-path", default="/config/charge_control_log.jsonl", help="log path on the HA host")
    p.add_argument("--log-sudo", action="store_true", help="prefix the remote cat with sudo")
    p.add_argument("--capacity-kwh", type=float, default=7.68, help="usable battery capacity")
    p.add_argument("--floor-soc", type=float, default=15.0, help="inverter discharge floor in %%")
    p.add_argument("--load-entity", default="sensor.house_consumption",
                   help="house load used by the simulation (default matches the reference calibration)")
    p.add_argument("--eta-discharge", type=float, default=0.60, help="discharge model: efficiency")
    p.add_argument("--standby-w", type=float, default=42.0, help="discharge model: standby W")
    p.add_argument("--data", default=str(HERE.parent / "dashboard-data" / "data.json"))
    p.add_argument("--out", default=str(HERE.parent / "dashboard-data" / "dashboard.html"))
    p.add_argument("--rebuild-all", action="store_true",
                   help="re-fetch cached days too (purged days are kept from the cache)")
    a = p.parse_args()
    if not a.ha_url:
        p.error("--ha-url or env HA_URL is required")
    token = Path(a.token_file).read_text().strip() if a.token_file else os.environ.get("HA_TOKEN")
    if not token:
        p.error("--token-file or env HA_TOKEN is required")
    a.token = token
    a.tzinfo = ZoneInfo(a.tz)
    return a


class HA:
    def __init__(self, url, token):
        self.url, self.token = url.rstrip("/"), token

    def get(self, path):
        req = urllib.request.Request(self.url + path, headers={"Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def history(self, entity, start_iso, end_iso):
        q = urllib.parse.quote
        d = self.get(f"/api/history/period/{q(start_iso)}?end_time={q(end_iso)}"
                     f"&filter_entity_id={entity}&minimal_response")
        return d[0] if d else []


def ts_of(row):
    return datetime.fromisoformat(row["last_changed"].replace("Z", "+00:00")).timestamp()


def to_slots(rows, slots):
    """Forward-filled 2-min slots; None as long as no value is known."""
    points = []
    for s in rows:
        st = s.get("state")
        if st in ("unknown", "unavailable", None):
            continue
        try:
            points.append((ts_of(s), float(st)))
        except ValueError:
            continue
    points.sort()
    out, i, last = [], 0, None
    for t in slots:
        while i < len(points) and points[i][0] <= t:
            last = points[i][1]
            i += 1
        out.append(last)
    return out


def wall_clock_slots(day, tz):
    """720 slots by local wall clock (00:00, 00:02, … 23:58) as UTC timestamps.
    The dashboard maps index -> time with a fixed 2 min; this keeps the axis
    correct on DST change days."""
    d = date.fromisoformat(day)
    midnight = datetime(d.year, d.month, d.day)
    return [(midnight + timedelta(seconds=k * STEP_S)).replace(tzinfo=tz).timestamp()
            for k in range(int(86400 / STEP_S))]


def read_log(args):
    if args.log_file:
        return Path(args.log_file).read_text()
    cmd = ("sudo " if args.log_sudo else "") + f"cat {args.log_path}"
    try:
        return subprocess.run(["ssh", "-o", "BatchMode=yes", args.log_ssh, cmd],
                              capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return ""


def write_events(log_text, tz):
    events = []
    for line in log_text.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") == "write_ok":
            # Log timestamps are naive local time -> interpret with the zone OF
            # that moment, not the current one (DST).
            ts = datetime.fromisoformat(e["ts"]).replace(tzinfo=tz).timestamp()
            events.append((ts, float(e["to"])))
    return sorted(events)


def limit_staircase(events, slots):
    """Inverter limit per slot from the write_ok events."""
    value, out, i, writes = 25.0, [], 0, 0
    day_start = slots[0]
    for ts, to in events:
        if ts <= day_start:
            value = to
    for t in slots:
        while i < len(events) and events[i][0] <= t:
            value = events[i][1]
            if events[i][0] > day_start:
                writes += 1
            i += 1
        out.append(value)
    return out, writes


def greedy_sim(pv, load, batt, soc, meter, limit, args):
    n, dt_h, cap_wh = len(pv), STEP_S / 3600.0, args.capacity_kwh * 1000
    s_batt, s_soc, s_meter = [None] * n, [None] * n, [None] * n
    coupled, soc_sim = True, None
    for k in range(n):
        if None in (pv[k], load[k], soc[k]):
            continue
        surplus = pv[k] - load[k]
        if soc_sim is None:
            soc_sim = soc[k]
        if coupled and limit[k] < 25 and surplus > 200:
            coupled = False
        if not coupled and abs(soc_sim - soc[k]) <= 1.0 and limit[k] == 25:
            coupled = True
        if coupled:
            soc_sim = soc[k]
            s_batt[k], s_soc[k], s_meter[k] = batt[k], soc[k], meter[k]
            continue
        if surplus > 0 and soc_sim < 100:
            if soc_sim < TAPER_START:
                lim = CHARGE_CAP_W
            elif soc_sim < TAPER_END:
                lim = max(TAPER_MIN_W, CHARGE_CAP_W * (TAPER_END - soc_sim) / (TAPER_END - TAPER_START))
            else:
                lim = TAPER_MIN_W
            p = min(surplus, lim)
            soc_sim = min(100.0, soc_sim + p * CHARGE_ETA * dt_h / cap_wh * 100)
            s_batt[k], s_meter[k] = -p, surplus - p
        elif surplus > 0:
            s_batt[k], s_meter[k] = 0.0, surplus
        else:
            deficit = -surplus
            if soc_sim > args.floor_soc:
                drain = (deficit / args.eta_discharge + args.standby_w) * dt_h / cap_wh * 100
                soc_sim = max(args.floor_soc, soc_sim - drain)
                s_batt[k], s_meter[k] = deficit, 0.0
            else:
                s_batt[k], s_meter[k] = 0.0, -deficit
        s_soc[k] = round(soc_sim, 2)
    return s_batt, s_soc, s_meter


def hhmm(k):
    return f"{(k * STEP_S) // 3600:02d}:{((k * STEP_S) % 3600) // 60:02d}"


def midday_kwh(meter):
    """Export in the grid stress window 11:00–15:00 (kWh). Less = more grid-friendly."""
    a, b = int(11 * 3600 / STEP_S), int(15 * 3600 / STEP_S)
    w = [v for v in meter[a:b] if v is not None and v > 0]
    return round(sum(w) * STEP_S / 3600 / 1000, 1)


def neighbourhood(meter, s_meter):
    """Our contribution at the moment of the neighbourhood peak.

    Assumption: the neighbourhood charges greedily and has the same weather –
    then our greedy curve is a proxy for their export profile. Negative = we
    relieve the sum peak, positive = we increase it."""
    pairs = [(k, i, s) for k, (i, s) in enumerate(zip(meter, s_meter)) if i is not None and s is not None]
    if not pairs:
        return {"delta": None, "time": "–", "peak": None}
    k, i, s = max(pairs, key=lambda p: p[2])
    if s < 1000:
        return {"delta": None, "time": "–", "peak": None}
    return {"delta": round((i - s) / 1000, 2), "peak": round(s / 1000, 2), "time": hhmm(k)}


def kpis(soc, s_soc, meter, s_meter, writes):
    def full_at(series):
        for k, v in enumerate(series):
            if v is not None and v >= 97.0:
                return hhmm(k)
        return "–"

    def peak(series):
        w = [v for v in series if v is not None]
        return round(max(w)) if w else 0

    return {"peak_actual": peak(meter), "peak_sim": peak(s_meter),
            "full_actual": full_at(soc), "full_sim": full_at(s_soc), "writes": writes,
            "midday_actual": midday_kwh(meter), "midday_sim": midday_kwh(s_meter),
            "neighbourhood": neighbourhood(meter, s_meter)}


def forecast_source(ha, day, start_iso, end_iso, tz):
    """Share of daylight slots (07–19) per source from sensor.pv_forecast_source.
    Returns None if the history is empty (no state change that day and the last
    row already purged) – resolved later by inherit_sources()."""
    try:
        rows = ha.history("sensor.pv_forecast_source", start_iso, end_iso)
    except Exception:
        rows = []
    slots = wall_clock_slots(day, tz)[int(7 * 3600 / STEP_S):int(19 * 3600 / STEP_S)]
    points = sorted((ts_of(s), s["state"]) for s in rows
                    if s.get("state") not in ("unknown", "unavailable", None))
    if not points:
        return None
    count, i, last = {}, 0, None
    for t in slots:
        while i < len(points) and points[i][0] <= t:
            last = points[i][1]
            i += 1
        if last:
            count[last] = count.get(last, 0) + 1
    n = max(1, sum(count.values()))
    return {"solcast": round(count.get("solcast", 0) / n, 2),
            "fallback": round(count.get("forecast_solar_fallback", 0) / n, 2),
            "main": max(count, key=count.get) if count else "unknown"}


def inherit_sources(data):
    """sensor.pv_forecast_source rarely changes. Once its last row is older than
    the recorder retention, the history of a day without a change comes back
    empty. Empty history means: no change that day -> the previous day's state
    still applies (a failure would have been visible as a change)."""
    prev = None
    for key in sorted(data):
        q = data[key].get("source")
        if q is None:
            q = data[key]["source"] = ({"solcast": 1.0, "fallback": 0.0, "main": "solcast"}
                                       if prev and prev["main"] in ("solcast", "forecast_solar_fallback")
                                       else {"solcast": 0.0, "fallback": 0.0,
                                             "main": prev["main"] if prev else "unknown"})
        prev = q


def fetch_day(ha, day, events, args):
    tz = args.tzinfo
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, tzinfo=tz).isoformat()
    nxt = d + timedelta(days=1)
    end_dt = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz) - timedelta(seconds=1)
    now = datetime.now().timestamp()
    complete = now >= end_dt.timestamp()
    slots = [t for t in wall_clock_slots(day, tz) if t <= now]
    ent = {"pv": "sensor.pv_power", "load": args.load_entity,
           "batt": "sensor.battery_power", "soc": "sensor.battery_state_of_charge",
           "meter": "sensor.meter_active_power_total"}
    raw = {k: ha.history(e, start, end_dt.isoformat()) for k, e in ent.items()}
    s = {k: to_slots(raw[k], slots) for k in raw}
    # Raw-value count as outage indicator: to_slots forward-fills, so a dead day
    # would otherwise look complete. The grid meter gets a new value on every
    # 2-min poll (~1300/day); the SOC barely changes on dark days.
    raw_count = len([r for r in raw["meter"] if r.get("state") not in ("unknown", "unavailable")])
    limit, writes = limit_staircase(events, slots)
    sb, ss, sm = greedy_sim(s["pv"], s["load"], s["batt"], s["soc"], s["meter"], limit, args)
    rnd = lambda series: [None if v is None else round(v) for v in series]
    return {"soc": s["soc"], "batt": rnd(s["batt"]), "meter": rnd(s["meter"]),
            "limit": limit, "sim_soc": ss, "sim_batt": rnd(sb), "sim_meter": rnd(sm),
            "kpi": kpis(s["soc"], ss, s["meter"], sm, writes), "complete": complete,
            "source": forecast_source(ha, day, start, end_dt.isoformat(), tz),
            "raw_count": raw_count,
            "gap": complete and raw_count < 400}


def main():
    args = parse_args()
    ha = HA(args.ha_url, args.token)
    data_file, out_file = Path(args.data), Path(args.out)
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(data_file.read_text()) if data_file.exists() else {}
    events = write_events(read_log(args), args.tzinfo)
    if not events:
        print("warning: no write_ok events found in the log", file=sys.stderr)
    today = date.today()
    day = date.fromisoformat(args.start)
    while day <= today:
        key = day.isoformat()
        if key not in data or not data[key].get("complete") or day == today or args.rebuild_all:
            print(f"fetch {key} ...", flush=True)
            new = fetch_day(ha, key, events, args)
            empty = not any(v is not None for v in new["soc"])
            if key in data and empty:
                # Purged by the recorder: never replace a cached day by an empty
                # fetch. A partially cached past day is marked complete.
                print(f"   {key}: history empty (purged) -> keeping cache", flush=True)
                if day < today and not data[key].get("complete"):
                    data[key]["complete"] = True
                    data[key]["truncated"] = True
            elif key not in data and empty and day < today and (today - day).days > 9:
                # Never cached and already purged: a gap of the observation, not
                # an outage of the system -> skip instead of showing "outage".
                print(f"   {key}: never cached, history purged -> skipped", flush=True)
            else:
                data[key] = new
        day += timedelta(days=1)
    inherit_sources(data)
    data_file.write_text(json.dumps(data))
    html = TEMPLATE.read_text().replace("__DATA__", json.dumps(data, separators=(",", ":")))
    out_file.write_text(html)
    print(f"OK: {len(data)} days -> {out_file} ({out_file.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
