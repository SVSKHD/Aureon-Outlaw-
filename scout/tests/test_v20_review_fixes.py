from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import xau_mt5_bot.mt5_client as adapter
from conftest import FakeClient
from test_mt5_adapter_contract import MockMT5
from test_v18_features import _BarClient, _CycleLogger, _Logger
from xau_mt5_bot.config import BotConfig
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.firestore_sink import FirestoreSink
from xau_mt5_bot.history import reset_history_cache
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import Action, SessionName, TriggerResult
from xau_mt5_bot.notify import Discord
from xau_mt5_bot.position_manager import PositionManager
from xau_mt5_bot.scouts import ScoutManager
from xau_mt5_bot.sessions import SessionEngine
from xau_mt5_bot.statefile import atomic_write_json, load_json_state


# 1 ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("flags,expected", [(1, 0), (2, 1), (3, 1), (0, 2)])   # FOK-only→FOK, IOC-only→IOC, both→IOC, none→RETURN
def test_filling_mode_maps_symbol_flags_to_order_enum(monkeypatch, flags, expected):
    fake = MockMT5(); monkeypatch.setattr(adapter, "mt5", fake)
    fake.symbol_info = lambda symbol: SimpleNamespace(visible=True, trade_mode=4, point=.01, trade_stops_level=0, trade_freeze_level=0, filling_mode=flags)
    assert adapter.MT5Client()._filling_mode("XAUUSD") == expected


# 2 / 3 -----------------------------------------------------------------------------------------------------------
class _LossClient(FakeClient):
    """A PA position that was stopped out between cycles: gone from positions(), loss visible in deal history."""
    def __init__(self):
        super().__init__(); self.finalized = False
    def closed_deals(self, ticket, *a, **k):
        return [{"entry": 1, "ticket": 9, "price": 2480.0, "profit": -1200.0, "commission": 0, "swap": 0,
                 "time": self.tick.time, "reason": "SL", "volume": 0.01}]


def test_realized_loss_between_cycles_locks_before_any_order(config, tmp_path: Path):
    config.risk.daily_max_loss_percent = 2.0
    client = _LossClient(); events = []
    pm = PositionManager(client, config, audit=lambda k, p: events.append(k), state_dir=str(tmp_path), account_key="t")
    pos = SimpleNamespace(ticket=501, symbol="XAUUSD", magic=config.magic.pa, volume=0.01, type=0, price_open=2500.0, sl=2480.0, tp=0.0, profit=0.0, time=int(client.tick.time.timestamp()))
    pm.track(pos, "PA", "LONDON", side="LONG", setup_id="s1", tdate="2026-09-04")
    engine = TradingEngine(client, config, _Logger()); engine.positions = pm; engine.cycle_now = client.tick.time
    # position vanished (SL hit) — nothing has finalised yet, the naive lock check would still say "ok"
    assert pm.risk_allowed("2026-09-04", 50000)[0] is True
    ok, why = engine.orders_allowed()
    assert ok is False and "daily max loss" in why


# 4 ---------------------------------------------------------------------------------------------------------------
class _StickyCloseClient(FakeClient):
    def close_position(self, ticket, magic):
        from xau_mt5_bot.mt5_client import OrderResult
        return OrderResult(False, 10006, ticket, None, "rejected")


def test_failed_scout_close_does_not_count_as_a_calibration_session(config):
    client = _StickyCloseClient(); events = []
    manager = ScoutManager(client, config, audit=lambda k, p: events.append(k))
    manager.current_session = SessionName.LONDON
    magic = manager.magic(SessionName.LONDON)
    client.send_market("XAUUSD", "BUY", 0.01, magic, "SCOUT_LONDON_BUY"); client.send_market("XAUUSD", "SELL", 0.01, magic, "SCOUT_LONDON_SELL")
    for _ in range(3):
        result = manager.close_session(SessionName.LONDON)
        assert result.success is False
    assert manager.calibration_sessions == 0
    assert events.count("scout_session_stats") == 0
    assert len(client.positions("XAUUSD", magic)) == 2


def test_confirmed_scout_close_counts_exactly_once(config):
    client = FakeClient(); events = []
    manager = ScoutManager(client, config, audit=lambda k, p: events.append(k))
    manager.current_session = SessionName.LONDON; magic = manager.magic(SessionName.LONDON)
    client.send_market("XAUUSD", "BUY", 0.01, magic, "a"); client.send_market("XAUUSD", "SELL", 0.01, magic, "b")
    assert manager.close_session(SessionName.LONDON).success is True
    assert manager.calibration_sessions == 1 and events.count("scout_session_stats") == 1


# 5 ---------------------------------------------------------------------------------------------------------------
def test_new_account_does_not_inherit_other_accounts_calibration(config, tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    for _ in range(20):
        logger.event("scout_session_stats", {"session": "ASIA", "account": "OLD", "symbol": "XAUUSD", "config_fingerprint": "fpA"})
    assert logger.scoped_event_count("scout_session_stats", "OLD", "XAUUSD", "fpA") == 20
    assert logger.scoped_event_count("scout_session_stats", "NEW", "XAUUSD", "fpA") == 0
    assert logger.scoped_event_count("scout_session_stats", "OLD", "XAUUSD", "fpB") == 0


def test_calibration_file_from_other_scope_is_reset(config, tmp_path: Path):
    client = FakeClient(); events = []
    a = ScoutManager(client, config); a.state_path = str(tmp_path / "s.json"); a.account_key = "A"; a.fingerprint = "fpA"
    a.calibration_sessions = 12; a._persist()
    b = ScoutManager(client, config, audit=lambda k, p: events.append(k)); b.state_path = a.state_path; b.account_key = "A"; b.fingerprint = "fpB"
    b.restore()
    assert b.calibration_sessions == 0 and b.calibration_source == "reset" and "pace_calibration_reset" in events


# 6 ---------------------------------------------------------------------------------------------------------------
def test_trigger_detectors_return_their_confirming_bar_time(config):
    from conftest import bars
    from xau_mt5_bot.trigger import detect_m5_confirmation
    from xau_mt5_bot.models import Pivot, Side, Zone
    start = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
    m5 = bars("2026-09-04 08:00", [(2500, 2501, 2499, 2500.5)] * 3 + [(2500.5, 2503, 2500.4, 2502.8), (2502.8, 2503, 2501.9, 2502.6)] + [(2502.6, 2503, 2502, 2502.5)] * 6, freq="5min")
    zone = Zone("DEMAND", 2498.0, 2501.0, start - timedelta(hours=2), "M5", "OB", "fresh")
    pivot = Pivot(start, start + timedelta(minutes=10), 2502.0, "HIGH", 0)
    result = detect_m5_confirmation(m5, zone, Side.LONG, start, [pivot])
    if result.confirmed:
        assert result.trigger_bar_time is not None
        assert result.trigger_bar_time < pd.Timestamp(m5.iloc[-1].time).to_pydatetime()   # not the newest bar
    assert "trigger_bar_time" in TriggerResult.__dataclass_fields__


def test_engine_measures_delay_from_the_confirming_bar_not_the_newest():
    src = (Path(__file__).resolve().parents[1] / "src" / "xau_mt5_bot" / "engine.py").read_text()
    assert "bar_open = trigger.trigger_bar_time or" in src
    assert "self.last_trigger_time = trigger.trigger_bar_time or" in src


# 7 ---------------------------------------------------------------------------------------------------------------
def test_cold_start_cycle_never_places_a_pa_order(config):
    reset_history_cache(); client = _BarClient(); engine = TradingEngine(client, config, _CycleLogger())
    assert engine.startup_cycle is True
    snapshot = engine.run_cycle(client.tick.time)
    assert snapshot.decision.action not in {Action.LONG, Action.SHORT}
    assert not any(s["magic"] == config.magic.pa for s in client.sent)
    assert engine.startup_cycle is False
    engine.shutdown()


# 8 ---------------------------------------------------------------------------------------------------------------
def test_state_file_survives_truncated_write(tmp_path: Path):
    path = tmp_path / "positions.json"
    atomic_write_json(path, {"tracked": {"1": {"kind": "PA"}}})
    atomic_write_json(path, {"tracked": {"1": {"kind": "PA"}, "2": {"kind": "SCOUT"}}})
    path.write_text('{"tracked": {"1": {"ki')                                  # simulate a kill mid-write of the main file
    data, source = load_json_state(path)
    assert source == "backup" and "1" in data["tracked"]
    assert not (tmp_path / "positions.json.tmp").exists()


def test_position_manager_uses_atomic_writes_and_audits_recovery(config, tmp_path: Path):
    events = []
    pm = PositionManager(FakeClient(), config, audit=lambda k, p: events.append(k), state_dir=str(tmp_path), account_key="t")
    pm.day["2026-09-04"] = {"pnl": -5.0}; pm._save(); pm._save()
    pm.path.write_text("{corrupt")
    pm2 = PositionManager(FakeClient(), config, audit=lambda k, p: events.append(k), state_dir=str(tmp_path), account_key="t")
    assert "state_file_recovered" in events and pm2.day.get("2026-09-04", {}).get("pnl") == -5.0


# 9 ---------------------------------------------------------------------------------------------------------------
def test_early_close_emits_a_close_boundary(config):
    config.sessions.market_early_closes = {"2026-09-04": "13:00"}
    engine = SessionEngine(config.sessions)
    closes = [b for b in engine.boundaries_for_utc_day(datetime(2026, 9, 4).date()) if b.kind == "CLOSE"]
    assert datetime(2026, 9, 4, 17, 0, tzinfo=UTC) in {b.timestamp for b in closes}           # 13:00 NY EDT
    assert engine.session_at(datetime(2026, 9, 4, 17, 30, tzinfo=UTC)) == SessionName.CLOSED
    assert any(b.timestamp == datetime(2026, 9, 4, 17, 0, tzinfo=UTC) for b in engine.events_between(datetime(2026, 9, 4, 16, 59, tzinfo=UTC), datetime(2026, 9, 4, 17, 1, tzinfo=UTC)))


# 10 --------------------------------------------------------------------------------------------------------------
def test_research_translation_is_dimensionally_correct_and_never_gates(config):
    reset_history_cache(); client = _BarClient(); engine = TradingEngine(client, config, _CycleLogger())
    snapshot = engine.run_cycle(client.tick.time); engine.shutdown()
    rep = snapshot.reporting
    assert rep["research_daily_usd"] == 500.0 and rep["research_daily_price_move"] == 5.0 and rep["account_mode"] == "DEMO_ONLY"
    tr = rep["research_translation"]
    assert tr["research_daily_move_pnl_usd"] == pytest.approx(500.0)          # FakeClient: $1 move × 1 lot × 100
    assert tr["warnings"] == [] and "NOT_A_TRADING_PERMISSION" in tr["scale"]
    assert not any(k.startswith("funded") or k.startswith("manual_") for k in rep)


def test_research_translation_warns_when_config_is_inconsistent(config):
    config.reporting.research_daily_usd = 5.0
    reset_history_cache(); client = _BarClient(); logger = _CycleLogger(); engine = TradingEngine(client, config, logger)
    engine.run_cycle(client.tick.time); engine.shutdown()
    payload = next(p for k, p in logger.events if k == "research_translation")
    assert any("research_daily_usd" in w for w in payload["warnings"])


# 11 --------------------------------------------------------------------------------------------------------------
def test_tracked_position_carries_opening_fingerprint(config, tmp_path: Path):
    pm = PositionManager(FakeClient(), config, state_dir=str(tmp_path), account_key="t", config_fingerprint="fpOPEN")
    pos = SimpleNamespace(ticket=1, symbol="XAUUSD", magic=config.magic.pa, volume=0.01, type=0, price_open=2500.0, sl=2490.0, tp=0.0, profit=0.0)
    pm.track(pos, "PA", "LONDON", side="LONG", setup_id="s", tdate="2026-09-04")
    assert pm.tracked["1"]["config_fingerprint"] == "fpOPEN"
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl")); logger.context = {"config_fingerprint": "fpCLOSE"}
    logger.trade({**pm.tracked["1"], "result_confirmed": 1, "close_time": "2026-09-04T10:00:00+00:00", "pnl": 1.0})
    assert logger.confirmed_trade_count("fpOPEN") == 1 and logger.confirmed_trade_count("fpCLOSE") == 0


# 12 --------------------------------------------------------------------------------------------------------------
def test_scout_weekly_stats_are_scoped(config, tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    base = {"session": "ASIA", "leader": "BUY", "open_time": "2026-09-01T00:00:00+00:00", "close_time": "2026-09-01T08:00:00+00:00",
            "buy_mfe": 1, "buy_mae": 0, "sell_mfe": 0, "sell_mae": 1, "symbol": "XAUUSD"}
    logger.event("scout_session_stats", {**base, "account": "A", "config_fingerprint": "fpA"})
    logger.event("scout_session_stats", {**base, "account": "B", "config_fingerprint": "fpA"})
    start, end = datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
    assert logger.scout_performance_summary(start, end)["sessions"]["ASIA"]["sessions"] == 2
    assert logger.scout_performance_summary(start, end, account="A", config_fingerprint="fpA")["sessions"]["ASIA"]["sessions"] == 1


# 13 --------------------------------------------------------------------------------------------------------------
def test_session_summary_preserves_go_that_applied(config, tmp_path: Path):
    client = FakeClient(); engine = TradingEngine(client, config, _Logger())
    engine.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    t = datetime(2026, 9, 4, 9, tzinfo=UTC)
    engine._record_session_go(SessionName.LONDON, "NO-GO", t)
    engine._record_session_go(SessionName.LONDON, "GO", t + timedelta(minutes=5))
    engine._record_session_go(SessionName.LONDON, "NO-GO", t + timedelta(minutes=10))
    report = engine._session_go_report(SessionName.LONDON)
    assert report["go"] == "GO" and report["go_cycles"] == 1 and report["cycles_observed"] == 3
    assert engine._session_go_report(SessionName.LONDON)["go"] == "GO"           # read-only until consumed (v3.0.0)
    engine._consume_session_go(report)
    assert engine._session_go_report(SessionName.LONDON)["go"] == "NO-GO"        # tally consumed


# 14 / 15 / 16 ----------------------------------------------------------------------------------------------------
def test_firestore_schema_version_matches_contract():
    contract = json.loads((Path(__file__).resolve().parents[1] / "FIRESTORE_SCHEMA.json").read_text())
    assert FirestoreSink.SCHEMA_VERSION == contract["version"] == "3.3.0"


class _Doc:
    def __init__(self, store, path): self.store, self.path = store, path
    def collection(self, name): return _Col(self.store, self.path + "/" + name)
    def set(self, data, merge=False): self.store[self.path] = {**self.store.get(self.path, {}), **data} if merge else dict(data)
    def get(self):
        data = self.store.get(self.path); return SimpleNamespace(exists=data is not None, to_dict=lambda: data)

class _Col:
    def __init__(self, store, path): self.store, self.path = store, path
    def document(self, name): return _Doc(self.store, self.path + "/" + name)
    def add(self, data): self.store[self.path + "/auto" + str(len(self.store))] = dict(data)

class _FakeDb:
    def __init__(self): self.store = {}
    def collection(self, name): return _Col(self.store, name)


def test_firestore_event_records_trading_date_and_discord_result():
    sink = FirestoreSink("missing"); sink.db = _FakeDb()
    sink.event("cycle_slow", {"x": 1}, tdate=datetime(2026, 9, 4).date(), discord_eligible=True, event_id="e1")
    doc = sink.db.store["events/e1"]
    assert doc["date"] == "2026-09-04" and doc["discord_eligible"] is True and doc["posted_discord"] is None
    sink.mark_discord("e1", True)
    assert sink.db.store["events/e1"]["posted_discord"] is True


def test_firestore_series_continues_after_restart():
    from xau_mt5_bot.models import AnalysisSnapshot, Decision, EntryState, Freshness, ScoutSnapshot
    db = _FakeDb(); now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    def snap(ts):
        return AnalysisSnapshot(ts, "XAUUSD", SessionName.LONDON, 1, 2, 1, Freshness.LIVE, {}, [], [], [], [], None, 0,
                                ScoutSnapshot(SessionName.LONDON), EntryState.NOT_IN_SETUP, TriggerResult(False, "NONE"), None,
                                Decision(Action.WAIT, "wait", ts))
    first = FirestoreSink("missing", push_seconds=0, series_seconds=0); first.db = db
    for i in range(5): first.on_snapshot(snap(now + timedelta(seconds=i)), now.date(), force=True)
    second = FirestoreSink("missing", push_seconds=0, series_seconds=0); second.db = db        # restart: fresh in-memory state
    second.on_snapshot(snap(now + timedelta(seconds=60)), now.date(), force=True)
    series = db.store["sessions/2026-09-04_LONDON/heavy/series"]
    assert len(series["t"]) == 6                                                               # 5 old points kept + 1 new


# 17 --------------------------------------------------------------------------------------------------------------
def test_forced_push_flag_is_computed_before_run_cycle():
    src = (Path(__file__).resolve().parents[1] / "src" / "xau_mt5_bot" / "main.py").read_text()
    assert "first_cycle = engine.last_cycle is None" in src
    assert src.index("first_cycle = engine.last_cycle is None") < src.index("snapshot = engine.run_cycle()")
    assert "first_cycle or engine.session_boundary_event" in src


# 18 --------------------------------------------------------------------------------------------------------------
def test_discord_forwards_operational_events(monkeypatch):
    d = Discord("NOPE_ENV", 0, 0, event_level="all"); d.url = "https://example.invalid/hook"; sent = []   # v3.1.1: "all" keeps v3.0 forwarding
    monkeypatch.setattr(d, "send", lambda text="", embed=None: sent.append(embed or text) or True)
    for kind in ("cycle_slow", "pattern_scan_failed", "pattern_scan_executor", "pace_calibration_reset", "order_withheld", "state_file_recovered"):
        assert d.event(kind, {"reason": "x"}) is True
    assert len(sent) == 6
    assert d.event("clock_check_noise", {}) is False


# 19 --------------------------------------------------------------------------------------------------------------
def test_main_uses_two_delivery_queues_and_drains_on_shutdown():
    src = (Path(__file__).resolve().parents[1] / "src" / "xau_mt5_bot" / "main.py").read_text()
    assert 'Outbox("discord")' in src and 'Outbox("firestore")' in src
    assert "drained_discord = discord_box.drain(timeout=20.0)" in src and "drained_firestore = firestore_box.drain(timeout=20.0)" in src
    assert "drain(timeout=20.0) and firestore_box.drain" not in src
    assert "enqueue(" not in src
