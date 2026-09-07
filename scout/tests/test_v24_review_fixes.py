from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[1]


def _engine(client, config, tmp_path, logger=None):
    engine = TradingEngine(client, config, logger or _Logger())
    engine.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    engine.account_key = "t"
    return engine


# 1 -----------------------------------------------------------------------------------------------------------------
def test_friday_close_go_is_fingerprint_scoped(config, tmp_path: Path):
    client = FakeClient(); close = datetime(2026, 9, 4, 21, tzinfo=UTC)
    old = _engine(client, config, tmp_path); old._remember_friday_close_go(close, "GO")
    raw = config.model_dump(); raw["session_target"]["target_price_move"] = 8.0
    new_cfg = BotConfig.model_validate(raw); assert strategy_fingerprint(new_cfg) != old.strategy_fingerprint
    new = _engine(client, new_cfg, tmp_path)
    result = new._friday_close_go(close, "NO-GO")
    assert result["go"] == "NO-GO" and result["go_basis"].startswith("CATCH-UP")
    assert old._friday_close_go(close, "NO-GO")["go"] == "GO"


# 2 / 3 ---------------------------------------------------------------------------------------------------------------
def _legacy_db(path: Path, extra_sql: str = ""):
    with sqlite3.connect(path) as db:
        db.executescript(f"""
            CREATE TABLE trades (ticket INTEGER PRIMARY KEY, kind TEXT NOT NULL, side TEXT, session TEXT, magic INTEGER, volume REAL,
                open_time TEXT, open_price REAL, close_time TEXT, close_price REAL, sl REAL, tp REAL, pnl REAL, mfe REAL, mae REAL,
                duration_s INTEGER, exit_reason TEXT, account TEXT, symbol TEXT, setup_id TEXT, result_confirmed INTEGER, payload_json TEXT);
            INSERT INTO trades(ticket,kind,side,session,pnl,account,symbol,result_confirmed,close_time) VALUES
                (1,'PA','LONG','LONDON',2.0,NULL,NULL,1,'2026-09-01T09:00:00+00:00'),
                (2,'PA','SHORT','ASIA',-1.0,'A','XAUUSD',1,'2026-09-01T09:00:00+00:00');
            {extra_sql}
        """)


def test_migration_converts_null_identity_to_unknown_and_columns_are_not_null(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"; _legacy_db(path)
    AuditLogger(str(path), str(tmp_path / "a.jsonl"))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT account,symbol FROM trades WHERE ticket=1").fetchone() == ("unknown", "unknown")
        notnull = {row[1]: row[3] for row in db.execute("PRAGMA table_info(trades)")}
        assert notnull["account"] == 1 and notnull["symbol"] == 1
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO trades(ticket,kind,account,symbol) VALUES(9,'PA',NULL,'XAUUSD')")


def test_migration_is_restart_safe_with_leftover_trades_v3(tmp_path: Path):
    path = tmp_path / "legacy.sqlite3"
    _legacy_db(path, "CREATE TABLE trades_v3 (id INTEGER PRIMARY KEY, junk TEXT);")     # crash between create and rename
    AuditLogger(str(path), str(tmp_path / "a.jsonl"))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE name='trades_v3'").fetchone() is None
        assert [r[1] for r in db.execute("PRAGMA table_info(trades)") if r[5]] == ["id"]
    AuditLogger(str(path), str(tmp_path / "a.jsonl"))                                     # second open: no-op
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


def test_migration_rolls_back_atomically_on_failure(tmp_path: Path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"; _legacy_db(path)
    original = AuditLogger._migrate_trade_identity
    def boom(database):
        info = list(database.execute("PRAGMA table_info(trades)"))
        if [r[1] for r in info if r[5]] == ["id"]: return
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute("CREATE TABLE trades_v3 (id INTEGER PRIMARY KEY, ticket INTEGER, kind TEXT)")
            database.execute("INSERT INTO trades_v3(ticket,kind) SELECT ticket,kind FROM trades")
            database.execute("DROP TABLE trades")
            raise sqlite3.OperationalError("disk I/O error")                                 # crash before rename
        except Exception:
            database.execute("ROLLBACK"); raise
    monkeypatch.setattr(AuditLogger, "_migrate_trade_identity", staticmethod(boom))
    with pytest.raises(sqlite3.OperationalError):
        AuditLogger(str(path), str(tmp_path / "a.jsonl"))
    with sqlite3.connect(path) as db:                                                      # legacy table intact
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE name='trades_v3'").fetchone() is None
    monkeypatch.setattr(AuditLogger, "_migrate_trade_identity", staticmethod(original))
    AuditLogger(str(path), str(tmp_path / "a.jsonl"))                                       # real migration then succeeds
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2


# 4 -----------------------------------------------------------------------------------------------------------------
class _ReportLogger(_Logger):
    def __init__(self): super().__init__(); self.reports = []
    def performance_summary(self, start, end, session=None, *a, **k): return {"pa_trades": 0, "session": session}
    def scout_performance_summary(self, *a, **k): return {}
    def report_once(self, kind, key, payload): self.reports.append((kind, key, payload)); return True
    def report_exists(self, kind, key): return False
    def reports_between(self, *a, **k): return []


def test_session_summary_survives_crash_between_scout_close_and_report(config, tmp_path: Path):
    client = FakeClient(); end = datetime(2026, 9, 4, 16, tzinfo=UTC)
    e1 = _engine(client, config, tmp_path)
    e1._persist_pending_report(SessionName.LONDON, end, {"leader": "BUY"})              # boundary cycle: scouts closed …
    # … process dies here, before _emit_reports
    logger = _ReportLogger(); e2 = _engine(client, config, tmp_path, logger)
    e2.startup_cycle = True; e2.session_boundary_event = False
    e2._restore_pending_reports()
    assert any(k == "session_summary_recovered" for k, _ in logger.events)
    assert [(s, e) for s, e, _ in e2.closed_sessions] == [(SessionName.LONDON, end)]
    from types import SimpleNamespace
    e2._emit_reports(end + timedelta(minutes=1), SimpleNamespace(pa_side=None, scout=SimpleNamespace(leader="NONE", market_speed="SLOW"), go_status="NO-GO"), [])
    assert any(kind == "session_summary" and payload["session"] == "LONDON" and payload["scout"] == {"leader": "BUY"} for kind, _, payload in logger.reports)
    assert e2.positions.meta.get("pending_reports") == {}                                # cleared after the write


def test_pending_report_from_another_fingerprint_is_discarded(config, tmp_path: Path):
    client = FakeClient(); end = datetime(2026, 9, 4, 16, tzinfo=UTC)
    old = _engine(client, config, tmp_path); old._persist_pending_report(SessionName.LONDON, end, {})
    raw = config.model_dump(); raw["analysis"]["min_confluence"] = config.analysis.min_confluence + 1
    new = _engine(client, BotConfig.model_validate(raw), tmp_path); new._restore_pending_reports()
    assert new.closed_sessions == []


# 5 -----------------------------------------------------------------------------------------------------------------
def test_event_keys_are_backfilled_and_duplicates_collapsed(tmp_path: Path):
    path = tmp_path / "v22.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL);
            INSERT INTO events(timestamp,event_type,payload_json) VALUES
              ('2026-09-01T08:00:00+00:00','scout_session_stats','{"session":"ASIA","session_id":"ASIA@2026-09-01T00:00:00+00:00","account":"A","symbol":"XAUUSD","config_fingerprint":"fp","leader":"BUY","open_time":"2026-09-01T00:00:00+00:00","close_time":"2026-09-01T08:00:00+00:00","buy_mfe":2,"buy_mae":0,"sell_mfe":0,"sell_mae":2}'),
              ('2026-09-01T08:00:05+00:00','scout_session_stats','{"session":"ASIA","session_id":"ASIA@2026-09-01T00:00:00+00:00","account":"A","symbol":"XAUUSD","config_fingerprint":"fp","leader":"BUY","open_time":"2026-09-01T00:00:00+00:00","close_time":"2026-09-01T08:00:00+00:00","buy_mfe":2,"buy_mae":0,"sell_mfe":0,"sell_mae":2}');
        """)
    logger = AuditLogger(str(path), str(tmp_path / "a.jsonl"))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE event_type='scout_session_stats'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM event_keys WHERE event_key='A:XAUUSD:fp:ASIA@2026-09-01T00:00:00+00:00'").fetchone()[0] == 1
    # a v2.2 pending summary re-emitted after upgrade is a no-op
    assert logger.event_once("scout_session_stats", "A:XAUUSD:fp:ASIA@2026-09-01T00:00:00+00:00", {"session": "ASIA"}) is False
    start, end = datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
    assert logger.scout_performance_summary(start, end, 1.0, account="A", config_fingerprint="fp", symbol="XAUUSD")["sessions"]["ASIA"]["sessions"] == 1
