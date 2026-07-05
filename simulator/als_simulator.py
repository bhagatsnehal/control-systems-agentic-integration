"""
ALS Accelerator Control System Simulator
-----------------------------------------
Simulates a subset of the Advanced Light Source (ALS) accelerator control
environment for use as the backend of the AccelAgent agentic system.

Design principles:
- Channels have safe bounds; writes outside bounds are rejected
- A small set of channels are physically coupled (beam position responds
  to corrector magnet current, beam size responds to ID gap)
- A rolling time-series buffer stores the last N readings per channel
- An anomaly injector can trigger realistic drift/fault scenarios for demos

Channel naming follows EPICS-style conventions (SUBSYSTEM:SIGNAL) to match
real ALS infrastructure naming patterns.
"""

import time
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import deque
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Channel:
    """A single named control/monitor channel."""
    name: str
    description: str
    units: str
    value: float
    safe_min: float
    safe_max: float
    writable: bool
    nominal: float          # expected operating value

    def in_bounds(self, v: float) -> bool:
        return self.safe_min <= v <= self.safe_max

    def __repr__(self):
        rw = "RW" if self.writable else "RO"
        return (f"Channel({self.name}, val={self.value:.4f} {self.units}, "
                f"bounds=[{self.safe_min}, {self.safe_max}], {rw})")


@dataclass
class ChannelReading:
    """A timestamped snapshot of a channel value."""
    channel: str
    value: float
    timestamp: datetime
    in_bounds: bool


@dataclass
class WriteResult:
    success: bool
    message: str
    previous_value: float
    new_value: float


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class ALSSimulator:
    """
    Simulated ALS accelerator control system.

    Channels
    --------
    Corrector magnets (writable) — steer the electron beam horizontally
    and vertically in each straight section.

    Insertion device gaps (writable) — undulator/wiggler gap controls
    photon beam brightness and energy. Closing the gap increases
    magnetic field strength.

    Beam position monitors (read-only) — measure transverse beam position.
    Coupled to corrector magnets: increasing a corrector current nudges the
    beam in the corresponding plane.

    Beam current (read-only) — stored electron current in mA. Decays
    slowly (lifetime ~8 hours). Refilled by injection.

    Beam size (read-only) — transverse RMS beam size at each ID straight.
    Coupled to ID gap: smaller gap → stronger focusing → smaller beam size.

    RF cavity voltage (writable) — controls longitudinal dynamics and
    bunch length.

    Vacuum pressure (read-only) — residual gas pressure in the storage
    ring sections. Spikes signal a vacuum event.
    """

    BUFFER_SIZE = 500       # readings retained per channel
    TICK_INTERVAL = 1.0     # seconds between background ticks

    def __init__(self, seed: int = 42):
        random.seed(seed)
        self._channels: dict[str, Channel] = {}
        self._history: dict[str, deque[ChannelReading]] = {}
        self._lock = threading.Lock()
        self._running = False
        self._tick_thread: Optional[threading.Thread] = None
        self._anomalies: dict[str, float] = {}   # channel -> drift_per_tick

        self._define_channels()
        self._seed_history(n_points=120, span_minutes=120)

    # ------------------------------------------------------------------
    # Channel definitions
    # ------------------------------------------------------------------

    def _define_channels(self):
        defs = [
            # --- Corrector magnets (writable) ---
            Channel("CM:H1:CURRENT",  "Horizontal corrector 1 current",   "A",   0.012,  -2.0,  2.0,  True,  0.012),
            Channel("CM:H2:CURRENT",  "Horizontal corrector 2 current",   "A",  -0.034,  -2.0,  2.0,  True, -0.034),
            Channel("CM:V1:CURRENT",  "Vertical corrector 1 current",     "A",   0.008,  -2.0,  2.0,  True,  0.008),
            Channel("CM:V2:CURRENT",  "Vertical corrector 2 current",     "A",  -0.019,  -2.0,  2.0,  True, -0.019),

            # --- Insertion device gaps (writable) ---
            Channel("ID:1:GAP",       "Undulator 1 gap",                  "mm",  14.5,   10.0, 300.0, True,  14.5),
            Channel("ID:3:GAP",       "Wiggler 3 gap",                    "mm",  22.0,   15.0, 300.0, True,  22.0),

            # --- RF cavity (writable) ---
            Channel("RF:CAV:VOLTAGE", "RF cavity voltage",                "MV",   1.5,    1.0,   2.0,  True,   1.5),

            # --- Beam position monitors (read-only) ---
            Channel("BPM:H1:POS",     "Horizontal beam position at BPM1", "mm",   0.021, -5.0,  5.0, False,  0.0),
            Channel("BPM:H2:POS",     "Horizontal beam position at BPM2", "mm",  -0.015, -5.0,  5.0, False,  0.0),
            Channel("BPM:V1:POS",     "Vertical beam position at BPM1",   "mm",   0.003, -5.0,  5.0, False,  0.0),
            Channel("BPM:V2:POS",     "Vertical beam position at BPM2",   "mm",  -0.008, -5.0,  5.0, False,  0.0),

            # --- Beam current (read-only) ---
            Channel("SR:BEAM:CURRENT","Storage ring beam current",        "mA", 400.0,   0.0, 500.0, False, 400.0),

            # --- Beam size (read-only) ---
            Channel("BS:ID1:SIGMA_X", "Horizontal beam size at ID1",      "um",  210.0, 100.0, 500.0, False, 210.0),
            Channel("BS:ID1:SIGMA_Y", "Vertical beam size at ID1",        "um",   12.0,   5.0,  80.0, False,  12.0),

            # --- Vacuum (read-only) ---
            Channel("VAC:SEC3:PRESS", "Vacuum pressure sector 3",         "nTorr", 0.8,  0.0,  20.0, False,  0.8),
            Channel("VAC:SEC7:PRESS", "Vacuum pressure sector 7",         "nTorr", 1.1,  0.0,  20.0, False,  1.1),
        ]
        for ch in defs:
            self._channels[ch.name] = ch
            self._history[ch.name] = deque(maxlen=self.BUFFER_SIZE)

    # ------------------------------------------------------------------
    # Physics coupling
    # ------------------------------------------------------------------

    def _apply_coupling(self):
        """
        Update read-only channels based on writable channel state.
        Coupling is intentionally simplified — just enough to make sweeps
        produce plausible response curves.
        """
        ch = self._channels

        # BPM horizontal responds to horizontal correctors
        ch["BPM:H1:POS"].value = (
            ch["CM:H1:CURRENT"].value * 1.8
            + ch["CM:H2:CURRENT"].value * 0.4
            + random.gauss(0, 0.002)
        )
        ch["BPM:H2:POS"].value = (
            ch["CM:H1:CURRENT"].value * 0.3
            + ch["CM:H2:CURRENT"].value * 2.1
            + random.gauss(0, 0.002)
        )

        # BPM vertical responds to vertical correctors
        ch["BPM:V1:POS"].value = (
            ch["CM:V1:CURRENT"].value * 1.6
            + ch["CM:V2:CURRENT"].value * 0.2
            + random.gauss(0, 0.001)
        )
        ch["BPM:V2:POS"].value = (
            ch["CM:V1:CURRENT"].value * 0.2
            + ch["CM:V2:CURRENT"].value * 1.9
            + random.gauss(0, 0.001)
        )

        # Beam size responds to ID gap (smaller gap → tighter beam)
        gap_factor = (ch["ID:1:GAP"].value - 10.0) / 290.0   # 0→1 as gap opens
        ch["BS:ID1:SIGMA_X"].value = 180.0 + gap_factor * 80.0 + random.gauss(0, 1.0)
        ch["BS:ID1:SIGMA_Y"].value =  10.0 + gap_factor *  5.0 + random.gauss(0, 0.2)

        # Beam current decays slowly (~0.05 mA/tick at 1s tick = ~8hr lifetime)
        ch["SR:BEAM:CURRENT"].value = max(
            0.0, ch["SR:BEAM:CURRENT"].value - 0.05 + random.gauss(0, 0.005)
        )

    # ------------------------------------------------------------------
    # History seeding
    # ------------------------------------------------------------------

    def _seed_history(self, n_points: int, span_minutes: int):
        """Pre-populate history buffers so historical queries work on day 1."""
        now = datetime.utcnow()
        interval = timedelta(minutes=span_minutes) / n_points
        for i in range(n_points):
            ts = now - timedelta(minutes=span_minutes) + i * interval
            for name, ch in self._channels.items():
                noise = random.gauss(0, abs(ch.nominal) * 0.01 + 0.001)
                val = ch.nominal + noise
                self._history[name].append(
                    ChannelReading(
                        channel=name,
                        value=val,
                        timestamp=ts,
                        in_bounds=ch.in_bounds(val),
                    )
                )

    # ------------------------------------------------------------------
    # Background tick
    # ------------------------------------------------------------------

    def start(self):
        """Start background simulation tick."""
        self._running = True
        self._tick_thread = threading.Thread(
            target=self._tick_loop, daemon=True
        )
        self._tick_thread.start()

    def stop(self):
        self._running = False

    def _tick_loop(self):
        while self._running:
            time.sleep(self.TICK_INTERVAL)
            with self._lock:
                self._apply_coupling()
                self._record_snapshot()
                if self._anomalies:
                    self._step_anomaly()

    def _record_snapshot(self):
        ts = datetime.utcnow()
        for name, ch in self._channels.items():
            self._history[name].append(
                ChannelReading(
                    channel=name,
                    value=ch.value,
                    timestamp=ts,
                    in_bounds=ch.in_bounds(ch.value),
                )
            )

    # ------------------------------------------------------------------
    # Anomaly injection
    # ------------------------------------------------------------------

    def inject_anomaly(self, channel: str, drift_per_tick: float):
        """
        Inject a slow drift anomaly on a channel. The channel value will
        drift by drift_per_tick each background tick until clear_anomaly()
        is called for that channel. The drift is allowed to carry the
        value outside its safe bounds so the excursion is detectable via
        in_bounds history and deviation_from_nominal, not just clamped
        away. Multiple channels can have independent anomalies active at
        once; re-injecting on an already-anomalous channel replaces its
        drift rate.

        Useful for demo: simulate beam position drifting out of tolerance.
        """
        if channel not in self._channels:
            raise ValueError(f"Unknown channel: {channel}")
        with self._lock:
            self._anomalies[channel] = drift_per_tick

    def clear_anomaly(self, channel: str):
        """Clear the anomaly on the given channel, if one is active."""
        with self._lock:
            self._anomalies.pop(channel, None)

    def _step_anomaly(self):
        # Drift is uncapped so the value can cross safe_min/safe_max —
        # the excursion is the anomaly signal, tracked via in_bounds
        # history and deviation_from_nominal, not prevented.
        for channel, drift in self._anomalies.items():
            self._channels[channel].value += drift

    # ------------------------------------------------------------------
    # Public API — read
    # ------------------------------------------------------------------

    def get_channel_list(self) -> list[dict]:
        """Return all channels with metadata (no values)."""
        with self._lock:
            return [
                {
                    "name": ch.name,
                    "description": ch.description,
                    "units": ch.units,
                    "safe_min": ch.safe_min,
                    "safe_max": ch.safe_max,
                    "writable": ch.writable,
                    "nominal": ch.nominal,
                }
                for ch in self._channels.values()
            ]

    def get_channel(self, name: str) -> dict:
        """Return current value and metadata for a single channel."""
        with self._lock:
            ch = self._channels.get(name)
            if ch is None:
                raise ValueError(f"Unknown channel: {name}")
            return {
                "name": ch.name,
                "description": ch.description,
                "units": ch.units,
                "value": ch.value,
                "safe_min": ch.safe_min,
                "safe_max": ch.safe_max,
                "writable": ch.writable,
                "nominal": ch.nominal,
                "in_bounds": ch.in_bounds(ch.value),
                "deviation_from_nominal": ch.value - ch.nominal,
            }

    def get_system_status(self) -> dict:
        """Snapshot of all channel current values and bound status."""
        with self._lock:
            return {
                name: {
                    "value": ch.value,
                    "units": ch.units,
                    "in_bounds": ch.in_bounds(ch.value),
                    "writable": ch.writable,
                }
                for name, ch in self._channels.items()
            }

    def get_historical_data(
        self,
        channel: str,
        last_n_minutes: Optional[int] = None,
        last_n_points: Optional[int] = None,
    ) -> list[dict]:
        """
        Return historical readings for a channel.
        Specify either last_n_minutes or last_n_points (last_n_points takes
        precedence if both provided).
        """
        with self._lock:
            if channel not in self._history:
                raise ValueError(f"Unknown channel: {channel}")
            readings = list(self._history[channel])

        if last_n_points is not None:
            readings = readings[-last_n_points:]
        elif last_n_minutes is not None:
            cutoff = datetime.utcnow() - timedelta(minutes=last_n_minutes)
            readings = [r for r in readings if r.timestamp >= cutoff]

        return [
            {
                "timestamp": r.timestamp.isoformat(),
                "value": r.value,
                "in_bounds": r.in_bounds,
            }
            for r in readings
        ]

    def get_safety_bounds(self, channel: str) -> dict:
        """Return safe operating bounds for a channel."""
        with self._lock:
            ch = self._channels.get(channel)
            if ch is None:
                raise ValueError(f"Unknown channel: {channel}")
            return {
                "channel": channel,
                "safe_min": ch.safe_min,
                "safe_max": ch.safe_max,
                "units": ch.units,
                "nominal": ch.nominal,
                "current_value": ch.value,
                "in_bounds": ch.in_bounds(ch.value),
            }

    def analyze_channel(self, channel: str, last_n_minutes: int = 60) -> dict:
        """
        Compute summary statistics over recent history for a channel.
        """
        readings = self.get_historical_data(channel, last_n_minutes=last_n_minutes)
        if not readings:
            return {"error": "No data in requested window"}
        values = [r["value"] for r in readings]
        out_of_bounds = [r for r in readings if not r["in_bounds"]]
        return {
            "channel": channel,
            "window_minutes": last_n_minutes,
            "n_readings": len(values),
            "mean": sum(values) / len(values),
            "min": min(values),
            "max": max(values),
            "peak_to_peak": max(values) - min(values),
            "latest": values[-1],
            "n_out_of_bounds": len(out_of_bounds),
            "first_timestamp": readings[0]["timestamp"],
            "last_timestamp": readings[-1]["timestamp"],
        }

    # ------------------------------------------------------------------
    # Public API — write
    # ------------------------------------------------------------------

    def set_channel(self, channel: str, value: float) -> WriteResult:
        """
        Write a new value to a writable channel.
        Enforces safe bounds — out-of-bounds writes are rejected.
        Physics coupling is applied immediately after a successful write.
        """
        with self._lock:
            ch = self._channels.get(channel)
            if ch is None:
                return WriteResult(
                    success=False,
                    message=f"Unknown channel: {channel}",
                    previous_value=float("nan"),
                    new_value=float("nan"),
                )
            if not ch.writable:
                return WriteResult(
                    success=False,
                    message=f"{channel} is read-only",
                    previous_value=ch.value,
                    new_value=ch.value,
                )
            if not ch.in_bounds(value):
                return WriteResult(
                    success=False,
                    message=(
                        f"Value {value} {ch.units} outside safe bounds "
                        f"[{ch.safe_min}, {ch.safe_max}]"
                    ),
                    previous_value=ch.value,
                    new_value=ch.value,
                )
            prev = ch.value
            ch.value = value
            self._apply_coupling()
            return WriteResult(
                success=True,
                message="Write successful",
                previous_value=prev,
                new_value=value,
            )

    def sweep_channel(
        self,
        channel: str,
        start: float,
        stop: float,
        n_steps: int = 10,
        step_delay: float = 0.5,
    ) -> list[dict]:
        """
        Step a writable channel from start to stop in n_steps increments,
        recording coupled channel responses at each step.

        Returns a list of step records: {step, setpoint, readings_snapshot}.
        Aborts early if any step write is rejected (bounds violation).
        """
        import numpy as np

        setpoints = [
            start + (stop - start) * i / (n_steps - 1)
            for i in range(n_steps)
        ]
        results = []
        for i, sp in enumerate(setpoints):
            result = self.set_channel(channel, sp)
            if not result.success:
                results.append({
                    "step": i,
                    "setpoint": sp,
                    "success": False,
                    "abort_reason": result.message,
                })
                break
            time.sleep(step_delay)
            snapshot = {
                name: ch.value
                for name, ch in self._channels.items()
            }
            results.append({
                "step": i,
                "setpoint": sp,
                "success": True,
                "readings": snapshot,
            })
        return results
