#!/usr/bin/env python3
"""Write path: sets the GoodWe setting battery_charge_current to an allowed stage.

Usage (from Home Assistant, see gfc_control.yaml):
    python3 /config/scripts/charge_control.py <A> [--emergency]
Allowed stages: 0 / 6 / 12 / 18 / 25 A.

Runs inside the Home Assistant container and uses the goodwe library that the
GoodWe integration already ships. Configure INVERTER_HOST below.

Safety rules implemented here (see docs/udp-rules.md):
- Write ONLY on actual change: read first, write only if different.
- Every write is verified by a read-back; up to 3 attempts, then ALARM.
- Avoids the poll-collision window of the 2-minute HA polling (even minute,
  seconds 0-17, plus the lead-in from second 55 of an odd minute). Max wait
  ~23 s, so the total runtime stays well below Home Assistant's hard 60 s
  shell_command timeout (the script always ends itself after 45 s).
- File lock against parallel UDP sessions (control / reconcile / release).
- Daily write budget from its own log: warning at 25, hard stop at 40
  writes per day (normal mode only).
- --emergency: no window wait (a collision is accepted, retries catch it),
  budget stop ignored (a safety write must never be refused).
- After EVERY run the state file is written; a command_line sensor in HA
  makes result and last read-back value visible.

Exit codes: 0 ok/skipped, 4 read-back failure, 5 invalid stage,
            6 budget exhausted (normal mode only), 7 connection error,
            8 locked, 9 configuration error.
"""
import asyncio
import datetime
import fcntl
import json
import os
import sys

import goodwe

# ── CONFIGURE ────────────────────────────────────────────────────────────────
# IP address of the inverter (WiFi/LAN dongle, UDP port 8899). Give the
# inverter a fixed DHCP lease. The environment variable is used by the tests.
INVERTER_HOST = os.environ.get("GFC_INVERTER_HOST", "CHANGE_ME")
INVERTER_FAMILY = "ET"  # goodwe family for ET/EH/BT/BH hybrid inverters
# ─────────────────────────────────────────────────────────────────────────────

SETTING = "battery_charge_current"
VALID = {0, 6, 12, 18, 25}
DATA_DIR = os.environ.get("GFC_DATA_DIR", "/config")
LOG = os.path.join(DATA_DIR, "charge_control_log.jsonl")
STATE = os.path.join(DATA_DIR, "charge_control_state.json")
LOCK = os.environ.get("GFC_LOCK_FILE",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)), ".charge_control.lock"))
BUDGET_WARN = 25
BUDGET_STOP = 40
# Hard self-limit: HA's shell_command kills the process after 60 s and then
# raises an error that aborts the calling automation. The script therefore
# ALWAYS ends itself earlier with a defined state.
DEADLINE_S = 45.0
T0 = datetime.datetime.now()


def time_left():
    return DEADLINE_S - (datetime.datetime.now() - T0).total_seconds()


def log(obj):
    obj["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(LOG, "a") as f:
        f.write(json.dumps(obj) + "\n")
    print(json.dumps(obj))
    return obj


def write_state(last_value, target, result, writes):
    try:
        with open(STATE, "w") as f:
            json.dump({"last_value": last_value, "target": target,
                       "result": result, "writes_today": writes,
                       "ts": datetime.datetime.now().isoformat(timespec="seconds")}, f)
    except OSError as e:
        log({"event": "state_write_error", "error": str(e)})


def writes_today():
    today = datetime.date.today().isoformat()
    n = 0
    try:
        with open(LOG) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("event") == "write_ok" and o.get("ts", "").startswith(today):
                    n += 1
    except FileNotFoundError:
        pass
    return n


def in_collision_window(now):
    """HA poll bursts happen in even minutes at ~seconds 1-15. Avoided: even
    minute seconds 0-17, plus the lead-in from second 55 of an odd minute (so a
    running exchange does not reach into the next burst)."""
    if now.minute % 2 == 0 and now.second <= 17:
        return True
    if now.minute % 2 == 1 and now.second >= 55:
        return True
    return False


async def wait_for_window():
    """Waits at most until 25 s are left – after that the write happens anyway
    despite the collision risk (retries catch it); a missed write would be
    worse than a possible collision."""
    for _ in range(30):
        if not in_collision_window(datetime.datetime.now()):
            return True
        if time_left() <= 25:
            log({"event": "window_skipped_deadline", "time_left_s": round(time_left(), 1)})
            return False
        await asyncio.sleep(1)
    return False


async def main():
    emergency = "--emergency" in sys.argv[2:]
    try:
        target = int(sys.argv[1])
    except (IndexError, ValueError):
        log({"event": "error", "reason": "missing/invalid argument", "argv": sys.argv[1:]})
        return 5
    if target not in VALID:
        log({"event": "error", "reason": f"stage {target} not allowed", "allowed": sorted(VALID)})
        return 5
    if INVERTER_HOST in ("", "CHANGE_ME"):
        log({"event": "error", "reason": "INVERTER_HOST not configured"})
        write_state("unknown", target, "config_error", writes_today())
        return 9

    n = writes_today()
    if n >= BUDGET_STOP and not emergency:
        log({"event": "budget_stop", "writes_today": n, "target": target})
        write_state("unknown", target, "budget_stop", n)
        return 6

    # Lock: normal mode waits briefly and gives up; emergency waits longer and
    # takes over anyway (collision + retries is better than no safety write).
    lockf = open(LOCK, "w")
    locked = True
    for _ in range(20 if emergency else 5):
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = False
            break
        except OSError:
            await asyncio.sleep(1)
    if locked and not emergency:
        log({"event": "locked", "target": target})
        write_state("unknown", target, "locked", n)
        return 8
    if locked:
        log({"event": "emergency_overrides_lock", "target": target})

    if not emergency:
        await wait_for_window()

    try:
        inv = await goodwe.connect(INVERTER_HOST, family=INVERTER_FAMILY, retries=3, timeout=2)
        current = float(await inv.read_setting(SETTING))
    except Exception as e:
        log({"event": "connection_error", "error": str(e), "emergency": emergency})
        write_state("unknown", target, "connection_error", n)
        return 7

    if current == float(target):
        log({"event": "skipped_unchanged", "value": current, "emergency": emergency})
        write_state(current, target, "ok_unchanged", n)
        return 0

    for attempt in range(1, 4):
        if attempt > 1 and time_left() < 12:
            log({"event": "retry_aborted_deadline", "attempt": attempt,
                 "time_left_s": round(time_left(), 1)})
            break
        try:
            await inv.write_setting(SETTING, target)
            await asyncio.sleep(1.0)
            rb = float(await inv.read_setting(SETTING))
        except Exception as e:
            log({"event": "write_error", "attempt": attempt, "error": str(e)})
            await asyncio.sleep(2)
            continue
        if rb == float(target):
            n += 1
            log({"event": "write_ok", "from": current, "to": target,
                 "attempt": attempt, "writes_today": n, "emergency": emergency})
            if n >= BUDGET_WARN:
                log({"event": "budget_warning", "writes_today": n})
            write_state(float(target), target, "ok_written", n)
            return 0
        log({"event": "readback_mismatch", "target": target, "actual": rb, "attempt": attempt})
        await asyncio.sleep(2)

    log({"event": "ALARM_readback_failed", "target": target, "emergency": emergency})
    write_state("unknown", target, "ALARM_readback", n)
    return 4


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
