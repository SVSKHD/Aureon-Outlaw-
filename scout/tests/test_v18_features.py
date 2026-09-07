from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from conftest import FakeClient
from xau_mt5_bot.candles import detect_candlestick_patterns
from xau_mt5_bot.config import BotConfig
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.fingerprint import strategy_fingerprint
from xau_mt5_bot.history import analysis_window, pattern_window
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.scouts import ScoutManager
from xau_mt5_bot.structure import analyze_structure


class _Logger:
    def __init__(self): self.events = []; self.context = {}
    def event(self, kind, payload): self.events.append((kind, payload))
    def confirmed_trade_count(self, fp=None): return 0


def _m5(n: int, start="2026-08-01") -> pd.DataFrame:
    rng = np.random.default_rng(7)
    t = pd.date_range(start, periods=n, freq="5min", tz="UTC")
    p = 3300 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame({"time": t, "open": p, "high": p + 0.4, "low": p - 0.4, "close": p + rng.normal(0, 0.1, n), "tick_volume": 100})


def test_per_cycle_window_is_small_and_pattern_window_is_long(config):
    frame = _m5(9000)
    assert len(analysis_window({"M5": frame}, config)["M5"]) == 1200
    long = pattern_window(frame, config, frame.time.iloc[-1].to_pydatetime())
    assert 8600 <= len(long) <= 8640


def test_per_cycle_structure_and_candles_stay_under_delay_budget(config):
    frame = analysis_window({"M5": _m5(9000)}, config)["M5"]
    t0 = time.perf_counter()
    analyze_structure(frame, "M5", 2, 2)
    detect_candlestick_patterns(frame.reset_index(drop=True), 14)
    elapsed = time.perf_counter() - t0
    assert elapsed < config.analysis.max_trigger_detection_delay_seconds / 3, f"per-cycle analysis took {elapsed:.2f}s"


def test_pattern_scan_is_cached_until_a_new_m5_bar_closes(config):
    engine = TradingEngine(FakeClient(), config, _Logger())
    frame = _m5(2000)
    now = frame.time.iloc[-1].to_pydatetime()
    first = engine._scan_patterns(frame, now, 1.0)
    engine.last_pattern_scan_ms = -1.0
    second = engine._scan_patterns(frame, now + timedelta(seconds=5), 1.0)
    assert first == second and engine.last_pattern_scan_ms == -1.0       # cache hit: no rescan
    extra = frame.iloc[-1:].copy(); extra["time"] = extra["time"] + pd.Timedelta(minutes=5)
    longer = pd.concat([frame, extra], ignore_index=True)
    served = engine._scan_patterns(longer, now + timedelta(minutes=5), 1.0)
    assert served == first                                                 # new bar: previous result served immediately
    assert engine._pattern_future is not None
    engine._pattern_future.result(timeout=120)
    engine._scan_patterns(longer, now + timedelta(minutes=5, seconds=5), 1.0)
    assert engine._pattern_cache["key"][1] == len(longer) - 1            # background rescan landed on the new bar
    assert engine.last_pattern_scan_ms >= 0
    engine.shutdown()


def test_calibration_sessions_survive_restart(config, tmp_path: Path):
    client = FakeClient(); manager = ScoutManager(client, config)
    manager.state_path = str(tmp_path / "scouts.json")
    manager.calibration_sessions = 7; manager._persist()
    fresh = ScoutManager(client, config); fresh.state_path = manager.state_path; fresh.restore()
    assert fresh.calibration_sessions == 7


def test_research_scale_values_are_currency_and_price_separated(config):
    rep = config.reporting
    assert rep.research_daily_price_move == 5.0 and rep.research_daily_usd == 500.0 and rep.research_reference_lot == 1.0
    assert not hasattr(rep, "max_manual_risk_usd") and not hasattr(rep, "max_manual_risk_to_daily_target_ratio")


def test_strategy_fingerprint_changes_with_parameters_and_scopes_counts(config, tmp_path: Path):
    fp = strategy_fingerprint(config)
    raw = config.model_dump(); raw["analysis"]["min_confluence"] = config.analysis.min_confluence + 1
    assert strategy_fingerprint(BotConfig.model_validate(raw)) != fp
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    base = {"kind": "PA", "result_confirmed": 1, "side": "LONG", "session": "LONDON", "open_time": "2026-09-01T08:00:00+00:00",
            "close_time": "2026-09-01T09:00:00+00:00", "pnl": 1.0}
    logger.trade({**base, "ticket": 1, "config_fingerprint": "old0000000000"})
    logger.context = {"config_fingerprint": fp}
    logger.trade({**base, "ticket": 2})
    assert logger.confirmed_trade_count() == 2
    assert logger.confirmed_trade_count(fp) == 1
    assert logger.confirmed_trade_count("old0000000000") == 1


def test_detection_delay_applies_to_first_detection_only():
    """Source-level guard: a carried trigger must not be re-vetoed on later cycles."""
    src = Path(__file__).resolve().parents[1] / "src" / "xau_mt5_bot" / "engine.py"
    text = src.read_text()
    assert "newly_confirmed = trigger.confirmed and (self.last_trigger is None or not self.last_trigger.confirmed)" in text
    assert "if newly_confirmed:" in text


class _BarClient(FakeClient):
    """FakeClient with synthetic native history so a full run_cycle can execute end-to-end."""
    FREQ = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}

    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        rng = np.random.default_rng(hash(timeframe) % 1000)
        end = self.tick.time
        t = pd.date_range(end=end, periods=count, freq=self.FREQ[timeframe], tz="UTC")
        p = 2500 + np.cumsum(rng.normal(0, 0.3, count))
        return pd.DataFrame({"time": t, "open": p, "high": p + 0.5, "low": p - 0.5,
                             "close": p + rng.normal(0, 0.1, count), "tick_volume": 100})


class _CycleLogger(_Logger):
    def snapshot(self, *a): pass
    def order(self, *a): pass
    def trade(self, *a): pass


def test_full_cycle_runs_under_poll_budget_with_thirty_day_history(config):
    from xau_mt5_bot.history import reset_history_cache
    reset_history_cache()
    client = _BarClient(); engine = TradingEngine(client, config, _CycleLogger())
    t0 = time.perf_counter(); first = engine.run_cycle(client.tick.time); cold = time.perf_counter() - t0
    t0 = time.perf_counter(); second = engine.run_cycle(client.tick.time + timedelta(seconds=5)); warm = time.perf_counter() - t0
    client.tick = type(client.tick)(client.tick.time + timedelta(minutes=5), client.tick.bid, client.tick.ask)
    t0 = time.perf_counter(); third = engine.run_cycle(client.tick.time); bar_close = time.perf_counter() - t0
    assert third.decision is not None
    assert bar_close < config.analysis.max_trigger_detection_delay_seconds, f"bar-close cycle {bar_close:.2f}s"
    assert first.decision is not None and second.decision is not None
    assert warm < config.poll_seconds, f"warm cycle {warm:.2f}s exceeds poll_seconds={config.poll_seconds}"
    assert warm < config.analysis.max_trigger_detection_delay_seconds
    assert cold >= warm
