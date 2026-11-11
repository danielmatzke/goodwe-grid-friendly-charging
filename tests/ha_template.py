"""Minimal emulation of the Home Assistant template environment for tests.

Implements just what the GFC templates use: states(), is_state(),
state_attr(), has_value(), now(), utcnow(), as_datetime(), today_at(),
timedelta, the `match`/`search` tests and `this`. Values behave like in HA:
states are strings, missing entities are 'unknown'.

It is NOT a full HA re-implementation. It exists to pin down the decision
logic of the packages so a change (or a port to another installation) can be
checked without a running Home Assistant.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jinja2
import yaml

PACKAGES = Path(__file__).resolve().parent.parent / "homeassistant" / "packages"


class _Loader(yaml.SafeLoader):
    pass


for _tag in ("!secret", "!include", "!include_dir_named", "!include_dir_merge_named",
             "!include_dir_list", "!include_dir_merge_list", "!env_var"):
    _Loader.add_constructor(_tag, lambda loader, node: f"<{node.tag} {loader.construct_scalar(node)}>")


def load_yaml(path):
    with open(path) as f:
        return yaml.load(f, Loader=_Loader)


def load_packages():
    return {p.stem: load_yaml(p) for p in sorted(PACKAGES.glob("*.yaml"))}


def template_entities(packages=None):
    """Map entity_id -> template config for every template sensor/binary_sensor."""
    packages = packages or load_packages()
    out = {}
    for pkg in packages.values():
        for block in pkg.get("template", []) or []:
            for domain in ("sensor", "binary_sensor"):
                for cfg in block.get(domain, []) or []:
                    out[f"{domain}.{slug(cfg['name'])}"] = cfg
    return out


def slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@dataclass
class This:
    state: str = "unknown"
    attributes: dict = field(default_factory=dict)


class World:
    """A snapshot of entity states plus a fixed 'now'."""

    def __init__(self, now, states=None, attrs=None):
        self.now = now
        self.states = dict(states or {})
        self.attrs = {k: dict(v) for k, v in (attrs or {}).items()}

    def env(self, this=None):
        env = jinja2.Environment(undefined=jinja2.Undefined)
        env.tests["match"] = lambda v, p, ignorecase=False: bool(
            re.match(p, str(v), re.IGNORECASE if ignorecase else 0))
        env.tests["search"] = lambda v, p, ignorecase=False: bool(
            re.search(p, str(v), re.IGNORECASE if ignorecase else 0))
        g = env.globals
        g["states"] = lambda e: self.states.get(e, "unknown")
        g["is_state"] = lambda e, v: self.states.get(e) == v
        g["state_attr"] = lambda e, a: self.attrs.get(e, {}).get(a)
        g["has_value"] = lambda e: self.states.get(e, "unknown") not in ("unknown", "unavailable")
        g["now"] = lambda: self.now
        g["utcnow"] = lambda: self.now.astimezone(timezone.utc)
        g["as_datetime"] = _as_datetime
        g["today_at"] = lambda t="00:00": self.now.replace(
            hour=int(t.split(":")[0]), minute=int(t.split(":")[1]), second=0, microsecond=0)
        g["timedelta"] = timedelta
        g["this"] = this or This()
        return env

    def render(self, template, this=None):
        return self.env(this).from_string(template).render().strip()


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def render_entity(world, cfg, key="state", this_state="unknown"):
    """Render the `state` (or `availability`) template of a template entity."""
    tpl = cfg.get(key)
    if tpl is None:
        return None
    return world.render(tpl, This(state=this_state))
