"""Shared helpers for the bootstrap tools.

All tools talk to the inverter directly over UDP (port 8899) with the goodwe
library. The UDP interface does not tolerate parallel sessions, so:

* read-only tools wait for the safe window: odd minute, seconds 5–30, and
  never at minutes 35–39 of an even hour (the GFC reconcile writes at :37);
* tools that run for several minutes (the write tests) require the HA polling
  automation "GFC GoodWe polling (every 2 min)" to be switched OFF for the
  duration of the test – and back ON afterwards.
"""
import asyncio
import datetime
import os
import time

import goodwe


def inverter_host(cli_value):
    host = cli_value or os.environ.get("GFC_INVERTER_HOST")
    if not host:
        raise SystemExit("inverter host missing: pass --host or set GFC_INVERTER_HOST")
    return host


def in_safe_window(now=None):
    n = now or datetime.datetime.now()
    if n.hour % 2 == 0 and 35 <= n.minute <= 39:
        return False
    return n.minute % 2 == 1 and 5 <= n.second <= 30


def wait_for_safe_window():
    while not in_safe_window():
        time.sleep(1)


async def connect(host, family="ET"):
    return await goodwe.connect(host, family=family, retries=3, timeout=2)


async def write_verified(inv, setting, value, log, attempts=5):
    """Write only on change; verify every write by a read-back."""
    current = float(await inv.read_setting(setting))
    if current == float(value):
        log({"event": "write_skipped_unchanged", "setting": setting, "value": current})
        return True
    for i in range(1, attempts + 1):
        await inv.write_setting(setting, value)
        await asyncio.sleep(1.5)
        rb = float(await inv.read_setting(setting))
        ok = rb == float(value)
        log({"event": "write", "setting": setting, "target": value, "readback": rb,
             "attempt": i, "ok": ok})
        if ok:
            return True
        await asyncio.sleep(2)
    return False
