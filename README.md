# control-systems-agentic-integration

## Simulator fixes log

Corrections made to `simulator/als_simulator.py`, in chronological order.

### 2026-07-05

Anomaly-injection logic:

- **Anomalies no longer self-clamp.** `_step_anomaly` previously stopped and
  clamped a drifting channel the moment it would cross `safe_min`/`safe_max`,
  which meant `in_bounds` could never actually flip to `False` from an
  anomaly. Drift is now uncapped, so excursions are detectable via both
  `in_bounds` history and `deviation_from_nominal`.
- **Multiple concurrent anomalies.** Replaced the single-slot
  `_anomaly_active` / `_anomaly_channel` / `_anomaly_drift` state with
  `_anomalies: dict[str, float]` (channel → drift_per_tick), so independent
  anomalies can run on different channels at once.
- **`clear_anomaly(channel)` now requires an explicit channel** — no
  implicit "clear all" behavior, to avoid accidentally wiping every active
  anomaly when only one was intended.
- **Thread safety:** `inject_anomaly` and `clear_anomaly` now acquire
  `self._lock`, matching every other public method — previously they
  mutated `_anomalies` unlocked while the background tick thread iterated
  it inside `_step_anomaly`, a race that could raise
  `RuntimeError: dictionary changed size during iteration`.
- Updated `tests/test_simulator.py`'s `TestAnomalyInjection` accordingly:
  drift tests now target writable channels (coupled read-only channels get
  overwritten by `_apply_coupling` each tick before an anomaly step can
  persist), added a test confirming drift can cross a safe bound, and added
  a test for two simultaneous independent anomalies.

## ALSSimulator design summary

`simulator/als_simulator.py` simulates a subset of an EPICS-style accelerator
control system (channel names follow the `SUBSYSTEM:SIGNAL` convention). It's
a standalone backend with no external dependencies — nothing here talks to
real hardware.

**Channels (16 total):**

| Channel | Writable | Coupled to |
|---|---|---|
| `CM:H1:CURRENT`, `CM:H2:CURRENT` | yes | drives `BPM:H1:POS` / `BPM:H2:POS` |
| `CM:V1:CURRENT`, `CM:V2:CURRENT` | yes | drives `BPM:V1:POS` / `BPM:V2:POS` |
| `ID:1:GAP` | yes | drives `BS:ID1:SIGMA_X` / `BS:ID1:SIGMA_Y` |
| `ID:3:GAP` | yes | none (no coupling implemented) |
| `RF:CAV:VOLTAGE` | yes | none |
| `BPM:H1:POS`, `BPM:H2:POS` | no | horizontal correctors (each BPM responds mainly to its own corrector, weakly to the other) |
| `BPM:V1:POS`, `BPM:V2:POS` | no | vertical correctors (same cross-coupling pattern) |
| `BS:ID1:SIGMA_X`, `BS:ID1:SIGMA_Y` | no | `ID:1:GAP` (smaller gap → smaller beam size) |
| `SR:BEAM:CURRENT` | no | none — decays independently (~0.05 mA/tick) |
| `VAC:SEC3:PRESS`, `VAC:SEC7:PRESS` | no | none — value never changes on its own |

**Core behaviors:**
- **Bounds enforcement:** every channel has `safe_min`/`safe_max`, but they're
  only enforced by `set_channel` — the one path a caller uses to write a
  value, which rejects both out-of-bounds writes and any write to a
  non-writable channel (`WriteResult.success = False`). Internal updates
  (coupling, anomaly drift) assign `channel.value` directly and bypass this
  check entirely, which is what lets coupling noise or an injected anomaly
  push a value outside its safe bounds.
- **Coupling:** on each background tick, `_apply_coupling` recomputes every
  coupled read-only channel from its source channel's *current* value plus
  small Gaussian noise. This is a full overwrite, not an increment — so a
  coupled channel's value never persists independently of its source.
- **History:** each channel keeps a rolling buffer (last `BUFFER_SIZE=500`
  readings) seeded on startup with ~2 hours of synthetic history, so
  `get_historical_data` / `analyze_channel` work immediately without waiting
  for the simulator to run.
- **Anomaly injection:** `inject_anomaly(channel, drift_per_tick)` drifts a
  channel by a fixed amount every tick until `clear_anomaly(channel)` is
  called; drift is uncapped and can cross safe bounds. Multiple channels can
  have independent anomalies active simultaneously. Injecting directly on a
  *coupled* read-only channel (`BPM:*`, `BS:ID1:*`) is a no-op in practice —
  `_apply_coupling` overwrites it before the drift can accumulate — so drift
  only persists on writable channels and on the channels with no coupling
  formula at all (`SR:BEAM:CURRENT`, `VAC:SEC3/7:PRESS`, `ID:3:GAP`,
  `RF:CAV:VOLTAGE`), since nothing else overwrites their value between ticks.
- **Threading:** a single background daemon thread ticks once per second
  (`TICK_INTERVAL`), applying coupling, recording a history snapshot, then
  stepping any active anomalies — all inside one lock, so all public methods
  that touch channel state are safe to call concurrently with the tick loop.

---

*Built with the assistance of Claude Sonnet 5 (Anthropic), via Claude Code.*
