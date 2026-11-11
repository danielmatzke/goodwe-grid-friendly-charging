# Observation dashboard (optional)

A single static HTML page that compares, day by day, the measured operation
with a simulation of the same day under greedy charging: charging power with
the written inverter limit, SOC, grid exchange, plus KPIs (export 11:00–15:00,
time of full charge, export peak, contribution to a greedy neighbourhood's
peak, forecast source, writes). "Ø all days" averages everything.

It runs on any machine that can reach Home Assistant (Python ≥ 3.9, standard
library only) and reads the write log via ssh or from a local copy.

```sh
export HA_TOKEN=...            # long-lived access token
python3 dashboard/build_dashboard.py \
    --ha-url http://homeassistant.local:8123 \
    --start 2026-09-01 \
    --log-ssh root@homeassistant.local     # or: --log-file local/charge_control_log.jsonl
```

Output: `dashboard-data/data.json` (cache) and `dashboard-data/dashboard.html`
(git-ignored – it contains your household's load profile).

**Run it at least every ~9 days.** The Home Assistant recorder keeps 10 days of
history by default; completed days are cached, purged days cannot be recovered.

## Options worth knowing

| Option | Default | Meaning |
|---|---|---|
| `--capacity-kwh` | 7.68 | usable battery capacity |
| `--floor-soc` | 15 | inverter discharge floor in % |
| `--tz` | Europe/Berlin | time zone of the installation (DST-correct axes) |
| `--load-entity` | `sensor.house_consumption` | load used by the simulation |
| `--eta-discharge`, `--standby-w` | 0.60, 42 | discharge model of the simulation |
| `--log-sudo` | off | prefix the remote `cat` with sudo |
| `--rebuild-all` | off | re-fetch cached days (purged days stay cached) |

## Calibrating the discharge model

The greedy world only differs from reality after the first throttling. From
then on, its SOC follows a model. The defaults are an empirical fit on the
reference system **against `sensor.house_consumption`**: that sensor is
computed by the goodwe library and reads ≈110 W too low at night, and the
fitted η = 0.60 + 42 W compensates exactly that. The physical values there are
η ≈ 0.86 and ≈34 W standby against the measured `sensor.load`.

To calibrate for your system, pick a few evenings/nights where the dashboard
shows both worlds coupled (identical SOC), simulate them with different
`--eta-discharge` / `--standby-w` (or `--load-entity sensor.load` with
physical values) and keep the pair whose simulated SOC tracks the real SOC
best. Until then, read the greedy SOC in the evening as an approximation.
