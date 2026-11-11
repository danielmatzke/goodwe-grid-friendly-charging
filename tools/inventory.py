#!/usr/bin/env python3
"""READ-ONLY inventory of a GoodWe hybrid inverter.

Reads device info, a runtime-data excerpt and ALL settings and stores them as
a baseline JSON (keep it – it documents the original settings before you
change anything). No write access.

    python3 tools/inventory.py --host 192.168.1.50 --out local/baseline.json

The output contains the serial number of your inverter – do not commit it.
"""
import argparse
import asyncio
import json
from datetime import datetime

from common import connect, inverter_host, wait_for_safe_window

KEYS = ["ppv", "battery_soc", "pbattery1", "vbattery1", "ibattery1", "active_power",
        "house_consumption", "load_ptotal", "meter_active_power_total", "battery_mode_label",
        "work_mode_label", "grid_mode_label", "battery_charge_limit", "battery_discharge_limit"]


async def main(args):
    if not args.now:
        wait_for_safe_window()
    inv = await connect(inverter_host(args.host))
    info = {k: getattr(inv, k, None) for k in
            ("model_name", "serial_number", "firmware", "arm_firmware", "rated_power")}
    print("=== Device ===")
    print(json.dumps(info, indent=1, default=str))
    runtime = await inv.read_runtime_data()
    print("\n=== Runtime data (excerpt) ===")
    rt = {k: runtime[k] for k in KEYS if k in runtime}
    for k, v in rt.items():
        print(f"  {k}: {v}")
    print("\n=== Settings (read) ===")
    settings = {}
    for s in inv.settings():
        try:
            v = await inv.read_setting(s.id_)
            settings[s.id_] = {"value": str(v), "name": s.name, "unit": s.unit}
            print(f"  {s.id_} = {v}  ({s.name}, {s.unit})")
        except Exception as e:
            settings[s.id_] = {"error": str(e)}
            print(f"  {s.id_} = ERROR: {e}")
        await asyncio.sleep(0.1)
    if "battery_charge_current" not in settings:
        print("\n!! The setting battery_charge_current was not found – this project will not work "
              "with this inverter/firmware.")
    with open(args.out, "w") as f:
        json.dump({"timestamp": datetime.now().isoformat(), "info": info,
                   "runtime_excerpt": {k: str(v) for k, v in rt.items()},
                   "settings": settings}, f, indent=1, default=str)
    print(f"\nBaseline saved: {args.out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="inverter IP (or env GFC_INVERTER_HOST)")
    p.add_argument("--out", default="baseline.json")
    p.add_argument("--now", action="store_true",
                   help="skip the safe-window wait (only if HA does not poll the inverter)")
    asyncio.run(main(p.parse_args()))
