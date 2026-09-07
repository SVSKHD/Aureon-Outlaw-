from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import FakeClient
from test_v18_features import _Logger
from xau_mt5_bot.config import BotConfig
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.fingerprint import strategy_fingerprint
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import SessionName
from xau_mt5_bot.position_manager import PositionManager
from xau_mt5_bot.scouts import ScoutManager

ROOT = Path(__file__).resolve().parents[1]
BASE = {"kind": "PA", "result_confirmed": 1, "side": "LONG", "session": "LONDON", "symbol": "XAUUSD", "config_fingerprint": "fp",
        "open_time": "2026-09-01T08:00:00+00:00", "close_time": "2026-09-01T09:00:00+00:00", "pnl": 1.0}


# 1 -----------------------------------------------------------------------------------------------------------------
def test_same_ticket_on_two_accounts_keeps_both_trades(tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    logger.trade({**BASE, "ticket": 77, "account": "A"})
    logger.trade({**BASE, "ticket": 77, "account": "B"})
    logger.trade({**BASE, "ticket": 77, "account": "A", "pnl": 5.0})                    # same identity → update, not duplicate
    assert logger.confirmed_trade_count("fp", account="A", symbol="XAUUSD") == 1
    assert logger.confirmed_trade_count("fp", account="B", symbol="XAUUSD") == 1
    with sqlite3.connect(tmp_path / "t.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
        assert db.execute("SELECT pnl FROM trades WHERE account='A'").fetchone()[0] == 5.0


def test_legacy_ticket_keyed_database_is_migrated_with_rows_kept(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE trades (ticket INTEGER PRIMARY KEY, kind TEXT NOT NULL, side TEXT, session TEXT, magic INTEGER, volume REAL,
                open_time TEXT, open_price REAL, close_time TEXT, close_price REAL, sl REAL, tp REAL, pnl REAL, mfe REAL, mae REAL,
                duration_s INTEGER, exit_reason TEXT, account TEXT, symbol TEXT, setup_id TEXT, result_confirmed INTEGER, payload_json TEXT);
            INSERT INTO trades(ticket,kind,side,session,pnl,account,symbol,result_confirmed,close_time) VALUES
                (1,'PA','LONG','LONDON',2.0,'A','XAUUSD',1,'2026-09-01T09:00:00+00:00'),
                (2,'SCOUT','SHORT','ASIA',-1.0,'A','XAUUSD',1,'2026-09-01T09:00:00+00:00');
        """)
    logger = AuditLogger(str(path), str(tmp_path / "a.jsonl"))
    with sqlite3.connect(path) as db:
        pk = [row[1] for row in db.execute("PRAGMA table_info(trades)") if row[5]]
        assert pk == ["id"]
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
        assert db.execute("SELECT config_fingerprint FROM trades WHERE ticket=1").fetchone() == (None,)
    logger.trade({**BASE, "ticket": 1, "account": "B"})                                   # same ticket, other account → new row
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 3


# 2 -----------------------------------------------------------------------------------------------------------------
def test_go_tally_is_fingerprint_scoped(config, tmp_path: Path):
    client = FakeClient(); t = datetime(2026, 9, 7, 9, tzinfo=UTC)
    old = TradingEngine(client, config, _Logger()); old.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    old._record_session_go(SessionName.LONDON, "GO", t)
    raw = config.model_dump(); raw["analysis"]["min_confluence"] = config.analysis.min_confluence + 1
    new_cfg = BotConfig.model_validate(raw); assert strategy_fingerprint(new_cfg) != old.strategy_fingerprint
    new = TradingEngine(client, new_cfg, _Logger()); new.positions = PositionManager(client, new_cfg, state_dir=str(tmp_path), account_key="t")
    new._record_session_go(SessionName.LONDON, "NO-GO", t + timedelta(minutes=5))
    report = new._session_go_report(SessionName.LONDON, t + timedelta(hours=3))
    assert report["go"] == "NO-GO" and report["session_instance"].startswith(new.strategy_fingerprint + ":")


# 3 -----------------------------------------------------------------------------------------------------------------
def test_fingerprint_changes_with_go_affecting_settings_only(config):
    base = strategy_fingerprint(config)
    raw = config.model_dump(); raw["session_target"]["target_price_move"] = 8.0
    assert strategy_fingerprint(BotConfig.model_validate(raw)) != base                    # feasibility gate changes GO
    raw = config.model_dump(); raw["reporting"]["research_daily_usd"] = 250.0
    assert strategy_fingerprint(BotConfig.model_validate(raw)) == base                    # research display scale does not
    raw = config.model_dump(); raw["reporting"]["discord_scout_pair_min_interval_seconds"] = 120
    assert strategy_fingerprint(BotConfig.model_validate(raw)) == base


# 4 -----------------------------------------------------------------------------------------------------------------
class _FailingOnce(AuditLogger):
    def __init__(self, *a, **k): super().__init__(*a, **k); self.fail_next = False
    def event_once(self, event_type, key, payload):
        if self.fail_next: self.fail_next = False; raise sqlite3.OperationalError("disk I/O error")
        return super().event_once(event_type, key, payload)


def _pair(client, manager, session=SessionName.LONDON):
    magic = manager.magic(session)
    client.send_market("XAUUSD", "BUY", 0.01, magic, "a"); client.send_market("XAUUSD", "SELL", 0.01, magic, "b")
    manager.current_session = session; manager.session_open_time = datetime(2026, 9, 4, 8, tzinfo=UTC)
    return magic


def test_sqlite_failure_keeps_pending_stats_and_calibration_unchanged(config, tmp_path: Path):
    logger = _FailingOnce(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    client = FakeClient(); m = ScoutManager(client, config, logger.event, logger.event_once)
    m.state_path = str(tmp_path / "s.json"); m.account_key = "A"; m.fingerprint = "fp"
    _pair(client, m)
    logger.fail_next = True
    with pytest.raises(sqlite3.OperationalError):
        m.close_session(SessionName.LONDON)
    assert m.calibration_sessions == 0 and m._pending_stats is not None
    fresh = ScoutManager(client, config, logger.event, logger.event_once); fresh.state_path = m.state_path; fresh.account_key = "A"; fresh.fingerprint = "fp"
    fresh.restore()
    assert fresh._pending_stats is not None and fresh.calibration_sessions == 0
    assert fresh.close_session(SessionName.LONDON).success is True                        # retry (positions already gone)
    assert fresh.calibration_sessions == 1
    assert logger.scoped_event_count("scout_session_stats", "A", "XAUUSD", "fp") == 1


def test_crash_after_stats_event_before_persist_does_not_duplicate(config, tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    client = FakeClient(); m = ScoutManager(client, config, logger.event, logger.event_once)
    m.state_path = str(tmp_path / "s.json"); m.account_key = "A"; m.fingerprint = "fp"
    _pair(client, m)
    original_persist = m._persist
    def crash_once():
        if m.calibration_sessions == 1 and m._pending_stats is None: raise RuntimeError("power loss")   # the persist AFTER the event
        original_persist()
    m._persist = crash_once
    with pytest.raises(RuntimeError):
        m.close_session(SessionName.LONDON)
    fresh = ScoutManager(client, config, logger.event, logger.event_once); fresh.state_path = m.state_path; fresh.account_key = "A"; fresh.fingerprint = "fp"
    fresh.restore()
    assert fresh._pending_stats is not None and fresh.calibration_sessions == 0            # state from before the crash
    assert fresh.close_session(SessionName.LONDON).success is True
    assert fresh.calibration_sessions == 1
    assert logger.scoped_event_count("scout_session_stats", "A", "XAUUSD", "fp") == 1     # idempotent: still one event
    with sqlite3.connect(tmp_path / "t.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_type='scout_session_stats'").fetchone()[0] == 1


# additional -------------------------------------------------------------------------------------------------------
def test_go_tally_survives_a_failed_report_write(config, tmp_path: Path):
    client = FakeClient(); engine = TradingEngine(client, config, _Logger())
    engine.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    t = datetime(2026, 9, 4, 9, tzinfo=UTC)
    engine._record_session_go(SessionName.LONDON, "GO", t)
    report = engine._session_go_report(SessionName.LONDON, t + timedelta(hours=3))
    # report write "fails" → nothing consumed
    again = engine._session_go_report(SessionName.LONDON, t + timedelta(hours=3))
    assert again["go"] == "GO" and again == report
    engine._consume_session_go(report)
    assert engine._session_go_report(SessionName.LONDON, t + timedelta(hours=3))["go"] == "NO-GO"
    src = (ROOT / "src" / "xau_mt5_bot" / "engine.py").read_text()
    assert src.index('once("session_summary"') < src.index("self._consume_session_go(go_report)")


def test_live_samples_require_exact_account():
    src = (ROOT / "src" / "xau_mt5_bot" / "engine.py").read_text()
    assert src.count('closed_trade.get("account") == self.account_key') == 3
    assert 'closed_trade.get("account") in (None' not in src


def test_go_prune_is_chronological_and_every_cycle_is_persisted(config, tmp_path: Path):
    client = FakeClient(); engine = TradingEngine(client, config, _Logger())
    engine.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    days = [datetime(2026, 8, 3, tzinfo=UTC) + timedelta(days=i) for i in range(20) if (datetime(2026, 8, 3, tzinfo=UTC) + timedelta(days=i)).weekday() < 5]
    for d in days[:13]:
        engine._record_session_go(SessionName.ASIA, "NO-GO", d + timedelta(hours=1))
        engine._record_session_go(SessionName.NEW_YORK, "NO-GO", d + timedelta(hours=14))
    keys = sorted(engine._go_store(), key=TradingEngine._key_time)
    assert len(keys) == 12 and TradingEngine._key_time(keys[0]) >= days[7].isoformat()[:10]   # oldest kept is recent, not alphabetical
    reloaded = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    assert reloaded.meta["session_go"][keys[-1]]["cycles"] == 1                                 # NO-GO cycle persisted immediately


def test_recovery_warnings_are_discord_eligible():
    from xau_mt5_bot.notify import Discord
    assert {"scout_pending_stats_dropped", "shutdown_discord_pending"} <= set(Discord.ELIGIBLE_EVENTS)
