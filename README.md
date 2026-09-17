# Appliance Watch

Home Assistant integration that follows a washing machine, a tumble dryer and a
dishwasher **through their power draw alone** — no smart appliance, no dedicated
plug — and exposes the discrete states an iOS Live Activity needs.

It exists for two situations that the usual "appliance finished" blueprints do
not handle:

- **two appliances behind one meter.** The washer and the dryer share a single
  Shelly, so a threshold tells you *something* is running, never *what*.
- **an appliance nothing measures at all.** The dishwasher is read from the
  house's unmetered consumption — total draw minus every metered appliance —
  where it sits right next to the oven.

## What it creates

Two devices, *Lave-vaisselle* and *Buanderie*, each with:

| Entity | Use |
|---|---|
| `binary_sensor.*_en_marche` | the trigger for a Live Activity — a discrete transition, never a power sensor |
| `sensor.*_etat` | `veille` / `en_cours` / `termine` |
| `sensor.*_appareil` | laundry only: `inconnu` → `lave_linge` / `seche_linge` / `les_deux` |
| `sensor.*_temps_restant` | minutes, from the learned duration — feeds `when` |
| `sensor.*_progression` | percent — feeds `progress` |
| `sensor.*_puissance` | estimated draw, for a tile or a message |
| `sensor.*_energie` | kWh, `total_increasing` → shows up in the Energy dashboard |
| `sensor.*_debut`, `*_fin`, `*_duree_dernier_cycle`, `*_energie_dernier_cycle` | history |
| `sensor.lave_vaisselle_conso_non_mesuree` | the residual itself — the number to look at when a cycle is missed or invented |
| `button.buanderie_oublier_empreintes` | throw away what was learned |

Entity *ids* follow the Home Assistant UI language — an English instance gets
`binary_sensor.buanderie_running` for the row above. The unique ids, and the
translation keys, do not move.

Every cycle fires an `appliance_watch_cycle` event (`watcher`, `kind`,
`appliance`, `reason`).

## How it decides

Nothing here is a round number picked by hand: the thresholds come from six days
of recorded traces, and those traces are the test suite (`tests/fixtures/`).

### Dishwasher — a step, not a level

| | measured |
|---|---|
| Dishwasher heating | **+1 940 … 2 015 W** above the ambient base, flat, 4 to 14 minutes at a time |
| Oven | +2 150 … 2 230 W |
| Hob | 2 400-3 700 W, toggling every minute |
| Ambient unmetered base | median 227 W, above 600 W only 6.9 % of the time |

The band kept is **1 850 – 2 080 W of *step*** above a rolling baseline, held
**4 unbroken minutes**. Working on the step rather than the absolute level is
what makes it survive a noisy base: an unusually high base yields a *missed*
cycle, never a false one.

Over five days that filter caught the three real cycles — 13:42, 17:00, and one
at 01:42 on off-peak hours — and nothing else.

Two traps the traces revealed, both handled:

- the **opening burst alternates heating and pumping**, and on one cycle never
  held four unbroken minutes; the plateau that proved the cycle came twenty
  minutes later. The start is therefore back-dated to the first heating.
- **silence does not mean finished**: this dishwasher goes 42 minutes between
  two heating phases, and its final dry draws nothing. The expected duration
  carries the cycle; a late heating extends it.

### Washer / dryer — one meter, two appliances

| | measured |
|---|---|
| Washer | heats ~7 min at 2 270-2 431 W, then runs at 150-300 W. 60 min, or 120 on the long programme |
| Dryer | 1 871-2 206 W in repeated plateaus across the whole cycle. 60-70 min |
| Standby | 30 W, brief 75 W blips — **and 15-minute episodes at ~180 W** |
| Freezer (same circuit) | ~120 W running, **surging to 1.5-2 kW on every compressor start** |

- A rise above 100 W only *arms* a cycle; it is confirmed by a burst above
  1 500 W **held for a minute**, within ten minutes of arming. The 180 W
  episodes — five in six days — arm and expire without ever confirming, and the
  freezer's 2 kW inrush is worth barely 150 W once averaged over that minute.
- The appliance is named on the **mean between T+15 and T+20**: measured 144 W
  for the washer, 1 035-1 983 W for the dryer. The bar sits at 800 W.
- The cycle is dated from the rise, not from the burst.
- **Known limit**: the washer's drum (142-215 W) and the freezer's compressor
  draw alike, so no threshold separates them. Idling is therefore judged low,
  and a compressor running when the laundry finishes delays "terminé" by up to
  one compressor run. The countdown has reached zero by then, so nothing is
  misstated — it is only late.
- A second appliance joining is inferred from what the running one cannot do:
  a washer past its heating never averages 1 600 W over twenty minutes, and a
  dryer's troughs never sit between 300 and 800 W. While both run, each one's
  progress rests on its learned duration rather than on the meter.

### Learned durations

The expected length starts at the nominal and is blended with the median of
what has actually been observed, in proportion to how many cycles have been
seen (full weight after eight). A cycle more than 2.5× from the known median is
recorded as rejected rather than learned — that is almost always two appliances
counted as one.

## Install

HACS → custom repository → `qelanhari/ha-appliance` → Integration. Then
*Settings → Devices & Services → Add integration → Appliance Watch*, and point
it at:

- the **whole-house consumption** sensor;
- every **already-metered** appliance, to be subtracted;
- **intermittent meters** (a car charger reports nothing while unplugged);
- the **laundry meter**.

A metered sensor that drops to `unavailable` **suspends** the residual rather
than counting 0 W: a missing reading would inflate it by exactly what that
appliance was drawing. The laundry meter is subtracted automatically, listed or
not — leaving it in would push a 2 kW washing machine straight into the
dishwasher's band.

## iOS Live Activities

The integration does not send them; it provides what they need — discrete
transitions and a credible remaining time. Trigger on
`binary_sensor.*_en_marche`, **never** on a power sensor: iOS throttles
frequent updates and will drop the activity.

```yaml
action: notify.mobile_app_<device>
data:
  title: "Lave-vaisselle"          # fixed at start; updates cannot change it
  message: "Cycle en cours"
  data:
    tag: lave_vaisselle
    live_update: true
    chronometer: true              # counts down on-device: no push for an hour
    when: "{{ states('sensor.lave_vaisselle_temps_restant') | int * 60 }}"
    when_relative: true
    progress: 0
    progress_max: 100
    notification_icon: mdi:dishwasher
```

Requires Home Assistant 2026.7+, iOS 17.2+ and a reachable instance (the token
handshake needs it). Beware the **push-to-start budget**: repeatedly starting
activities while testing exhausts it, after which new ones fail *silently* —
the automation succeeds, nothing is logged, the phone stays quiet.

## Re-recording the traces

```bash
python3 scripts/export_traces.py --config ../claude-ha/config.env
python3 -m pytest tests -q          # detection logic, no Home Assistant needed
```

```bash
python3 -m venv .venv-test
.venv-test/bin/pip install -r requirements_test.txt
.venv-test/bin/pytest               # adds the wiring tests
```
