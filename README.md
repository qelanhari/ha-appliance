# Appliance Watch

Home Assistant integration that follows a dishwasher, a washing machine and a
tumble dryer, and exposes the discrete states an iOS Live Activity needs.

It exists for the situations the usual "appliance finished" blueprints do not
handle:

- **an appliance nothing measures at all.** The dishwasher is read from the
  house's unmetered consumption — total draw minus every metered appliance —
  where it sits right next to the oven.
- **two appliances behind one meter.** The washer and the dryer share a single
  Shelly. The washer — an LG — reports itself through **LG ThinQ**, so whatever
  the meter sees while the washer is idle is the dryer.

## What it creates

Three devices, *Lave-vaisselle*, *Lave-linge* and *Sèche-linge*, each with:

| Entity | Use |
|---|---|
| `binary_sensor.*_en_marche` | the trigger for a Live Activity — a discrete transition, never a power sensor |
| `sensor.*_etat` | `veille` / `en_cours` / `termine` |
| `sensor.*_phase` | dishwasher only: `veille` / `chauffe` / `cycle` / `termine` |
| `sensor.*_chauffes` | dishwasher only: heating phases so far, with the learned total as an attribute |
| `sensor.*_etape` | washer only: the stage ThinQ reports — `lavage`, `rincage`, `essorage`, `pause`… |
| `sensor.*_temps_restant` | minutes — feeds `when`. The washer's is ThinQ's own; the others come from the learned duration |
| `sensor.*_fin_prevue` | timestamp of the expected end |
| `sensor.*_progression` | percent — feeds `progress` |
| `sensor.*_puissance` | estimated draw, for a tile or a message |
| `sensor.*_energie` | kWh, `total_increasing` → shows up in the Energy dashboard |
| `sensor.*_debut`, `*_fin`, `*_duree_dernier_cycle`, `*_energie_dernier_cycle` | history |
| `sensor.*_cycles_appris` | dishwasher and dryer: cycles learned from |
| `sensor.lave_vaisselle_conso_non_mesuree` | the residual itself — the number to look at when a cycle is missed or invented |
| `button.seche_linge_oublier_empreintes` | throw away what was learned |

Entity *ids* follow the Home Assistant UI language — an English instance gets
`binary_sensor.lave_linge_running` for the row above. The unique ids, and the
translation keys, do not move.

Every cycle fires an `appliance_watch_cycle` event (`watcher`, `kind`,
`appliance`, `reason`); `kind` is `started`, `finished` or `cancelled`.

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

### Washer — as LG ThinQ reports it

Nothing is inferred. A ThinQ status of `detecting`, `running`, `rinsing`,
`spinning`… opens the cycle, `end` or `power_off` closes it. A `pause`, a
`rinse_hold` or an `error` keeps it open: the drum is still full.

- **The start is the machine's**, not the moment Home Assistant heard of it:
  predicted end minus programme length. That is what recovers it after a
  restart mid-cycle — unlike the meter-read machines, the washer *is* restored
  — or behind a slow cloud. On 3 Oct ThinQ came up half an hour into a wash
  and dated it 17:21:13; the meter had risen at 17:18:54, for the load sensing.
- ThinQ publishes the status a few milliseconds before the predicted end and
  the length, so the start is corrected on the update after it.
- The remaining time and the end follow ThinQ's revisions — a few minutes
  either way over the cycle.
- The energy is read off the laundry meter, except while the dryer runs too:
  the meter cannot split them, and the dryer gets the whole reading.
- `unavailable` is "no answer", never "off": a cycle already open stays open.

### Dryer — the laundry meter, when the washer is idle

| | measured |
|---|---|
| Dryer | 1 871-2 206 W in repeated plateaus across the whole cycle, 8.2 min apart. 60-70 min |
| Standby | 30 W, brief 75 W blips — **and 15-minute episodes at ~180 W** |
| Freezer (same circuit) | ~120 W running, **surging to 1.5-2 kW on every compressor start** |

- A rise above 100 W only *arms* a cycle; it is confirmed by a burst above
  1 500 W **held for twenty seconds**, within ten minutes of arming. The 180 W
  episodes — five in six days — arm and expire without ever confirming. The
  freezer's inrush never reaches the state stream at all — the meter averages
  it away.
- **Only while ThinQ says the washer is idle.** A hot wash draws exactly what
  the dryer draws; telling them apart by power is the mistake that named a
  washer "sèche-linge" on 19 Sept. If ThinQ has no answer, no dryer cycle
  opens: a missed dryer beats a washer named a dryer.
- ThinQ can lag the meter. A dryer cycle opened less than **5 minutes** before
  the washer's own start was the washer's opening burst, and is withdrawn
  (`cancelled`).
- The cycle is dated from the rise, not from the burst.
- It closes after **six minutes** of silence — not one: measured dead times
  inside a cycle reach 234-300 s. That delay **tightens as the machine's rhythm
  is learned**: twice the longest dead time ever recorded, never under two
  minutes. Learned as a max, never an average. Visible as `temps_mort_max_s`
  on the running sensor.
- **Both machines at once.** A dryer already running keeps its cycle when the
  washer starts, but the meter never goes quiet then: the dryer is kept alive
  by its heating alone and closes **13 minutes** after its last heat (it
  reheats every 8.2). A dryer started *while* the washer runs is not seen.
- **Known limit**: the dryer's tumbling and the freezer's compressor draw
  alike. A compressor running when the drying finishes delays "terminé" by up
  to one compressor run.

### Dishwasher stages

Its heating *is* the detection signal, so the stage can be stated rather than
guessed — and the count of heating phases places a cycle far better than a
share of a nominal hour: the machine heats for the wash, then for each rinse.
Four on one recorded cycle, two on another, and the opening burst counts as one
despite alternating with the pump every minute.

The expected total is learned like the durations, and left off the display
until it is known rather than invented.

### Learned durations

The expected length starts at the nominal and is blended with the median of
what has actually been observed, in proportion to how many cycles have been
seen (full weight after eight). A cycle more than 2.5× from the known median is
recorded as rejected rather than learned. The washer learns nothing: it
announces its own length.

## Install

HACS → custom repository → `qelanhari/ha-appliance` → Integration. Then
*Settings → Devices & Services → Add integration → Appliance Watch*, and point
it at:

- the **whole-house consumption** sensor;
- every **already-metered** appliance, to be subtracted;
- **intermittent meters** (a car charger reports nothing while unplugged);
- the **laundry meter**;
- the washer's ThinQ **current status** sensor. Its remaining-time and total-time
  sensors are found on the same device.

Upgrading from 0.4: open *Options* once and pick the washer's status sensor —
until then the dryer is not detected. The dryer's learned rhythm is carried
over; the old *Buanderie* device and its entities can be deleted.

A metered sensor that drops to `unavailable` **suspends** the residual rather
than counting 0 W: a missing reading would inflate it by exactly what that
appliance was drawing. The laundry meter is subtracted automatically, listed or
not — leaving it in would push a 2 kW washing machine straight into the
dishwasher's band.

## iOS Live Activities

The integration does not send them; it provides what they need — discrete
transitions and a credible remaining time. Trigger on
`binary_sensor.*_en_marche`, **never** on a power sensor: iOS throttles
frequent updates and will drop the activity. Every machine is named from the
first push, so one activity per cycle is enough.

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
