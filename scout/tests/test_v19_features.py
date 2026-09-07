from __future__ import annotations

import time
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import FakeClient
from test_v18_features import _BarClient, _CycleLogger, _Logger, _m5
from xau_mt5_bot.config import BotConfig, load_config
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.fingerprint import strategy_fingerprint
from xau_mt5_bot.history import reset_history_cache
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import Action
from xau_mt5_bot.pattern_scan import compute_patterns
from xau_mt5_bot.scouts import ScoutManager


def test_poll_is_three_seconds(config):
    assert config.poll_seconds == 3


def test_pattern_scan_runs_in_a_separate_process(config):
    engine = TradingEngine(FakeClient(), config, _Logger())
    frame = _m5(2000); now = frame.time.iloc[-1].to_pydatetime()
    engine._scan_patterns(frame, now, 1.0)                                  # startup sync
    extra = frame.iloc[-1:].copy(); extra["time"] = extra["time"] + pd.Timedelta(minutes=5)
    engine._scan_patterns(pd.concat([frame, extra], ignore_index=True), now + timedelta(minutes=5), 1.0)
    assert engine._pattern_executor_kind == "process"
    engine._pattern_future.result(timeout=120); engine.shutdown()


def test_pattern_scan_result_is_picklable_plain_data(config):
    result = compute_patterns(_m5(600), 1.0, 14, 2, 2)
    import pickle
    pickle.loads(pickle.dumps(result))
    assert set(result) >= {"candles", "charts", "volatility", "bars", "elapsed_ms", "bar_time"}


def test_failed_background_scan_is_audited_and_retried(config):
    logger = _Logger(); engine = TradingEngine(FakeClient(), config, logger)
    frame = _m5(600); now = frame.time.iloc[-1].to_pydatetime()
    engine._scan_patterns(frame, now, 1.0)
    failing: Future = Future(); failing.set_exception(RuntimeError("boom"))
    engine._pattern_future = failing; engine._pattern_pending_key = ("x", 1)
    engine._collect_pattern_future(now)
    assert any(kind == "pattern_scan_failed" for kind, _ in logger.events)
    assert engine._pattern_pending_key is None
    assert engine._pattern_retry_after == now + timedelta(seconds=config.analysis.pattern_scan_retry_seconds)
    extra = frame.iloc[-1:].copy(); extra["time"] = extra["time"] + pd.Timedelta(minutes=5)
    longer = pd.concat([frame, extra], ignore_index=True)
    engine._scan_patterns(longer, now + timedelta(seconds=10), 1.0)
    assert engine._pattern_future is None                                    # blocked during back-off
    engine._scan_patterns(longer, now + timedelta(seconds=config.analysis.pattern_scan_retry_seconds + 1), 1.0)
    assert engine._pattern_future is not None                                # retried after back-off
    engine._pattern_future.result(timeout=120); engine.shutdown()


def test_pending_key_resets_after_completion_so_same_bar_new_count_rescans(config):
    engine = TradingEngine(FakeClient(), config, _Logger())
    frame = _m5(600); now = frame.time.iloc[-1].to_pydatetime()
    engine._scan_patterns(frame, now, 1.0)
    reloaded = frame.iloc[1:].reset_index(drop=True)                        # same last bar, different count (history reload)
    engine._scan_patterns(reloaded, now + timedelta(seconds=3), 1.0)
    assert engine._pattern_future is not None
    engine._pattern_future.result(timeout=120)
    engine._scan_patterns(reloaded, now + timedelta(seconds=6), 1.0)
    assert engine._pattern_pending_key is None and engine._pattern_cache["key"][1] == len(reloaded) - 1
    engine.shutdown()


def test_pattern_report_exposes_age_and_status(config):
    engine = TradingEngine(FakeClient(), config, _Logger())
    frame = _m5(600); now = frame.time.iloc[-1].to_pydatetime()
    engine._scan_patterns(frame, now, 1.0)
    report = engine.pattern_scan_report(now + timedelta(seconds=9))
    assert report["status"] == "CURRENT" and report["age_seconds"] == 9.0 and report["bar_time"] is not None


def test_fingerprint_covers_sessions_safety_and_magic(config):
    base = strategy_fingerprint(config)
    for section, field, value in (("sessions", "asia_open", "01:23"), ("safety", "allow_pa_orders", not config.safety.allow_pa_orders),
                                  ("magic", "pa", config.magic.pa + 1)):
        raw = config.model_dump(); raw[section][field] = value
        assert strategy_fingerprint(BotConfig.model_validate(raw)) != base, section


def test_performance_summary_is_scoped_to_fingerprint(config, tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    base = {"kind": "PA", "result_confirmed": 1, "side": "LONG", "session": "LONDON", "volume": 0.01,
            "open_time": "2026-09-01T08:00:00+00:00", "close_time": "2026-09-01T09:00:00+00:00", "pnl": 1.0}
    logger.trade({**base, "ticket": 1, "config_fingerprint": "old"})
    logger.trade({**base, "ticket": 2, "config_fingerprint": "new"})
    start, end = datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 2, tzinfo=UTC)
    assert logger.performance_summary(start, end)["pa_trades"] == 2
    assert logger.performance_summary(start, end, config_fingerprint="new")["pa_trades"] == 1


def test_calibration_state_file_is_authoritative_and_reset_flag_zeroes_it(config, tmp_path: Path):
    client = FakeClient(); manager = ScoutManager(client, config)
    manager.state_path = str(tmp_path / "scouts.json"); manager.calibration_sessions = 4; manager._persist()
    fresh = ScoutManager(client, config); fresh.state_path = manager.state_path
    fresh.calibration_sessions = 19                                          # SQLite fallback value from another account
    fresh.restore(); assert fresh.calibration_sessions == 4
    raw = config.model_dump(); raw["scout_analysis"]["reset_pace_calibration"] = True
    events = []
    reset = ScoutManager(client, BotConfig.model_validate(raw), audit=lambda k, p: events.append(k))
    reset.state_path = manager.state_path; reset.restore()
    assert reset.calibration_sessions == 0 and "pace_calibration_reset" in events


def _trend_bars(timeframe: str, count: int, end: datetime, slope: float) -> pd.DataFrame:
    """Clean stair-step uptrend with pullbacks: higher highs / higher lows on every timeframe."""
    freq = _BarClient.FREQ[timeframe]
    t = pd.date_range(end=end, periods=count, freq=freq, tz="UTC")
    step = np.arange(count)
    wave = np.sin(step / 8.0) * 3.0                                          # pullback rhythm
    p = 2400 + step * slope + wave
    body = np.where(np.diff(p, prepend=p[0]) >= 0, 0.6, -0.6)
    return pd.DataFrame({"time": t, "open": p - body / 2, "high": p + 0.8, "low": p - 0.8, "close": p + body / 2, "tick_volume": 100})


class _TrendClient(_BarClient):
    SLOPE = {"M1": 0.02, "M5": 0.1, "M15": 0.3, "H1": 1.2, "H4": 4.8, "D1": 12.0}

    def get_bars(self, symbol, timeframe, count):
        return _trend_bars(timeframe, count, self.tick.time, self.SLOPE[timeframe])


def test_end_to_end_cycle_produces_a_directional_read_on_a_clean_trend(config):
    reset_history_cache()
    client = _TrendClient(); client.tick = type(client.tick)(datetime(2026, 9, 4, 12, tzinfo=UTC), 2500.0, 2500.2)
    engine = TradingEngine(client, config, _CycleLogger())
    snapshot = engine.run_cycle(client.tick.time)
    assert snapshot.decision.action in {Action.LONG, Action.SHORT, Action.WAIT, Action.NO_TRADE}
    assert snapshot.pa_side is not None or snapshot.confluence != 0, "engine gave no directional read on a clean trend"
    engine.shutdown()


def test_full_cycle_stays_under_three_second_poll(config):
    reset_history_cache()
    client = _BarClient(); engine = TradingEngine(client, config, _CycleLogger())
    engine.run_cycle(client.tick.time)
    timings = []
    for i in range(3):
        if i == 1:
            client.tick = type(client.tick)(client.tick.time + timedelta(minutes=5), client.tick.bid, client.tick.ask)
        t0 = time.perf_counter(); engine.run_cycle(client.tick.time + timedelta(seconds=3 * i)); timings.append(time.perf_counter() - t0)
    engine.shutdown()
    assert max(timings) < config.poll_seconds, timings                       # every cycle, including during a background scan, fits the 3-s poll


def test_firestore_contract_lists_pattern_scan_and_fingerprint(config):
    import json
    contract = json.loads((Path(__file__).parents[1] / "FIRESTORE_SCHEMA.json").read_text())
    required = set(contract["collections"]["sessions"]["reporting_required"])
    reset_history_cache(); client = _BarClient(); engine = TradingEngine(client, config, _CycleLogger())
    snapshot = engine.run_cycle(client.tick.time); engine.shutdown()
    assert required <= set(snapshot.reporting)
    assert set(contract["collections"]["sessions"]["pattern_scan_fields"]) <= set(snapshot.reporting["pattern_scan"])
    assert snapshot.reporting["config_fingerprint"] == engine.strategy_fingerprint
