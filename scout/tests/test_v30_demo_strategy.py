from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import xau_mt5_bot.mt5_client as adapter
from conftest import FakeClient, bars
from test_mt5_adapter_contract import MockMT5
from test_v18_features import _BarClient, _CycleLogger, _Logger
from test_v21_review_fixes import _load_outbox, _ns
from xau_mt5_bot.config import BotConfig, load_config, mt5_terminal_path
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.history import reset_history_cache
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import Action, LiquidityLevel, ScoutVerdict, SessionName, Side, StructureResult, StructureState, TriggerResult
from xau_mt5_bot.mtf import alignment, treatment
from xau_mt5_bot.outcomes import OutcomeStore, SetupTracker, classify_path, reliability, wilson
from xau_mt5_bot.position_manager import PositionManager
from xau_mt5_bot.scouts import ScoutManager
from xau_mt5_bot.session_target import TargetInputs, assess_session_target

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "xau_mt5_bot"


def _cfg(tmp_path: Path) -> BotConfig:
    cfg = load_config(ROOT / "config.yaml"); cfg.safety.allow_scout_orders = True
    cfg.project_dir = str(tmp_path)
    cfg.logging.sqlite_path = str(tmp_path / "logs" / "t.sqlite3"); cfg.logging.jsonl_path = str(tmp_path / "logs" / "a.jsonl")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return cfg


def _logger(cfg: BotConfig) -> AuditLogger:
    return AuditLogger(cfg.logging.sqlite_path, cfg.logging.jsonl_path)


# ======================================================================================= §4 MT5 attach without login
def test_mt5_initialize_success_validates_terminal_without_login(monkeypatch):
    fake = MockMT5(); calls = {}
    fake.initialize = lambda **kw: calls.setdefault("init", kw) or True
    fake.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    fake.last_error = lambda: (0, "ok")
    fake.symbol_info = lambda symbol: SimpleNamespace(visible=True, volume_min=0.01, volume_step=0.01, volume_max=100.0, trade_mode=4, point=.01, trade_stops_level=0, trade_freeze_level=0, filling_mode=2)
    fake.symbol_info_tick = lambda symbol: SimpleNamespace(time=int(datetime.now(UTC).timestamp()), bid=2500.0, ask=2500.2)
    monkeypatch.setattr(adapter, "mt5", fake)
    client = adapter.MT5Client(None, symbol="XAUUSD")
    result = client.initialize()
    assert calls["init"] == {} and result["is_demo"] and result["login"] == 1 and result["algo_trading"] and result["symbol"] == "XAUUSD"
    assert not hasattr(fake, "login") or "login" not in calls


def test_mt5_initialize_with_terminal_path(monkeypatch):
    fake = MockMT5(); calls = {}
    fake.initialize = lambda **kw: calls.setdefault("init", kw) or True
    fake.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    fake.symbol_info = lambda symbol: SimpleNamespace(visible=True, volume_min=0.01, volume_step=0.01, volume_max=100.0)
    fake.symbol_info_tick = lambda symbol: SimpleNamespace(time=int(datetime.now(UTC).timestamp()), bid=2500.0, ask=2500.2)
    monkeypatch.setattr(adapter, "mt5", fake)
    adapter.MT5Client(r"C:\\MT5\\terminal64.exe").initialize()
    assert calls["init"] == {"path": r"C:\\MT5\\terminal64.exe"}


def test_mt5_initialize_failure_reports_last_error(monkeypatch):
    fake = MockMT5(); fake.initialize = lambda **kw: False; fake.last_error = lambda: (-10005, "IPC timeout")
    monkeypatch.setattr(adapter, "mt5", fake)
    with pytest.raises(RuntimeError, match="IPC timeout"):
        adapter.MT5Client().initialize()


def test_live_account_is_rejected_at_attach(monkeypatch):
    fake = MockMT5(); fake.trade_mode = fake.ACCOUNT_TRADE_MODE_REAL; shut = []
    fake.initialize = lambda **kw: True; fake.shutdown = lambda: shut.append(1)
    fake.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    monkeypatch.setattr(adapter, "mt5", fake)
    with pytest.raises(RuntimeError, match="not a DEMO"):
        adapter.MT5Client().initialize()
    assert shut == [1]                                                                   # detached again, no loop, no order


def test_hedging_validation_is_captured_and_gates_scouts(monkeypatch, config):
    fake = MockMT5(); fake.initialize = lambda **kw: True
    fake.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    fake.symbol_info = lambda symbol: SimpleNamespace(visible=True, volume_min=0.01, volume_step=0.01, volume_max=100.0)
    fake.symbol_info_tick = lambda symbol: SimpleNamespace(time=int(datetime.now(UTC).timestamp()), bid=2500.0, ask=2500.2)
    fake.account_info = lambda: SimpleNamespace(login=1, server="Demo", balance=5e4, equity=5e4, margin_free=4.9e4, trade_allowed=True, trade_mode=0, margin_mode=0)
    monkeypatch.setattr(adapter, "mt5", fake)
    assert adapter.MT5Client().initialize()["is_hedging"] is False
    from xau_mt5_bot.execution import account_is_safe
    client = FakeClient(); client.hedging = False
    ok, why = account_is_safe(client, config, for_scouts=True)
    assert ok is False and "NETTING" in why


def test_production_code_never_logs_in_and_needs_no_credentials(monkeypatch):
    import ast
    for path in SRC.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "login", f"{path.name} calls .login()"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"), f"{path.name} references {node.value}"
    for var in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_TERMINAL_PATH"): monkeypatch.delenv(var, raising=False)
    assert mt5_terminal_path() is None
    env = (ROOT / ".env.example").read_text()
    assert "MT5_TERMINAL_PATH=" in env and "MT5_LOGIN" not in env and "MT5_PASSWORD" not in env


def test_reconnect_sequence_then_cold_start_no_trade(monkeypatch):
    fake = MockMT5(); events = []
    state = {"n": 0}
    def init(**kw):
        state["n"] += 1; events.append("init"); return state["n"] >= 2
    fake.initialize = init; fake.shutdown = lambda: events.append("shutdown"); fake.last_error = lambda: (1, "down")
    fake.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    fake.symbol_info = lambda symbol: SimpleNamespace(visible=True, volume_min=0.01, volume_step=0.01, volume_max=100.0)
    fake.symbol_info_tick = lambda symbol: SimpleNamespace(time=int(datetime.now(UTC).timestamp()), bid=2500.0, ask=2500.2)
    monkeypatch.setattr(adapter, "mt5", fake)
    slept = []
    adapter.MT5Client().reconnect(attempts=3, base_backoff_seconds=1.0, sleep=slept.append)
    assert events[:4] == ["shutdown", "init", "shutdown", "init"] and slept == [1.0, 2.0]
    # after reconnect the engine's next cycle is a startup cycle: adoption + NO_TRADE
    reset_history_cache(); client = _BarClient(); engine = TradingEngine(client, load_config(ROOT / "config.yaml"), _CycleLogger())
    engine.last_cycle = None; engine.startup_cycle = True
    snap = engine.run_cycle(client.tick.time); engine.shutdown()
    assert snap.decision.action not in {Action.LONG, Action.SHORT}


# ============================================================================= §5.1 pending report recovery via run_cycle
class _RunLogger(AuditLogger):
    def __init__(self, *a, **k): super().__init__(*a, **k); self.reports_written = []
    def report_once(self, report_type, report_key, payload):
        created = super().report_once(report_type, report_key, payload)
        if created: self.reports_written.append((report_type, report_key))
        return created


def test_pending_session_report_is_recovered_through_real_run_cycle_exactly_once(tmp_path: Path):
    cfg = _cfg(tmp_path); logger = _RunLogger(cfg.logging.sqlite_path, cfg.logging.jsonl_path)
    reset_history_cache(); client = _BarClient()
    e1 = TradingEngine(client, cfg, logger); e1.run_cycle(client.tick.time); e1.shutdown()            # bootstrap creates the state file
    end = datetime(2026, 9, 4, 16, tzinfo=UTC)
    e1._persist_pending_report(SessionName.LONDON, end, {"leader": "BUY"})                               # boundary cycle: scouts closed, crash before report
    assert e1.positions.meta["pending_reports"]
    reset_history_cache(); e2 = TradingEngine(_BarClient(), cfg, logger)
    e2.run_cycle(client.tick.time + timedelta(minutes=1)); e2.shutdown()                                 # REAL run_cycle: _handle_sessions runs before recovery
    written = [k for t, k in logger.reports_written if t == "session_summary" and "LONDON" in k]
    assert len(written) == 1
    assert e2.positions.meta.get("pending_reports") == {}                                                # cleared only after storage
    reset_history_cache(); e3 = TradingEngine(_BarClient(), cfg, logger)
    e3.run_cycle(client.tick.time + timedelta(minutes=2)); e3.shutdown()
    assert len([k for t, k in logger.reports_written if t == "session_summary" and "LONDON" in k]) == 1  # second restart: no duplicate
    with sqlite3.connect(cfg.logging.sqlite_path) as db:
        assert db.execute("SELECT COUNT(*) FROM generated_reports WHERE report_type='session_summary'").fetchone()[0] == 1


def test_pending_report_cleared_only_after_successful_storage(tmp_path: Path):
    cfg = _cfg(tmp_path)
    class Failing(_RunLogger):
        fail = False
        def report_once(self, *a, **k):
            if self.fail and a[0] == "session_summary": raise sqlite3.OperationalError("disk I/O error")
            return super().report_once(*a, **k)
    logger = Failing(cfg.logging.sqlite_path, cfg.logging.jsonl_path)
    reset_history_cache(); client = _BarClient()
    e1 = TradingEngine(client, cfg, logger); e1.run_cycle(client.tick.time); e1.shutdown()
    e1._persist_pending_report(SessionName.LONDON, datetime(2026, 9, 4, 16, tzinfo=UTC), {})
    logger.fail = True
    reset_history_cache(); e2 = TradingEngine(_BarClient(), cfg, logger)
    with pytest.raises(sqlite3.OperationalError):
        e2.run_cycle(client.tick.time + timedelta(minutes=1))
    e2.shutdown()
    assert e2.positions.meta["pending_reports"]                                                          # still pending → retried next start


# ================================================================================================ §5.2 scoped idempotency
def _closed_pair(logger, cfg, account, symbol_cfg=None, fp="fp"):
    client = FakeClient(); m = ScoutManager(client, symbol_cfg or cfg, logger.event, logger.event_once)
    m.account_key = account; m.fingerprint = fp; m.current_session = SessionName.LONDON; m.session_open_time = datetime(2026, 9, 4, 8, tzinfo=UTC)
    magic = m.magic(SessionName.LONDON); sym = (symbol_cfg or cfg).symbol
    client.send_market(sym, "BUY", 0.01, magic, "a"); client.send_market(sym, "SELL", 0.01, magic, "b")
    assert m.close_session(SessionName.LONDON).success
    return m


def test_scout_stat_idempotency_is_scoped(tmp_path: Path):
    cfg = _cfg(tmp_path); logger = _logger(cfg)
    _closed_pair(logger, cfg, "A"); _closed_pair(logger, cfg, "A")                       # same account/session replay → 1
    _closed_pair(logger, cfg, "B")                                                        # other account → +1
    _closed_pair(logger, cfg, "A", fp="fp2")                                              # other fingerprint → +1
    raw = cfg.model_dump(); raw["symbol"] = "XAGUSD"; other = BotConfig.model_validate(raw)
    m = _closed_pair(logger, cfg, "A", symbol_cfg=other)                                  # other symbol → +1
    with sqlite3.connect(cfg.logging.sqlite_path) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_type='scout_session_stats'").fetchone()[0] == 4
    assert logger.scoped_event_count("scout_session_stats", "A", "XAUUSD", "fp") == 1
    assert logger.scoped_event_count("scout_session_stats", "B", "XAUUSD", "fp") == 1
    assert logger.scoped_event_count("scout_session_stats", "A", "XAUUSD", "fp2") == 1
    assert logger.scoped_event_count("scout_session_stats", "A", "XAGUSD", "fp") == 1


# ================================================================================================= §5.3 scoped backfill
def test_backfill_groups_by_scope_and_keeps_other_scopes(tmp_path: Path):
    path = tmp_path / "v22.sqlite3"
    def row(account, fp, ts):
        return (ts, "scout_session_stats", json.dumps({"session": "ASIA", "session_id": "ASIA@2026-09-01T00:00:00+00:00", "account": account, "symbol": "XAUUSD",
                                                       "config_fingerprint": fp, "leader": "BUY", "open_time": "2026-09-01T00:00:00+00:00", "close_time": "2026-09-01T08:00:00+00:00",
                                                       "buy_mfe": 1, "buy_mae": 0, "sell_mfe": 0, "sell_mae": 1}))
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL)")
        db.executemany("INSERT INTO events(timestamp,event_type,payload_json) VALUES(?,?,?)",
                       [row("A", "fpA", "2026-09-01T08:00:00+00:00"), row("A", "fpA", "2026-09-01T08:00:05+00:00"),   # exact duplicate
                        row("B", "fpA", "2026-09-01T08:00:00+00:00"), row("A", "fpB", "2026-09-01T08:00:00+00:00")])
    logger = AuditLogger(str(path), str(tmp_path / "a.jsonl"))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_type='scout_session_stats'").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM event_keys").fetchone()[0] == 3
    for account, fp in (("A", "fpA"), ("B", "fpA"), ("A", "fpB")):
        assert logger.scoped_event_count("scout_session_stats", account, "XAUUSD", fp) == 1
    start, end = datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
    assert logger.scout_performance_summary(start, end, 1.0, account="A", config_fingerprint="fpA", symbol="XAUUSD")["sessions"]["ASIA"]["sessions"] == 1
    assert logger.scout_performance_summary(start, end, 1.0, account="B", config_fingerprint="fpA", symbol="XAUUSD")["sessions"]["ASIA"]["sessions"] == 1


# ================================================================================================= §5.4 trade migration
V21_ROWS = "INSERT INTO trades(ticket,kind,side,session,pnl,account,symbol,result_confirmed,close_time) VALUES (1,'PA','LONG','LONDON',2.0,NULL,NULL,1,'2026-09-01T09:00:00+00:00'),(2,'PA','SHORT','ASIA',-1.0,'A','XAUUSD',1,'2026-09-01T09:00:00+00:00')"


def _v23_db(path: Path):
    with sqlite3.connect(path) as db:
        db.executescript(f"""
            CREATE TABLE trades (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER NOT NULL, kind TEXT NOT NULL, side TEXT, session TEXT, magic INTEGER, volume REAL,
                open_time TEXT, open_price REAL, close_time TEXT, close_price REAL, sl REAL, tp REAL, pnl REAL, mfe REAL, mae REAL, duration_s INTEGER, exit_reason TEXT,
                account TEXT, symbol TEXT, setup_id TEXT, result_confirmed INTEGER, requested_entry REAL, actual_entry REAL, initial_sl REAL, final_sl REAL,
                tp1 REAL, tp2 REAL, tp3 REAL, partial_exits_json TEXT, total_realized_pnl REAL, commission REAL, swap REAL, config_fingerprint TEXT, payload_json TEXT,
                UNIQUE(account, symbol, ticket));
            {V21_ROWS};""")


def _assert_current(path: Path, rows: int):
    with sqlite3.connect(path) as db:
        assert AuditLogger._trades_schema_is_current(db)
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == rows
        assert db.execute("SELECT account,symbol FROM trades WHERE ticket=1").fetchone() == ("unknown", "unknown")
        assert db.execute("SELECT name FROM sqlite_master WHERE name='trades_v3'").fetchone() is None


def test_authentic_v23_schema_with_nullable_identity_is_migrated(tmp_path: Path):
    path = tmp_path / "v23.sqlite3"; _v23_db(path)
    AuditLogger(str(path), str(tmp_path / "a.jsonl")); _assert_current(path, 2)
    AuditLogger(str(path), str(tmp_path / "a.jsonl")); _assert_current(path, 2)          # second startup: no-op


@pytest.mark.parametrize("fail_after", ["create", "copy", "drop"])
def test_migration_rolls_back_at_every_stage(tmp_path: Path, monkeypatch, fail_after):
    path = tmp_path / "v23.sqlite3"; _v23_db(path)
    class Fragile(sqlite3.Connection):
        def execute(self, sql, *a, **k):
            result = super().execute(sql, *a, **k)
            s = sql.strip().upper()
            if fail_after == "create" and s.startswith("CREATE TABLE TRADES_V3"): raise sqlite3.OperationalError("disk I/O error")
            if fail_after == "copy" and s.startswith("INSERT OR IGNORE INTO TRADES_V3"): raise sqlite3.OperationalError("disk I/O error")
            if fail_after == "drop" and s == "DROP TABLE TRADES": raise sqlite3.OperationalError("disk I/O error")
            return result
    def fragile_connect(self):
        c = sqlite3.connect(self.sqlite_path, factory=Fragile); c.execute("PRAGMA journal_mode=WAL"); return c
    monkeypatch.setattr(AuditLogger, "_connect", fragile_connect)
    with pytest.raises(sqlite3.OperationalError):
        AuditLogger(str(path), str(tmp_path / "a.jsonl"))
    monkeypatch.undo()
    with sqlite3.connect(path) as db:                                                    # original intact after rollback
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
        assert not AuditLogger._trades_schema_is_current(db)
    AuditLogger(str(path), str(tmp_path / "a.jsonl")); _assert_current(path, 2)          # restart completes it


# ================================================================================================= §5.5 cleanup persisted
def test_discarded_pending_reports_do_not_reappear(tmp_path: Path):
    cfg = _cfg(tmp_path); client = FakeClient()
    e = TradingEngine(client, cfg, _Logger()); e.positions = PositionManager(client, cfg, state_dir=str(tmp_path), account_key="t"); e.account_key = "t"
    e.positions.meta["pending_reports"] = {"x:LONDON@bad": {"session": "LONDON", "end": "not-a-time", "fingerprint": e.strategy_fingerprint},
                                            "y": {"session": "NOPE", "end": "2026-09-04T16:00:00+00:00", "fingerprint": e.strategy_fingerprint},
                                            "z": "garbage", "w": {"session": "ASIA", "end": "2026-09-04T16:00:00+00:00", "fingerprint": "other"}}
    e.positions._save()
    assert e._restore_pending_reports() == 0
    reloaded = PositionManager(client, cfg, state_dir=str(tmp_path), account_key="t")
    assert reloaded.meta.get("pending_reports") == {}


# ================================================================================================= §5.6 event routing
def test_recovery_events_reach_sqlite_firestore_and_console(tmp_path: Path, capsys):
    cfg = _cfg(tmp_path); logger = _logger(cfg)
    from xau_mt5_bot.notify import Discord
    block = _load_outbox(); fs = []
    class _Sink:
        def event(self, *a): fs.append(a[0])
        def mark_discord(self, *a): pass
    d = Discord("NOPE", 0, 0, event_level="all"); d.url = None
    ns = _ns(d, _Sink()); ns["logger"] = logger; exec(block, ns)
    for kind in ("session_summary_recovered", "scout_pending_stats_dropped", "shutdown_discord_pending"):
        ns["fanout"](kind, {"count": 1})
    ns["discord_box"].drain(5); ns["firestore_box"].drain(5)
    with sqlite3.connect(cfg.logging.sqlite_path) as db:
        kinds = {r[0] for r in db.execute("SELECT event_type FROM events")}
    assert {"session_summary_recovered", "scout_pending_stats_dropped", "shutdown_discord_pending"} <= kinds      # SQLite
    assert set(fs) >= {"session_summary_recovered", "scout_pending_stats_dropped", "shutdown_discord_pending"}     # Firestore
    assert Discord.is_eligible(d, "session_summary_recovered") and Discord.is_eligible(d, "scout_pending_stats_dropped")
    src = (SRC / "main.py").read_text()
    assert 'print(f"INTEGRATION WARNING — queues did not drain' in src                                             # console


# ============================================================================================= §6 demo-signal semantics
def test_final_go_is_demo_signal_not_funded(tmp_path: Path):
    cfg = _cfg(tmp_path); reset_history_cache(); client = _BarClient(); engine = TradingEngine(client, cfg, _CycleLogger())
    snap = engine.run_cycle(client.tick.time); engine.shutdown()
    ds = snap.analysis["decision_summary"]
    assert set(ds) >= {"signal_go", "trade_action", "scout_verdict", "target_verdict", "calibration_status", "reason"}
    assert ds["signal_go"] == ("GO" if snap.decision.action in {Action.LONG, Action.SHORT} else "NO-GO")
    assert ds["calibration_status"] in {"CALIBRATED", "COLLECTING"}
    flat = json.dumps(snap.reporting) + json.dumps(snap.analysis)
    assert "funded" not in flat.lower() and "manual_plan_risk" not in flat


# ============================================================================================= §7 session target
def _levels(*items):
    return [LiquidityLevel(price, kind, "D1", datetime(2026, 9, 1, tzinfo=UTC), strength) for price, kind, strength in items]


def _inputs(side=Side.LONG, entry=2500.0, remaining=300.0, pace=0.08, srange=6.0, samples=30, hit=0.7, **over):
    base = dict(side=side, reference_entry=entry, bid=entry - 0.1, ask=entry + 0.1, spread=0.2, atr=1.2, remaining_minutes=remaining,
                session_minutes_total=480.0, session_high=entry + srange / 2, session_low=entry - srange / 2, pace_range_per_min=pace,
                velocity_direction="UP" if side == Side.LONG else "DOWN", market_speed="NORMAL", liquidity=[], alignment_label="FULL_ALIGNMENT",
                alignment_score=10, scout_leader="BUY", scout_strength=6, scout_verdict="CONFIRMS", historical_samples=samples,
                historical_plus_target_rate=hit, historical_fakeout_rate=0.2, session_range_p50=14.0, session_range_p75=20.0, session_range_p90=28.0)
    base.update(over); return TargetInputs(**base)


def test_target_achievable_case(config):
    r = assess_session_target(_inputs(), config.session_target)
    assert r["target_verdict"] == "ACHIEVABLE" and r["target_price"] == 2510.0 and r["expected_direction"] == "UP"


def test_target_stretched_case(config):
    r = assess_session_target(_inputs(pace=0.03, alignment_label="LOWER_TF_COUNTERTREND", scout_verdict="NEUTRAL", hit=0.4, srange=14.0,
                                      liquidity=_levels((2506.0, "PDH", 2.0))), config.session_target)
    assert r["target_verdict"] == "STRETCHED" and r["nearest_blocking_liquidity"]["kind"] == "PDH" and r["distance_to_blocking_liquidity"] == pytest.approx(6.1, abs=0.05)


def test_target_unlikely_when_time_or_range_is_gone(config):
    assert assess_session_target(_inputs(remaining=20.0), config.session_target)["target_verdict"] == "UNLIKELY"
    assert assess_session_target(_inputs(srange=27.0), config.session_target)["target_verdict"] == "UNLIKELY"


def test_target_insufficient_history_keeps_structural_read(config):
    r = assess_session_target(_inputs(samples=3, hit=None), config.session_target)
    assert r["target_verdict"] == "INSUFFICIENT_HISTORY" and r["structural_verdict"] == "ACHIEVABLE" and r["historical_samples"] == 3


def test_target_long_short_symmetry(config):
    long = assess_session_target(_inputs(Side.LONG), config.session_target)
    short = assess_session_target(_inputs(Side.SHORT, scout_leader="SELL"), config.session_target)
    assert long["target_price"] == 2510.0 and short["target_price"] == 2490.0
    assert long["required_price_move"] == pytest.approx(short["required_price_move"], abs=0.01)
    assert long["target_verdict"] == short["target_verdict"] == "ACHIEVABLE"


def test_opposing_liquidity_blocking_is_direction_aware():
    from xau_mt5_bot.session_target import _nearest_blocker
    levels = _levels((2504.0, "PDH", 4.0), (2496.0, "PDL", 4.0), (2507.0, "PWL", 3.0))
    blocker, dist = _nearest_blocker(levels, 2500.0, 2510.0, True)
    assert blocker.kind == "PDH" and dist == 4.0                                          # PWL above price is not a long blocker
    blocker, dist = _nearest_blocker(levels, 2500.0, 2490.0, False)
    assert blocker.kind == "PDL" and dist == 4.0


def test_session_time_remaining_uses_next_boundary(config):
    engine = TradingEngine(FakeClient(), config, _Logger())
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)                                          # inside London (Sep 4 2026, BST)
    end = engine._session_end(now)
    assert end is not None and end > now and (end - now).total_seconds() / 60 <= 12 * 60
    assert engine._session_end(datetime(2026, 9, 5, 12, tzinfo=UTC)) is None              # Saturday: CLOSED


# ================================================================================================= §8–10 outcomes
def _features(setup_id, ts, direction="LONG", entry=2500.0, inval=2495.0, session="LONDON", fam="BOS", **over):
    f = {"setup_id": setup_id, "account": "t", "symbol": "XAUUSD", "config_fingerprint": "fp", "timestamp": ts.isoformat(), "trading_date": ts.date().isoformat(),
         "session": session, "direction": direction, "pattern_names": ["Bullish BOS"], "pattern_family": fam, "trigger_source": "M1", "trigger_bar_time": ts.isoformat(),
         "entry_price": entry, "invalidation_price": inval, "d1_structure": "Bullish", "h4_structure": "Bullish", "h1_structure": "Bullish", "m15_structure": "Bullish",
         "m5_structure": "Bullish", "alignment": "FULL_ALIGNMENT", "treatment": "TREND_CONTINUATION", "liquidity_context": [], "sweep_context": "NONE",
         "zone_type": "OB", "confluence": 70, "market_speed": "NORMAL", "atr": 1.2, "atr_bucket": "LOW", "scout_leader": "BUY", "scout_strength": 6,
         "scout_verdict": "CONFIRMS", "spread": 0.2, "session_minutes_remaining": 300.0, "remaining_bucket": "EARLY"}
    f.update(over); return f


def _path(ts, closes):
    """M1 bars after ts following `closes` (each bar high/low ±0.3 around close)."""
    return bars((ts + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M"), [(c, c + 0.3, c - 0.3, c) for c in closes])


def test_tracker_resolves_continuation_and_reversal(tmp_path: Path, config):
    cfg = _cfg(tmp_path); logger = _logger(cfg); store = logger.outcome_store(); meta = {}
    tracker = SetupTracker(store, meta, lambda: None, cfg.outcomes)
    ts = datetime(2026, 9, 4, 9, tzinfo=UTC)
    assert tracker.register(_features("s1", ts)) and not tracker.register(_features("s1", ts))
    tracker.register(_features("s2", ts, direction="SHORT", entry=2500.0, inval=2505.0, scout_leader="SELL"))
    path = _path(ts, [2501, 2503, 2506, 2508, 2511])                                        # +11 for the long, invalidates the short
    resolved = {r["setup_id"]: r for r in tracker.update(path, ts + timedelta(minutes=6))}
    assert resolved["s1"]["continuation_classification"] == "CONTINUATION" and resolved["s1"]["reached_plus_10"] == 1 and resolved["s1"]["time_to_plus_5"] is not None
    assert resolved["s2"]["continuation_classification"] == "REVERSAL" and resolved["s2"]["invalidation_hit"] == 1 and resolved["s2"]["fakeout_classification"] == "REVERSAL"
    assert resolved["s1"]["scout_agreed_with_outcome"] == 1 and resolved["s2"]["scout_agreed_with_outcome"] == 0
    assert meta["open_setups"] == {}


def test_fakeout_classifications_are_structural():
    reached = {3.0: False, 5.0: False, 10.0: False}
    assert classify_path("LONG", "PDL:SHORT", "M1", 1.0, 6.0, reached, True, True, 5.0, 3.0)[0] == "SWEEP_RECLAIM"
    assert classify_path("LONG", "NONE", "M5", 1.5, 6.0, reached, True, False, 5.0, 3.0)[0] == "FAILED_BREAKOUT"
    assert classify_path("LONG", "NONE", "M1", 1.5, 6.0, reached, True, False, 5.0, 3.0, "RANGE_REVERSION")[0] == "RANGE_REVERSION"
    hit = {3.0: True, 5.0: True, 10.0: False}
    assert classify_path("LONG", "NONE", "M1", 6.0, 3.5, hit, False, False, 5.0, 3.0) == ("STOP_HUNT_THEN_CONTINUATION", "CONTINUATION")
    assert classify_path("LONG", "NONE", "M1", 6.0, 1.0, hit, False, False, 5.0, 3.0) == ("CONTINUATION", "CONTINUATION")
    assert classify_path("LONG", "NONE", "M1", 2.0, 2.0, reached, False, False, 5.0, 3.0) == ("INCONCLUSIVE", "INCONCLUSIVE")
    # a losing trade that never lost structure is NOT a fakeout
    assert classify_path("LONG", "NONE", "M1", 2.0, 2.5, reached, False, False, 5.0, 3.0)[0] == "INCONCLUSIVE"


def test_reliability_has_no_look_ahead_and_falls_back(tmp_path: Path):
    cfg = _cfg(tmp_path); store = _logger(cfg).outcome_store(); meta = {}
    tracker = SetupTracker(store, meta, lambda: None, cfg.outcomes)
    t0 = datetime(2026, 9, 1, 9, tzinfo=UTC)
    for i in range(8):
        ts = t0 + timedelta(hours=i)
        tracker.register(_features(f"a{i}", ts))
        tracker.update(_path(ts, [2503, 2506, 2511] if i % 2 == 0 else [2499, 2494]), ts + timedelta(minutes=4))
    future = t0 + timedelta(hours=20); tracker.register(_features("future", future))
    tracker.update(_path(future, [2503, 2506, 2511]), future + timedelta(minutes=4))
    before = store.resolved_before("t", "XAUUSD", None, t0 + timedelta(hours=6, minutes=30))
    assert {r["setup_id"] for r in before} == {f"a{i}" for i in range(7)}                   # a7 (decided later) and 'future' excluded
    current = {"session": "LONDON", "direction": "LONG", "pattern_family": "BOS", "alignment": "FULL_ALIGNMENT", "market_speed": "NORMAL",
               "sweep_context": "NONE", "atr_bucket": "LOW", "remaining_bucket": "EARLY"}
    rel = reliability(before, current, minimum_samples=20, min_level_samples=5)
    assert rel["comparison_level"] == "EXACT" and rel["samples"] == 7 and rel["status"] == "INSUFFICIENT_SAMPLE"
    assert rel["plus_10_hit_rate"] == pytest.approx(4 / 7, abs=0.01) and rel["confidence_interval_plus_10"][0] < rel["plus_10_hit_rate"] < rel["confidence_interval_plus_10"][1]
    rel2 = reliability(before, {**current, "pattern_family": "CHART", "direction": "SHORT"}, 20, 5)
    assert rel2["comparison_level"] == "SAME_SESSION_REGIME" and rel2["samples"] == 7
    assert wilson(0, 0) is None and wilson(5, 10)[0] < 0.5 < wilson(5, 10)[1]


def test_tracker_state_survives_restart(tmp_path: Path):
    cfg = _cfg(tmp_path); store = _logger(cfg).outcome_store()
    client = FakeClient(); pm = PositionManager(client, cfg, state_dir=str(tmp_path), account_key="t")
    tracker = SetupTracker(store, pm.meta, pm._save, cfg.outcomes)
    ts = datetime(2026, 9, 4, 9, tzinfo=UTC); tracker.register(_features("s1", ts))
    tracker.update(_path(ts, [2501, 2502]), ts + timedelta(minutes=3))
    pm2 = PositionManager(client, cfg, state_dir=str(tmp_path), account_key="t")
    tracker2 = SetupTracker(store, pm2.meta, pm2._save, cfg.outcomes)
    assert "s1" in tracker2.meta["open_setups"] and tracker2.meta["open_setups"]["s1"]["mfe"] == pytest.approx(2.3)
    resolved = tracker2.update(_path(ts + timedelta(minutes=2), [2506, 2511]), ts + timedelta(minutes=5))
    assert resolved and resolved[0]["reached_plus_10"] == 1


# ================================================================================================= §11 multi-timeframe
def _structs(**states):
    return {tf: StructureResult(tf, StructureState(states.get(tf, "Neutral")), [], []) for tf in ("D1", "H4", "H1", "M15", "M5")}


def test_multi_timeframe_alignment_labels():
    b, s, n = "Bullish", "Bearish", "Neutral"
    assert alignment(_structs(D1=b, H4=b, H1=b, M15=b, M5=b), Side.LONG)["label"] == "FULL_ALIGNMENT"
    assert alignment(_structs(D1=b, H4=b, H1=n, M15=b, M5=b), Side.LONG)["label"] == "PARTIAL_ALIGNMENT"
    assert alignment(_structs(D1=b, H4=b, H1=b, M15=s, M5=s), Side.LONG)["label"] == "LOWER_TF_COUNTERTREND"
    assert alignment(_structs(D1=s, H4=s, H1=b, M15=b, M5=b), Side.LONG)["label"] == "HIGHER_TF_CONFLICT"
    assert alignment(_structs(D1="Range", H4="Range", H1=n, M15="Range", M5=b), Side.LONG)["label"] == "RANGE_CONDITION"
    assert alignment(_structs(D1=b, H4=b, H1=b, M15=b, M5=b), None)["label"] == "RANGE_CONDITION"


def test_treatment_names_the_setup():
    st = _structs(D1="Bullish", H4="Bullish", H1="Bullish", M15="Bullish", M5="Bullish")
    trig = TriggerResult(True, "M1")
    assert treatment(Side.LONG, "FULL_ALIGNMENT", [], [], trig, st, [])["treatment"] == "TREND_CONTINUATION"
    assert treatment(Side.LONG, "HIGHER_TF_CONFLICT", [], [], trig, st, [])["treatment"] == "COUNTERTREND_REVERSAL"
    assert treatment(None, "RANGE_CONDITION", [], [], trig, st, [])["treatment"] == "NO_VALID_STRUCTURE"
    sweep = SimpleNamespace(active=True, direction="SHORT", level_type="PDL")
    assert treatment(Side.LONG, "PARTIAL_ALIGNMENT", [sweep], [], trig, st, [])["treatment"] == "LIQUIDITY_SWEEP_REVERSAL"
    assert treatment(Side.LONG, "PARTIAL_ALIGNMENT", [], [], trig, st, [{"name": "Failed Bearish BOS"}])["treatment"] == "FAILED_BREAKOUT"


# ============================================================================================= §12 locks / repair gate
def test_daily_loss_and_consecutive_loss_locks(config, tmp_path: Path):
    config.risk.daily_max_loss_percent = 2.0; config.risk.max_consecutive_losses = 3
    pm = PositionManager(FakeClient(), config, state_dir=str(tmp_path), account_key="t")
    d = pm._day("2026-09-04"); d["pnl"] = -1100.0
    ok, why = pm.risk_allowed("2026-09-04", 50000); assert ok is False and "daily max loss" in why       # -2.2% of 50k
    pm2 = PositionManager(FakeClient(), config, state_dir=str(tmp_path / "b"), account_key="t")
    d2 = pm2._day("2026-09-04"); d2["consecutive_losses"] = 3
    ok, why = pm2.risk_allowed("2026-09-04", 50000); assert ok is False and "consecutive" in why
    pm3 = PositionManager(FakeClient(), config, state_dir=str(tmp_path / "c"), account_key="t")
    d3 = pm3._day("2026-09-04"); d3["consecutive_losses"] = 2; d3["pnl"] = -900.0
    assert pm3.risk_allowed("2026-09-04", 50000)[0] is True


def test_scout_repair_is_blocked_by_the_risk_gate(config):
    client = FakeClient(); m = ScoutManager(client, config); m.current_session = SessionName.LONDON
    magic = m.magic(SessionName.LONDON); client.send_market("XAUUSD", "BUY", 0.01, magic, "a")
    m.orders_ok = lambda: (False, "daily max loss reached")
    sends_before = len(client.sent)
    m.repair_leg(datetime(2026, 9, 4, 9, tzinfo=UTC))
    assert not any(s["magic"] == magic and s["side"] == "SELL" for s in client.sent[sends_before:])   # no new leg while the lock holds
    assert len(client.positions("XAUUSD", magic)) <= 1


# ============================================================================================= §16/§17 cycle timing
def test_warm_cycle_stays_within_poll_seconds(tmp_path: Path):
    cfg = _cfg(tmp_path); reset_history_cache(); client = _BarClient(); engine = TradingEngine(client, cfg, _CycleLogger())
    engine.run_cycle(client.tick.time)
    timings = []
    for i in range(1, 4):
        t0 = time.perf_counter(); engine.run_cycle(client.tick.time + timedelta(seconds=3 * i)); timings.append(time.perf_counter() - t0)
    engine.shutdown()
    assert max(timings) < cfg.poll_seconds, timings
