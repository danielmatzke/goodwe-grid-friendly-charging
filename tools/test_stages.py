#!/usr/bin/env python3
"""Supervised test: does the inverter honour INTERMEDIATE values of
battery_charge_current (stepped charging power instead of only 0/25 A)?

Sequence: baseline 25 A (reference, 3 min) -> 12 A (3 min) -> 6 A (3 min) -> 25 A.
Expected on a working system: charging power ≈ A × battery voltage
(reference system at 327 V: 12 A -> 3.91 kW measured vs. 3.92 expected,
6 A -> 1.94 kW vs. 1.96).

Before: switch OFF the HA automation "GFC GoodWe polling (every 2 min)".
Afterwards: switch it back ON.

Safety:
- Preconditions (SOC headroom, surplus, battery actively charging) are checked
  first; if not met -> exit 2 WITHOUT any write. Without --yes nothing is
  written in any case.
- Writes only on actual change, each verified by a read-back.
- Restore to 25 A in a finally block (up to 5 attempts, then alarm).
- Aborts on: grid import > 500 W while charging, SOC ≥ 97 %, surplus
  < 1500 W (measurement no longer meaningful).

    python3 tools/test_stages.py --host 192.168.1.50 --out local/stages.json [--yes]
"""
import argparse
import asyncio
import json
import statistics
import sys
from datetime import datetime

from common import connect, inverter_host, write_verified

SETTING = "battery_charge_current"
BASELINE = 25.0
STAGES = [12, 6]
MEASURE_S = 180
SAMPLE_S = 20
SOC_MAX = 85.0
SOC_ABORT = 97.0
SURPLUS_MIN = 4000.0
SURPLUS_ABORT = 1500.0
CHARGE_MIN = 2000.0


def make_logger(path):
    def log(obj):
        obj["ts"] = datetime.now().isoformat(timespec="seconds")
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    return log


async def measure(inv, label, log):
    rt = await inv.read_runtime_data()
    m = {"event": "sample", "label": label, "ppv": rt.get("ppv"), "pbattery1": rt.get("pbattery1"),
         "soc": rt.get("battery_soc"), "grid": rt.get("active_power"), "load": rt.get("load_ptotal"),
         "vbattery": rt.get("vbattery1"), "ibattery": rt.get("ibattery1")}
    log(m)
    return m


async def run_stage(inv, ampere, log):
    if not await write_verified(inv, SETTING, ampere, log):
        log({"event": "abort", "reason": f"{ampere} A could not be written and verified"})
        return None
    await asyncio.sleep(15)
    charge, surplus = [], []
    for i in range(MEASURE_S // SAMPLE_S):
        await asyncio.sleep(SAMPLE_S)
        try:
            m = await measure(inv, f"{ampere}A_{i + 1}", log)
        except Exception as e:
            log({"event": "sample_error", "error": str(e)})
            continue
        p = -(m["pbattery1"] or 0)
        s = (m["ppv"] or 0) - (m["load"] or 0)
        charge.append(p)
        surplus.append(s)
        if (m["grid"] or 0) < -500 and p > 50:
            log({"event": "abort", "reason": "grid import while charging"})
            return "ABORT"
        if (m["soc"] or 0) >= SOC_ABORT:
            log({"event": "abort", "reason": f"SOC >= {SOC_ABORT} %"})
            return "ABORT"
        if s < SURPLUS_ABORT:
            log({"event": "abort", "reason": f"surplus < {SURPLUS_ABORT} W"})
            return "ABORT"
    if not charge:
        return None
    return {"ampere": ampere, "charge_median_w": statistics.median(charge),
            "surplus_median_w": statistics.median(surplus), "samples": len(charge)}


async def main(args):
    log = make_logger(args.log)
    inv = await connect(inverter_host(args.host))
    rt = await inv.read_runtime_data()
    soc = rt.get("battery_soc") or 0
    surplus = (rt.get("ppv") or 0) - (rt.get("load_ptotal") or 0)
    charging = -(rt.get("pbattery1") or 0)
    vbat = rt.get("vbattery1") or 0
    pre = {"event": "preconditions", "soc": soc, "surplus": surplus, "charging_w": charging,
           "vbattery": vbat, "ok_soc": soc < SOC_MAX, "ok_surplus": surplus >= SURPLUS_MIN,
           "ok_charging": charging >= CHARGE_MIN}
    log(pre)
    if not (pre["ok_soc"] and pre["ok_surplus"] and pre["ok_charging"]):
        log({"event": "no_test", "reason": "preconditions not met – nothing written"})
        return 2
    if not args.yes:
        log({"event": "dry", "reason": "--yes not given – nothing written"})
        return 0
    base = float(await inv.read_setting(SETTING))
    if base != BASELINE:
        log({"event": "no_test", "reason": f"baseline {base} != {BASELINE}"})
        return 3

    results, wrote = [], False
    try:
        ref = await run_stage(inv, int(BASELINE), log)
        wrote = True
        if ref == "ABORT":
            return 4
        if isinstance(ref, dict):
            results.append(ref)
        for a in STAGES:
            r = await run_stage(inv, a, log)
            if r == "ABORT":
                return 4
            if r is None:
                break
            results.append(r)
        return 0
    finally:
        if wrote:
            ok = await write_verified(inv, SETTING, BASELINE, log)
            log({"event": "restore", "ok": ok})
            if not ok:
                log({"event": "RESTORE_FAILED", "action": "CHECK battery_charge_current MANUALLY!"})
        for r in results:
            r["expected_w"] = round(r["ampere"] * vbat)
            r["limited"] = r["charge_median_w"] < r["expected_w"] * 1.15
        with open(args.out, "w") as f:
            json.dump({"time": datetime.now().isoformat(timespec="seconds"),
                       "vbattery": vbat, "stages": results}, f, indent=1)
        log({"event": "result", "stages": results})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="inverter IP (or env GFC_INVERTER_HOST)")
    p.add_argument("--log", default="stages_test.jsonl")
    p.add_argument("--out", default="stages_result.json")
    p.add_argument("--yes", action="store_true", help="really write (a human is watching, polling is off)")
    sys.exit(asyncio.run(main(p.parse_args())) or 0)
