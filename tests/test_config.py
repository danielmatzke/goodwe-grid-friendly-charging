"""Consistency checks for the Home Assistant packages.

Every entity referenced anywhere in the packages must either be defined by
the packages themselves or be one of the EXTERNAL source entities documented
in AGENTS.md. If you rename a source entity for your installation, update
EXTERNAL here as well.
"""
import json
import re

from ha_template import load_packages, slug

EXTERNAL = {
    # GoodWe integration
    "sensor.battery_state_of_charge", "sensor.battery_power", "sensor.pv_power",
    "sensor.load", "sensor.meter_active_power_total",
    # Forecast.Solar integration
    "sensor.energy_production_today_remaining",
    # Home Assistant core
    "sun.sun",
}
REF = re.compile(r"\b((?:sensor|binary_sensor|input_boolean|input_number|automation|script|"
                 r"shell_command|sun)\.[a-z0-9_]+)\b")


def defined_entities(pkgs):
    out = set()
    for pkg in pkgs.values():
        for dom in ("input_number", "input_boolean", "script", "shell_command"):
            out |= {f"{dom}.{k}" for k in (pkg.get(dom) or {})}
        for a in pkg.get("automation") or []:
            out.add(f"automation.{slug(a['alias'])}")
        for s in pkg.get("sensor") or []:
            out.add(f"sensor.{slug(s['name'])}")
        for r in pkg.get("rest") or []:
            out |= {f"sensor.{slug(s['name'])}" for s in r.get("sensor", [])}
        for c in pkg.get("command_line") or []:
            out |= {f"sensor.{slug(c['sensor']['name'])}"} if "sensor" in c else set()
        for block in pkg.get("template") or []:
            for dom in ("sensor", "binary_sensor"):
                out |= {f"{dom}.{slug(e['name'])}" for e in block.get(dom, [])}
    return out


def without_actions(o):
    """Drop `action: domain.service` values – those are services, not entities."""
    if isinstance(o, dict):
        return {k: without_actions(v) for k, v in o.items() if k != "action"}
    if isinstance(o, list):
        return [without_actions(v) for v in o]
    return o


def test_every_referenced_entity_exists():
    pkgs = load_packages()
    defined = defined_entities(pkgs)
    text = json.dumps(without_actions(pkgs))
    referenced = set(REF.findall(text))
    unknown = sorted(referenced - defined - EXTERNAL)
    assert not unknown, f"referenced but not defined/documented: {unknown}"


def test_control_ships_disarmed():
    pkgs = load_packages()
    control = next(a for a in pkgs["gfc_control"]["automation"] if a["id"] == "gfc_control")
    assert control.get("initial_state") is False, \
        "the published package must ship with the control disarmed (initial_state: false)"


def test_stage_lists_are_consistent():
    """The allowed stages appear in the template, the automations and the script."""
    pkgs = load_packages()
    text = json.dumps(pkgs)
    assert text.count('[\\"0\\",\\"6\\",\\"12\\",\\"18\\",\\"25\\"]') >= 2
    script = (__import__("pathlib").Path(__file__).resolve().parent.parent
              / "homeassistant" / "scripts" / "charge_control.py").read_text()
    assert "VALID = {0, 6, 12, 18, 25}" in script
