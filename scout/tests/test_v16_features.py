from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from conftest import FakeClient
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.firestore_sink import FirestoreSink
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import (
    Action, AnalysisSnapshot, Decision, EntryState, Freshness, ScoutSnapshot, SessionName,
    SpreadState, TriggerResult,
)
from xau_mt5_bot.notify import Discord
from xau_mt5_bot.scouts import ScoutManager


class ReportLogger:
    def __init__(self): self.summaries = 0; self.reports = []
    def event(self, *args): pass
    def performance_summary(self, *args): self.summaries += 1; return {"pa_trades": 0, "net_pnl_reference_lot": 0}
    def scout_performance_summary(self, *args): return {"leader_accuracy_pct": None}
    def report_once(self, kind, key, payload): self.reports.append((kind, key, payload)); return True


def test_rolling_range_stays_fast_after_full_retrace(config):
    client = FakeClient(); manager = ScoutManager(client, config); now = client.tick.time
    manager.open_session(SessionName.LONDON, now - timedelta(minutes=30)); manager.session_open_price = 2500
    manager.price_samples = [((now - timedelta(minutes=20)).isoformat(), 2500),
                             ((now - timedelta(minutes=10)).isoformat(), 2510)]
    client.tick = client.tick.__class__(now, 2500.0, 2500.2)
    scout = manager.snapshot(None)
    assert scout.market_speed == "FAST" and scout.pace_range >= 10


def test_session_open_grace_is_warmup_not_slow(config):
    client = FakeClient(); manager = ScoutManager(client, config); now = client.tick.time
    manager.open_session(SessionName.ASIA, now - timedelta(minutes=5))
    scout = manager.snapshot(None)
    assert scout.market_speed == "WARMUP" and "slow hold is not active" in scout.guidance


def test_report_sql_is_skipped_without_boundary(config):
    logger = ReportLogger(); engine = TradingEngine(FakeClient(), config, logger)
    engine.session_boundary_event = False; engine.startup_cycle = False; engine.closed_sessions = []
    snapshot = SimpleNamespace(session=SessionName.LONDON, pa_side=None, scout=SimpleNamespace(leader="NONE", market_speed="NORMAL"))
    engine._emit_reports(datetime(2026, 9, 2, tzinfo=UTC), snapshot, [])
    assert logger.summaries == 0 and logger.reports == []


def test_friday_next_week_report_uses_ny_close_and_session_pending(config):
    logger = ReportLogger(); engine = TradingEngine(FakeClient(), config, logger)
    end = datetime(2026, 9, 4, 21, tzinfo=UTC); start = end - timedelta(hours=9)
    engine.session_boundary_event = True; engine.closed_sessions = [(SessionName.NEW_YORK, end, {"leader": "BUY"})]
    engine._session_start_for = lambda session, value: start
    engine.positions.pending_finalize = {"9": {"ticket": 9, "kind": "PA", "session": "NEW_YORK", "side": "LONG",
                                                   "open_time": (start + timedelta(hours=1)).isoformat()}}
    snapshot = SimpleNamespace(session=SessionName.CLOSED, pa_side=None, scout=SimpleNamespace(leader="BUY", market_speed="NORMAL"))
    engine._emit_reports(end, snapshot, [])
    kinds = {x[0] for x in logger.reports}
    session_payload = next(x[2] for x in logger.reports if x[0] == "session_summary")
    next_payload = next(x[2] for x in logger.reports if x[0] == "next_week_open_report")
    assert session_payload["pending_pa_count"] == 1
    assert "next_week_open_report" in kinds and next_payload["friday_close_time"] == end.isoformat()


def test_strength_fallback_is_audited_once(config):
    client = FakeClient(); client.calc_profit = lambda *args: None; events = []
    manager = ScoutManager(client, config, lambda kind, payload: events.append(kind)); now = client.tick.time
    manager.open_session(SessionName.LONDON, now - timedelta(minutes=20))
    manager.snapshot(None); manager.snapshot(None)
    assert events.count("scout_strength_fallback") == 1


def test_weekly_scout_stats_include_mfe_mae_and_leader_accuracy(tmp_path):
    logger = AuditLogger(str(tmp_path / "db.sqlite3"), str(tmp_path / "a.jsonl"))
    start = datetime(2026, 9, 1, tzinfo=UTC); end = start + timedelta(days=7)
    logger.event("scout_session_stats", {"session": "LONDON", "open_time": start.isoformat(),
                 "close_time": (start + timedelta(hours=8)).isoformat(), "leader": "BUY",
                 "buy_mfe": 10, "buy_mae": -2, "sell_mfe": 3, "sell_mae": -9})
    logger.trade({"ticket": 1, "kind": "PA", "side": "LONG", "session": "LONDON", "volume": .1,
                  "open_time": start, "close_time": start + timedelta(hours=2), "pnl": 20, "result_confirmed": 1})
    result = logger.scout_performance_summary(start, end)
    assert result["leader_accuracy_pct"] == 100.0
    assert result["sessions"]["LONDON"]["average_buy_mfe"] == 10


def test_go_field_and_reference_lot_are_in_firestore_summary(tmp_path):
    scout = ScoutSnapshot(SessionName.LONDON, buy_pnl_reference_lot=50, sell_pnl_reference_lot=-60)
    snapshot = AnalysisSnapshot(datetime.now(UTC), "XAUUSD", SessionName.LONDON, 2500, 2500.2, .2,
        Freshness.LIVE, {}, [], [], [], [], None, 0, scout, EntryState.NOT_IN_SETUP,
        TriggerResult(False, "NONE"), None, Decision(Action.LONG, "ok", datetime.now(UTC)))
    snapshot.go_status = "GO"; snapshot.reporting = {"reference_lot": 1.0, "daily_target_usd": 500.0}
    sink = FirestoreSink(str(tmp_path / "missing.json"))
    summary = sink._summary(snapshot)
    assert summary["final"]["go"] == "GO" and summary["reporting"]["reference_lot"] == 1.0


def test_discord_collapses_leg_events_and_throttles_pairs(monkeypatch):
    discord = Discord("MISSING", scout_pair_min_interval=60); sent = []
    monkeypatch.setattr(discord, "send", lambda value="", embed=None: sent.append(embed or value) or True)
    discord.event("scout_order", {"session": "ASIA"})
    discord.event("scout_session_open", {"session": "ASIA"})
    discord.event("scout_session_open", {"session": "ASIA"})
    assert len(sent) == 1 and "SCOUTS PLACED" in sent[0]["title"]            # v3.2.0: embed card


def test_forward_test_risk_and_pattern_lookback_config(config):
    assert config.risk.daily_max_loss_percent == 2
    assert config.risk.max_consecutive_losses == 3
    assert config.risk.emergency_scout_sl_price == 20
    assert config.analysis.pattern_lookback_days == 30


def test_vue_schema_rules_and_windows_guide_are_packaged():
    root = Path(__file__).resolve().parents[1]
    assert "final.go" in (root / "FIRESTORE_SCHEMA.md").read_text()
    assert "allow write: if false" in (root / "firestore.rules").read_text()
    guide = (root / "WINDOWS_DEPLOYMENT.md").read_text()
    assert "Task Scheduler" in guide and "NSSM" in guide and "supervisor.py" in guide
