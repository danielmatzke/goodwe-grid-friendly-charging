"""Tests for the write path homeassistant/scripts/charge_control.py with a fake
inverter (no network, no goodwe installation needed)."""
import asyncio
import datetime
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "homeassistant" / "scripts" / "charge_control.py"


class FakeInverter:
    def __init__(self, value=25.0, sticky=False, fail_connect=False):
        self.value, self.sticky, self.writes = value, sticky, []

    async def read_setting(self, setting):
        assert setting == "battery_charge_current"
        return self.value

    async def write_setting(self, setting, value):
        assert setting == "battery_charge_current"
        self.writes.append(value)
        if not self.sticky:            # sticky = inverter ignores the write
            self.value = float(value)


@pytest.fixture
def cc(tmp_path, monkeypatch):
    """Load charge_control.py fresh with a fake goodwe module and temp paths."""
    state = {"inv": FakeInverter(), "fail": False}

    async def connect(host, family, retries, timeout):
        if state["fail"]:
            raise OSError("timeout")
        return state["inv"]

    monkeypatch.setitem(sys.modules, "goodwe", types.SimpleNamespace(connect=connect))
    monkeypatch.setenv("GFC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GFC_LOCK_FILE", str(tmp_path / "lock"))
    monkeypatch.setenv("GFC_INVERTER_HOST", "192.0.2.10")
    spec = importlib.util.spec_from_file_location("charge_control_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(mod, "in_collision_window", lambda now: False)

    def run(*argv):
        monkeypatch.setattr(sys, "argv", ["charge_control.py", *map(str, argv)])
        return asyncio.run(mod.main())

    mod.run, mod.state, mod.tmp = run, state, tmp_path
    return mod


def events(mod):
    return [json.loads(line)["event"] for line in (mod.tmp / "charge_control_log.jsonl").read_text().splitlines()]


def result(mod):
    return json.loads((mod.tmp / "charge_control_state.json").read_text())


def test_unchanged_value_is_not_written(cc):
    assert cc.run(25) == 0
    assert cc.state["inv"].writes == []
    assert events(cc) == ["skipped_unchanged"]
    assert result(cc)["result"] == "ok_unchanged"


def test_change_is_written_and_read_back(cc):
    assert cc.run(12) == 0
    assert cc.state["inv"].writes == [12]
    assert events(cc) == ["write_ok"]
    r = result(cc)
    assert (r["result"], r["last_value"], r["writes_today"]) == ("ok_written", 12.0, 1)


def test_failed_read_back_retries_then_alarms(cc):
    cc.state["inv"] = FakeInverter(sticky=True)
    assert cc.run(0) == 4
    assert len(cc.state["inv"].writes) == 3
    assert events(cc)[-1] == "ALARM_readback_failed"
    assert result(cc)["result"] == "ALARM_readback"


def test_invalid_stage_is_rejected(cc):
    assert cc.run(10) == 5
    assert cc.state["inv"].writes == []


def test_unconfigured_host_is_rejected(cc):
    cc.INVERTER_HOST = "CHANGE_ME"
    assert cc.run(12) == 9
    assert result(cc)["result"] == "config_error"


def test_connection_error_is_reported(cc):
    cc.state["fail"] = True
    assert cc.run(12) == 7
    assert result(cc)["result"] == "connection_error"


def _fill_budget(cc, n):
    today = datetime.date.today().isoformat()
    with open(cc.tmp / "charge_control_log.jsonl", "w") as f:
        for _ in range(n):
            f.write(json.dumps({"event": "write_ok", "ts": f"{today}T10:00:00"}) + "\n")


def test_budget_stop_blocks_normal_writes(cc):
    _fill_budget(cc, 40)
    assert cc.run(12) == 6
    assert cc.state["inv"].writes == []


def test_emergency_path_ignores_budget_stop(cc):
    _fill_budget(cc, 40)
    assert cc.run(0, "--emergency") == 0
    assert cc.state["inv"].writes == [0]


def test_collision_window():
    spec = importlib.util.spec_from_file_location("cc_window", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("goodwe", types.SimpleNamespace(connect=None))
    spec.loader.exec_module(mod)
    t = datetime.datetime(2026, 1, 1, 12, 0, 0)
    assert mod.in_collision_window(t.replace(minute=10, second=5))       # even minute, poll burst
    assert not mod.in_collision_window(t.replace(minute=10, second=30))
    assert not mod.in_collision_window(t.replace(minute=11, second=20))
    assert mod.in_collision_window(t.replace(minute=11, second=56))      # lead-in to next burst
