#!/usr/bin/env python3
"""READ-ONLY diagnostic: battery current as measured by the inverter
(ibattery1/pbattery1) vs. as reported by the battery BMS (bms_bat_current).

On the reference system the inverter's battery-power reading is off by
≈110–150 W towards "charging" at low power (quantised in ~31 W steps), which
also corrupts the library-computed house_consumption. Use this to check your
own system. One UDP session in the safe window, 3 samples ~6 s apart.

    python3 tools/bms_compare.py --host 192.168.1.50
"""
import argparse
import asyncio
import datetime

from common import connect, inverter_host, wait_for_safe_window


async def main(args):
    if not args.now:
        wait_for_safe_window()
    inv = await connect(inverter_host(args.host))
    rows = []
    for i in range(3):
        rt = await inv.read_runtime_data()
        bms_i = await inv.read_setting("bms_bat_current")
        bms_u = await inv.read_setting("bms_bat_voltage")
        rows.append((datetime.datetime.now().strftime("%H:%M:%S"), rt.get("vbattery1"),
                     rt.get("ibattery1"), rt.get("pbattery1"), rt.get("battery_soc"), bms_u, bms_i,
                     rt.get("load_ptotal"), rt.get("active_power"), rt.get("ppv")))
        if i < 2:
            await asyncio.sleep(5)
    print(f"{'time':9s}{'INV U':>7s}{'INV I':>6s}{'INV P':>7s}{'SOC':>5s} |"
          f"{'BMS U':>7s}{'BMS I':>6s}{'BMS P':>7s} |{'load':>6s}{'grid':>7s}{'PV':>6s}")
    for r in rows:
        bms_p = round(r[5] * r[6]) if all(isinstance(v, (int, float)) for v in (r[5], r[6])) else "–"
        print(f"{r[0]:9s}{r[1]!s:>7s}{r[2]!s:>6s}{r[3]!s:>7s}{r[4]!s:>5s} |"
              f"{r[5]!s:>7s}{r[6]!s:>6s}{bms_p!s:>7s} |{r[7]!s:>6s}{r[8]!s:>7s}{r[9]!s:>6s}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="inverter IP (or env GFC_INVERTER_HOST)")
    p.add_argument("--now", action="store_true",
                   help="skip the safe-window wait (only if HA does not poll the inverter)")
    asyncio.run(main(p.parse_args()))
