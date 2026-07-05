"""
Unit tests for ALSSimulator
Covers: bounds enforcement, historical queries, coupling, write API, sweep, anomaly injection.
"""

import time
import pytest
from simulator.als_simulator import ALSSimulator


@pytest.fixture
def sim():
    s = ALSSimulator(seed=0)
    return s


# ---------------------------------------------------------------------------
# Channel list and metadata
# ---------------------------------------------------------------------------

class TestChannelList:
    def test_returns_all_channels(self, sim):
        channels = sim.get_channel_list()
        assert len(channels) == 16

    def test_channel_has_required_fields(self, sim):
        ch = sim.get_channel_list()[0]
        for field in ("name", "description", "units", "safe_min", "safe_max",
                      "writable", "nominal"):
            assert field in ch

    def test_writable_and_readonly_channels_exist(self, sim):
        channels = sim.get_channel_list()
        writables = [c for c in channels if c["writable"]]
        readonly = [c for c in channels if not c["writable"]]
        assert len(writables) > 0
        assert len(readonly) > 0

    def test_known_writable_channel(self, sim):
        channels = {c["name"]: c for c in sim.get_channel_list()}
        assert channels["CM:H1:CURRENT"]["writable"] is True

    def test_known_readonly_channel(self, sim):
        channels = {c["name"]: c for c in sim.get_channel_list()}
        assert channels["BPM:H1:POS"]["writable"] is False


# ---------------------------------------------------------------------------
# Single channel read
# ---------------------------------------------------------------------------

class TestGetChannel:
    def test_known_channel_returns_value(self, sim):
        ch = sim.get_channel("CM:H1:CURRENT")
        assert "value" in ch
        assert "in_bounds" in ch
        assert "deviation_from_nominal" in ch

    def test_unknown_channel_raises(self, sim):
        with pytest.raises(ValueError, match="Unknown channel"):
            sim.get_channel("DOES:NOT:EXIST")

    def test_nominal_value_is_in_bounds(self, sim):
        for name in [c["name"] for c in sim.get_channel_list()]:
            ch = sim.get_channel(name)
            assert ch["in_bounds"], f"{name} nominal value should be in bounds"


# ---------------------------------------------------------------------------
# System status snapshot
# ---------------------------------------------------------------------------

class TestSystemStatus:
    def test_returns_all_channels(self, sim):
        status = sim.get_system_status()
        assert len(status) == 16

    def test_each_entry_has_value_and_bounds(self, sim):
        status = sim.get_system_status()
        for name, entry in status.items():
            assert "value" in entry
            assert "in_bounds" in entry
            assert "writable" in entry


# ---------------------------------------------------------------------------
# Historical data
# ---------------------------------------------------------------------------

class TestHistoricalData:
    def test_seeded_history_exists(self, sim):
        history = sim.get_historical_data("CM:H1:CURRENT", last_n_points=10)
        assert len(history) == 10

    def test_history_has_required_fields(self, sim):
        history = sim.get_historical_data("CM:H1:CURRENT", last_n_points=5)
        for entry in history:
            assert "timestamp" in entry
            assert "value" in entry
            assert "in_bounds" in entry

    def test_last_n_points_respected(self, sim):
        history = sim.get_historical_data("SR:BEAM:CURRENT", last_n_points=20)
        assert len(history) == 20

    def test_last_n_minutes_returns_subset(self, sim):
        # Seeded history spans 120 minutes; requesting 30 should return ~25% of points
        all_history = sim.get_historical_data("CM:H1:CURRENT", last_n_points=120)
        recent = sim.get_historical_data("CM:H1:CURRENT", last_n_minutes=30)
        assert len(recent) < len(all_history)
        assert len(recent) > 0

    def test_unknown_channel_raises(self, sim):
        with pytest.raises(ValueError):
            sim.get_historical_data("FAKE:CHANNEL", last_n_points=5)

    def test_timestamps_are_ordered(self, sim):
        history = sim.get_historical_data("CM:H1:CURRENT", last_n_points=50)
        timestamps = [h["timestamp"] for h in history]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Safety bounds
# ---------------------------------------------------------------------------

class TestSafetyBounds:
    def test_returns_bounds_for_known_channel(self, sim):
        bounds = sim.get_safety_bounds("CM:H1:CURRENT")
        assert "safe_min" in bounds
        assert "safe_max" in bounds
        assert bounds["safe_min"] < bounds["safe_max"]

    def test_raises_for_unknown_channel(self, sim):
        with pytest.raises(ValueError):
            sim.get_safety_bounds("FAKE:CH")

    def test_nominal_is_within_bounds(self, sim):
        for ch in sim.get_channel_list():
            bounds = sim.get_safety_bounds(ch["name"])
            assert bounds["safe_min"] <= bounds["nominal"] <= bounds["safe_max"], \
                f"Nominal for {ch['name']} is outside safe bounds"


# ---------------------------------------------------------------------------
# Write / bounds enforcement
# ---------------------------------------------------------------------------

class TestSetChannel:
    def test_valid_write_succeeds(self, sim):
        result = sim.set_channel("CM:H1:CURRENT", 0.5)
        assert result.success is True
        assert sim.get_channel("CM:H1:CURRENT")["value"] == pytest.approx(0.5)

    def test_write_above_max_rejected(self, sim):
        result = sim.set_channel("CM:H1:CURRENT", 999.0)
        assert result.success is False
        assert "outside safe bounds" in result.message

    def test_write_below_min_rejected(self, sim):
        result = sim.set_channel("CM:H1:CURRENT", -999.0)
        assert result.success is False

    def test_write_to_readonly_rejected(self, sim):
        result = sim.set_channel("BPM:H1:POS", 1.0)
        assert result.success is False
        assert "read-only" in result.message

    def test_write_to_unknown_channel_rejected(self, sim):
        result = sim.set_channel("DOES:NOT:EXIST", 1.0)
        assert result.success is False

    def test_write_at_exact_bound_succeeds(self, sim):
        bounds = sim.get_safety_bounds("CM:H1:CURRENT")
        result = sim.set_channel("CM:H1:CURRENT", bounds["safe_max"])
        assert result.success is True

    def test_previous_value_preserved_on_failed_write(self, sim):
        original = sim.get_channel("CM:H1:CURRENT")["value"]
        sim.set_channel("CM:H1:CURRENT", 999.0)
        assert sim.get_channel("CM:H1:CURRENT")["value"] == pytest.approx(original)


# ---------------------------------------------------------------------------
# Physics coupling
# ---------------------------------------------------------------------------

class TestCoupling:
    def test_bpm_responds_to_corrector_write(self, sim):
        before = sim.get_channel("BPM:H1:POS")["value"]
        sim.set_channel("CM:H1:CURRENT", 1.5)   # large corrector kick
        after = sim.get_channel("BPM:H1:POS")["value"]
        assert abs(after) > abs(before) or after != before  # position shifted

    def test_beam_size_responds_to_id_gap(self, sim):
        sim.set_channel("ID:1:GAP", 10.5)   # close gap
        small_gap_size = sim.get_channel("BS:ID1:SIGMA_X")["value"]
        sim.set_channel("ID:1:GAP", 200.0)  # open gap wide
        large_gap_size = sim.get_channel("BS:ID1:SIGMA_X")["value"]
        assert large_gap_size > small_gap_size


# ---------------------------------------------------------------------------
# Analyze channel
# ---------------------------------------------------------------------------

class TestAnalyzeChannel:
    def test_returns_stats_dict(self, sim):
        stats = sim.analyze_channel("CM:H1:CURRENT", last_n_minutes=120)
        for key in ("mean", "min", "max", "peak_to_peak", "n_readings", "latest"):
            assert key in stats

    def test_min_leq_mean_leq_max(self, sim):
        stats = sim.analyze_channel("CM:H1:CURRENT", last_n_minutes=120)
        assert stats["min"] <= stats["mean"] <= stats["max"]

    def test_peak_to_peak_is_max_minus_min(self, sim):
        stats = sim.analyze_channel("CM:H1:CURRENT", last_n_minutes=120)
        assert stats["peak_to_peak"] == pytest.approx(stats["max"] - stats["min"])


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

class TestSweep:
    def test_sweep_returns_correct_n_steps(self, sim):
        results = sim.sweep_channel("CM:H1:CURRENT", start=-0.5, stop=0.5,
                                    n_steps=5, step_delay=0.0)
        assert len(results) == 5

    def test_sweep_all_steps_succeed_within_bounds(self, sim):
        results = sim.sweep_channel("CM:H1:CURRENT", start=-0.5, stop=0.5,
                                    n_steps=5, step_delay=0.0)
        assert all(r["success"] for r in results)

    def test_sweep_aborts_on_out_of_bounds_setpoint(self, sim):
        # start in bounds, sweep to out-of-bounds stop
        results = sim.sweep_channel("CM:H1:CURRENT", start=0.0, stop=10.0,
                                    n_steps=5, step_delay=0.0)
        # At least one step should have failed and aborted
        failed = [r for r in results if not r["success"]]
        assert len(failed) >= 1

    def test_sweep_records_snapshots(self, sim):
        results = sim.sweep_channel("CM:H1:CURRENT", start=-0.2, stop=0.2,
                                    n_steps=3, step_delay=0.0)
        for r in results:
            if r["success"]:
                assert "readings" in r
                assert "BPM:H1:POS" in r["readings"]


# ---------------------------------------------------------------------------
# Anomaly injection
# ---------------------------------------------------------------------------

class TestAnomalyInjection:
    def test_anomaly_drifts_channel_over_time(self, sim):
        # Writable channels aren't reset by _apply_coupling each tick, so
        # drift accumulates here (unlike on coupled read-only channels).
        sim.start()
        initial = sim.get_channel("CM:H1:CURRENT")["value"]
        sim.inject_anomaly("CM:H1:CURRENT", drift_per_tick=0.05)
        time.sleep(3.5)   # allow ~3 ticks
        after = sim.get_channel("CM:H1:CURRENT")["value"]
        sim.stop()
        sim.clear_anomaly("CM:H1:CURRENT")
        assert after != initial

    def test_anomaly_can_exceed_bounds(self, sim):
        sim.start()
        sim.inject_anomaly("RF:CAV:VOLTAGE", drift_per_tick=0.3)   # bounds [1.0, 2.0]
        time.sleep(3.5)   # enough ticks to cross the bound
        ch = sim.get_channel("RF:CAV:VOLTAGE")
        sim.stop()
        sim.clear_anomaly("RF:CAV:VOLTAGE")
        assert ch["value"] > ch["safe_max"]
        assert ch["in_bounds"] is False

    def test_clear_anomaly_stops_drift(self, sim):
        """After clearing the anomaly, it is no longer tracked as active."""
        sim.start()
        sim.inject_anomaly("CM:H2:CURRENT", drift_per_tick=0.05)
        time.sleep(2.0)
        sim.clear_anomaly("CM:H2:CURRENT")
        sim.stop()
        assert sim._anomalies == {}

    def test_multiple_anomalies_track_independently(self, sim):
        sim.start()
        sim.inject_anomaly("CM:H1:CURRENT", drift_per_tick=0.05)
        sim.inject_anomaly("RF:CAV:VOLTAGE", drift_per_tick=0.02)
        time.sleep(2.5)
        assert set(sim._anomalies) == {"CM:H1:CURRENT", "RF:CAV:VOLTAGE"}
        sim.clear_anomaly("CM:H1:CURRENT")
        assert set(sim._anomalies) == {"RF:CAV:VOLTAGE"}
        sim.stop()
        sim.clear_anomaly("RF:CAV:VOLTAGE")
