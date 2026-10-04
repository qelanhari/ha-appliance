# CLAUDE.md — ha-appliance

Guidance for working in this repository.

## Shape

- `custom_components/appliance_watch/logic/` — **pure**: no `homeassistant`
  import anywhere under it. That is what lets the detection suite replay real
  traces under plain `pytest`, in milliseconds.
- everything above it is Home Assistant plumbing: state subscriptions, a 30 s
  clock tick, persistence, entities.

Put judgement in `logic/`, I/O in `coordinator.py`. If a rule needs `hass` to be
decided, it is in the wrong file.

## Thresholds are measurements, not preferences

Every default in `logic/detector.py` comes from a recorded trace, and the traces
are in `tests/fixtures/`. Changing a default without a trace to justify it is a
regression, and the guard tests at the end of `tests/test_detector.py` exist to
make that loud.

Re-record with `scripts/export_traces.py` (needs the token from `../claude-ha`).
The **negatives matter more than the positives**: the oven, the hob, the
garage's 180 W episodes and every wash replayed with the washer running are
what a naive detector gets wrong. `laundry` traces (`thinq_*`) carry ThinQ's
states next to the meter, and `tests/test_laundry.py` replays them in the
coordinator's order.

## The washer tells, the dryer is deduced

The washer (LG) reports itself through ThinQ: `logic/washer.py` follows it and
infers nothing. The laundry meter is the dryer's **only while the washer is
known to be idle** — ThinQ `unavailable` is "no answer", never "off", and no
dryer cycle opens on it. Do not bring back a power rule to tell the two apart:
a hot wash draws what the dryer draws, which is how a washer was named
"sèche-linge" on 19 Sept 2026.

## Starting a machine is an action, not a guess

`logic/solar_start.py` presses start on a real appliance. Every input must be
*known* — remote-start flag, washer status, an observed window of grid
readings — and a missing one means "do not start", never "assume it is fine".
Its thresholds are judged on what the sun **then actually gave**
(`realised_saving` in `tests/test_solar_start.py`), not on the estimate:
that is how a 15-minute window was shown to start into clouds.

## What survives a restart, and what must not

Persisted in `Store(hass, 1, f"{DOMAIN}.{entry_id}")`:

- learned fingerprints (durations, energies, rejected count);
- cumulative energy per watcher, last cycle duration/energy, last finish time;
- the solar-start switch.

**Deliberately not persisted: a cycle in flight.** After a restart the meter-read
detectors have no trace behind them, so they can neither confirm nor end a
cycle they did not see start — restoring one would leave a countdown running
for ever. The washer needs no persistence: ThinQ reports it running on the
first refresh, and its start is `ends_at - total`.

Also not persisted: rolling windows and hysteresis timers. They describe the
last few minutes; an hour later they are a lie.

`_save_state()` is called on every finish, not only at the end of a cycle of the
main loop — most early returns never reach the bottom.

## A reading holds until the next one

That is true of these meters and of every window statistic here — and it cuts
both ways. A window covered by a single sample is *not* evidence: before
concluding anything from `Trace.mean`, check that the trace actually spans the
window. Skipping that check let a restart mid-burst confirm a cycle on one
reading.

## Reading sensors

`_read()` returns `None` for `unavailable`/`unknown`, never `0.0`, and
`residual_watts` propagates that as "no answer". A missing reading coerced to
zero inflates the residual by whatever that appliance was drawing, which is the
one failure mode that turns a dishwasher detector into an oven detector.

## Release

HACS only sees GitHub **releases**, not tags:

1. bump `manifest.json`
2. `git commit` + `git tag vX.Y.Z`
3. `gh release create vX.Y.Z --generate-notes`
