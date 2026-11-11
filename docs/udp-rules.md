# Inverter communication rules (UDP) – binding

GoodWe ET inverters are reached over a local UDP protocol (port 8899, via the
WiFi/LAN dongle) with the open-source `goodwe` library. There is no official
API and no documentation of how the firmware handles concurrent access. The
following rules are binding for everything in this repository – code, tools
and agents.

1. **Only one UDP peer at a time.** The interface does not tolerate parallel
   sessions; a collision with the HA GoodWe integration (same interface) has an
   undefined outcome. Observed on the reference system: one manual query that
   collided with a poll froze all HA GoodWe sensors for ~12 minutes.
   * HA polling is reduced to **one refresh every 2 minutes** (polling disabled
     on the config entry + automation `time_pattern /2`).
   * Every own access is placed **between** the polls (see timing below) or
     made while the polling automation is off. After an own access, leave at
     least one poll tick of quiet before the next one.
2. **Write only on actual change.** Read first, write only if the value
   differs – protects the inverter's (possibly flash) memory.
3. **Every write is verified by a read-back.** Retry on mismatch; on a
   persistent mismatch raise an alarm and go to a safe state.
4. **Safety before optimisation.** Any intervention must have a guaranteed
   restore path (`finally` semantics in tests; release script in operation).
5. **Only `battery_charge_current` is ever written,** and only 0/6/12/18/25 A.

## Timing (measured on the reference system)

* The polling automation fires on even minutes. The GoodWe coordinator then
  exchanges data at about second :01 and :11 of that minute (498 of 499
  updates on even minutes).
* `charge_control.py` therefore avoids **even minute, seconds 0–17**, and the
  lead-in from second 55 of an odd minute. Maximum wait ~23 s.
* Manual tools use the stricter window **odd minute, seconds 5–30**, and never
  minutes 35–39 of an even hour (the reconcile writes at :37).
* Triggers based on the polled sensors fire right after a poll, i.e. at the
  start of an even minute – exactly when a naive "wait for odd minute" would
  wait longest. This is why the window logic avoids a short window instead of
  waiting for a long one.
