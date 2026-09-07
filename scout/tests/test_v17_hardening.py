from __future__ import annotations

import json
import os
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from xau_mt5_bot.config import BotConfig, load_environment
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.firestore_sink import FirestoreSink
from xau_mt5_bot.history import analysis_window, load_native_history, reset_history_cache
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import Action, AnalysisSnapshot, Decision, EntryState, Freshness, ScoutSnapshot, SessionName, TriggerResult
from xau_mt5_bot.notify import Discord
from xau_mt5_bot.position_manager import PositionManager
from xau_mt5_bot.scouts import ScoutManager
from xau_mt5_bot.sessions import SessionEngine

from conftest import FakeClient


class ReportLogger:
    def __init__(self): self.reports = []; self.summaries = 0
    def event(self, *_): pass
    def performance_summary(self, *args): self.summaries += 1; return {"pa_trades": 0, "net_pnl_reference_lot": 0}
    def scout_performance_summary(self, *_): return {"leader_accuracy_pct": None}
    def report_once(self, kind, key, payload): self.reports.append((kind, key, payload)); return True
    def report_exists(self, *_): return False


def _snapshot(now: datetime) -> SimpleNamespace:
    return SimpleNamespace(session=SessionName.CLOSED, pa_side=None,
                           scout=SimpleNamespace(leader="NONE", market_speed="UNKNOWN"))


def test_pattern_window_is_really_thirty_calendar_days(config):
    frame = pd.DataFrame({"time": pd.date_range("2026-01-01", periods=9000, freq="5min", tz="UTC")})
    from xau_mt5_bot.history import pattern_window
    now = frame.time.iloc[-1].to_pydatetime()
    assert len(pattern_window(frame, config, now)) == 8640          # long pattern window: 30 calendar days
    assert len(analysis_window({"M5": frame}, config)["M5"]) == 1200  # fast per-cycle window stays small (v1.8.0)
    raw = config.model_dump(); raw["history_bars"]["M5"] = 1200
    with pytest.raises(ValueError, match="history_bars.M5"):
        BotConfig.model_validate(raw)


def test_short_broker_history_is_marked_incomplete(config):
    class ShortHistory:
        def get_bars(self, symbol, timeframe, count):
            n = count - 10
            return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
                                 "open": 1, "high": 2, "low": 0, "close": 1})
    reset_history_cache()
    config.analysis.pattern_lookback_days = 1; config.history_bars = {"M5": 300}
    frame = load_native_history(ShortHistory(), config, force_full=True)["M5"]
    assert frame.attrs["history_complete"] is False and frame.attrs["received_bars"] == 290


def test_startup_catches_weekly_and_friday_reports_without_memory(config):
    logger = ReportLogger(); engine = TradingEngine(FakeClient(), config, logger)
    engine.startup_cycle = True; engine.session_boundary_event = False; engine.closed_sessions = []
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    engine._emit_reports(now, _snapshot(now), [])
    kinds = {kind for kind, _, _ in logger.reports}
    assert {"weekly_report", "next_week_open_report"} <= kinds
    assert all(payload.get("go") in {"GO", "NO-GO"} and "go_basis" in payload for _, _, payload in logger.reports)   # v2.0.0: historical GO preserved
    assert all(payload["report_status"] == "CATCH_UP" for _, _, payload in logger.reports)


def test_restart_is_not_a_pa_session_end(config):
    engine = TradingEngine(FakeClient(), config, ReportLogger())
    engine._handle_sessions(datetime(2026, 9, 4, 12, tzinfo=UTC))
    assert engine.session_boundary_event is False
    assert engine.pa_session_end_event is False


def test_scout_realized_loss_updates_combined_daily_lock(tmp_path, config):
    client = FakeClient(); manager = PositionManager(client, config, state_dir=str(tmp_path), account_key="acct")
    opened = datetime(2026, 9, 4, 10, tzinfo=UTC)
    manager.pending_finalize["55"] = {"ticket": 55, "kind": "SCOUT", "side": "LONG", "session": "LONDON",
        "magic": config.magic.scout_london, "volume": .01, "original_volume": .01, "open_time": opened.isoformat(),
        "open_price": 2500.0, "sl": 2480.0, "tp": 0, "mfe": 0, "mae": -20, "realized": 0,
        "trading_date": "2026-09-04", "partial_exits": [], "exit_deal_ids": []}
    client.deals[55] = [{"deal_ticket": 1, "price": 2480.0, "profit": -20, "commission": 0, "swap": 0,
                         "net": -20, "time": opened + timedelta(hours=1), "reason": "SL", "volume": .01}]
    manager._finalize_pending(2480)
    assert manager.day["pnl"] == -20 and manager.day["scout_pnl"] == -20
    assert manager.day["consecutive_losses"] == 1


def test_combined_day_lock_blocks_scout_order_gate(config):
    client = FakeClient(); engine = TradingEngine(client, config, ReportLogger())
    engine.cycle_now = client.tick.time
    tdate = engine.sessions.broker_trading_date(client.tick.time).isoformat()
    engine.positions.day = {"date": tdate, "pnl": -1001, "pa_pnl": 0, "scout_pnl": -1001,
                            "trades": 0, "per_session": {}, "consecutive_losses": 0, "locked": None}
    allowed, reason = engine.orders_allowed()
    assert not allowed and "daily max loss" in reason


def test_closed_scout_loss_is_finalized_before_next_pair_opens(config):
    config.risk.daily_max_loss_percent = 0.0001
    client = FakeClient(); engine = TradingEngine(client, config, ReportLogger())
    now = client.tick.time  # 12:00 UTC = New York start on this date
    engine.cycle_now = now; engine.last_cycle = now - timedelta(minutes=1)
    engine.scouts.open_session(SessionName.LONDON, now - timedelta(hours=5))
    tdate = engine.sessions.broker_trading_date(now).isoformat()
    for p in client.positions("XAUUSD", config.magic.scout_london):
        engine.positions.track(p, "SCOUT", "LONDON", tdate=tdate)
    engine._handle_sessions(now)
    assert engine.positions.day["scout_pnl"] < 0
    assert client.positions("XAUUSD", config.magic.scout_new_york) == []
    assert engine.pending_boundaries


def test_velocity_is_unsigned_and_direction_is_separate(config):
    client = FakeClient(); manager = ScoutManager(client, config)
    now = client.tick.time; manager.open_session(SessionName.LONDON, now - timedelta(minutes=20))
    manager.session_open_price = 2510; manager.price_samples = [((now - timedelta(minutes=10)).isoformat(), 2510),
                                                                 ((now - timedelta(minutes=5)).isoformat(), 2520)]
    snap = manager.snapshot(None)
    assert snap.velocity >= 0 and snap.velocity_direction == "DOWN" and snap.pace_range >= 10


def test_broker_holiday_and_early_close_calendar(config):
    config.sessions.market_holidays = ["2026-12-25"]
    config.sessions.market_early_closes = {"2026-11-27": "13:00"}
    sessions = SessionEngine(config.sessions)
    assert not sessions.calendar_open(datetime(2026, 12, 25, 15, tzinfo=UTC))
    assert not sessions.calendar_open(datetime(2026, 11, 27, 19, tzinfo=UTC))


def test_env_file_overrides_stale_inherited_value(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK", "stale")
    path = tmp_path / ".env"; path.write_text("DISCORD_WEBHOOK=fresh\n")
    loaded = load_environment(path)
    assert os.environ["DISCORD_WEBHOOK"] == "fresh" and loaded["DISCORD_WEBHOOK"] == "fresh"


def test_netting_scout_failure_is_permanent_not_retried(config):
    result = ScoutManager(FakeClient(hedging=False), config).open_session(SessionName.ASIA, datetime.now(UTC))
    assert not result.success and not result.retryable


def test_firestore_series_is_bounded_and_contract_matches(config):
    contract = json.loads((Path(__file__).parents[1] / "FIRESTORE_SCHEMA.json").read_text())
    sink = FirestoreSink("missing", series_seconds=0, series_max_points=10); sink.db = object(); sink._last_push = time.time()
    now = datetime.now(UTC)
    scout = ScoutSnapshot(SessionName.LONDON)
    snap = AnalysisSnapshot(now, "XAUUSD", SessionName.LONDON, 1, 2, 1, Freshness.LIVE, {}, [], [], [], [], None, 0,
                            scout, EntryState.NOT_IN_SETUP, TriggerResult(False, "NONE"), None,
                            Decision(Action.WAIT, "wait", now))
    for i in range(25):
        snap.timestamp = now + timedelta(seconds=i); sink.on_snapshot(snap, now.date())
    series = next(iter(sink._series.values()))
    assert all(len(values) <= contract["collections"]["sessions"]["series_document"]["max_points"] for values in series.values())
    summary = sink._summary(snap)
    assert set(contract["collections"]["sessions"]["required"]) <= set(summary)
    assert set(contract["collections"]["sessions"]["scouts_required"]) <= set(summary["scouts"])


def test_discord_chunks_and_retries_429(monkeypatch):
    monkeypatch.setenv("WEBHOOK", "https://example.invalid")
    calls = []
    def fake_open(req, timeout):
        calls.append(json.loads(req.data)["content"])
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "rate", {"Retry-After": "0"}, None)
        return SimpleNamespace(read=lambda: b"")
    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    discord = Discord("WEBHOOK", retry_count=1, retry_backoff_seconds=0)
    assert discord.send("x" * 2500)
    assert len(calls) == 3 and max(map(len, calls)) <= 1900


def test_sqlite_utc_retention_and_recovery(tmp_path):
    logger = AuditLogger(str(tmp_path / "audit.db"), str(tmp_path / "audit.jsonl"), retention_days=30)
    old = (datetime.now(UTC) - timedelta(days=40)).astimezone().isoformat()
    with logger._connect() as db:
        db.execute("INSERT INTO events(timestamp,event_type,payload_json) VALUES(?,?,?)", (old, "old", "{}"))
    logger._last_prune = 0; logger.prune()
    assert logger.event_count("old") == 0


def test_volume_budget_rejects_future_oversubscription(config):
    raw = config.model_dump(); raw["risk"].update({"scout_lot": .5, "fixed_pa_lot": .1, "max_total_volume": 1.0})
    with pytest.raises(ValueError, match="scout-pair"):
        BotConfig.model_validate(raw)
