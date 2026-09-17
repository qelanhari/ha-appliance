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
The **negatives matter more than the positives**: the oven, the hob and the
garage's 180 W episodes are what a naive detector gets wrong.

## What survives a restart, and what must not

Persisted in `Store(hass, 1, f"{DOMAIN}.{entry_id}")`:

- learned fingerprints (durations, energies, rejected count);
- cumulative energy per watcher, last cycle duration/energy, last finish time.

**Deliberately not persisted: a cycle in flight.** After a restart the detector
has no trace behind it, so it can neither confirm nor end a cycle it did not
see start — restoring one would leave a countdown running for ever.

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
