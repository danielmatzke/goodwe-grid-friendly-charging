#!/usr/bin/env python3
"""Supervised test: does battery_charge_current = 0 A pause charging WITHOUT
grid import, while discharging keeps working?

Sequence: baseline 25 A -> 0 A (observe 3 min) -> restore 25 A (observe 1.5 min).

Run it on a sunny late morning with PV surplus, while a human is watching.
Before: switch OFF the HA automation "GFC GoodWe polling (every 2 min)"
(exclusive UDP session). Afterwards: switch it back ON.

Safety:
- Without --yes only the preconditions are checked; nothing is written.
- Writes only on actual change, each verified by a read-back.
- Restore to 25 A in a finally block (up to 5 attempts, then a loud alarm).
- Aborts and restores on grid import > 500 W while charging > 50 W.

    python3 tools/test_pause.py --host 192.168.1.50 --log local/pause_test.jsonl [--yes]
"""
import argparse
import asyncio
import json
import sys
from datetime import datetime

from common import connect, inverter_host, write_verified

SETTING = "battery_charge_current"
BASELINE = 25.0
OBSERVE_PAUSED_S = 180
OBSERVE_RESTORED_S = 90
SAMPLE_S = 12


def make_logger(path):
    def log(obj):
        obj["ts"] = datetime.now().isoformat(timespec="seconds")
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    return log


async def sample(inv, label, log):
    rt = await inv.read_runtime_data()
    s = {"event": "sample", "label": label, "ppv": rt.get("ppv"), "pbattery1": rt.get("pbattery1"),
         "soc": rt.get("battery_soc"), "grid": rt.get("active_power"),
         "battery_mode": rt.get("battery_mode_label"), "charge_limit_a": rt.get("battery_charge_limit")}
    log(s)
    return s


async def main(args):
    log = make_logger(args.log)
    inv = await connect(inverter_host(args.host))
    log({"event": "connected", "model": inv.model_name})
    baseline = float(await inv.read_setting(SETTING))
    log({"event": "baseline", "setting": SETTING, "value": baseline})
    if baseline != BASELINE:
        log({"event": "abort", "reason": f"baseline {baseline} != expected {BASELINE}"})
        return 2
    s = await sample(inv, "before", log)
    if not args.yes:
        log({"event": "dry", "reason": "--yes not given: preconditions only, nothing written",
             "charging_w": -(s["pbattery1"] or 0)})
        return 0

    wrote = False
    try:
        if not await write_verified(inv, SETTING, 0, log):
            log({"event": "abort", "reason": "0 A could not be written and verified"})
            return 3
        wrote = True
        n = OBSERVE_PAUSED_S // SAMPLE_S
        for i in range(n):
            await asyncio.sleep(SAMPLE_S)
            try:
                s = await sample(inv, f"paused_{i + 1}/{n}", log)
            except Exception as e:
                log({"event": "sample_error", "error": str(e)})
                continue
            if (s["grid"] is not None and s["pbattery1"] is not None
                    and s["grid"] < -500 and s["pbattery1"] < -50):
                log({"event": "abort", "reason": "grid import while charging – restoring"})
                return 4
        return 0
    finally:
        if wrote:
            ok = await write_verified(inv, SETTING, BASELINE, log)
            log({"event": "restore", "ok": ok})
            if not ok:
                log({"event": "RESTORE_FAILED", "action": "CHECK battery_charge_current MANUALLY!"})
            else:
                for i in range(OBSERVE_RESTORED_S // SAMPLE_S):
                    await asyncio.sleep(SAMPLE_S)
                    try:
                        await sample(inv, f"restored_{i + 1}", log)
                    except Exception as e:
                        log({"event": "sample_error", "error": str(e)})


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="inverter IP (or env GFC_INVERTER_HOST)")
    p.add_argument("--log", default="pause_test.jsonl")
    p.add_argument("--yes", action="store_true", help="really write (a human is watching, polling is off)")
    sys.exit(asyncio.run(main(p.parse_args())) or 0)
