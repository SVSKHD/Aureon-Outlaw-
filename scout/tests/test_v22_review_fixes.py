from __future__ import annotations

import json
import queue
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeClient
from test_v18_features import _Logger
from test_v21_review_fixes import _load_outbox, _ns
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import SessionName
from xau_mt5_bot.position_manager import PositionManager
from xau_mt5_bot.scouts import ScoutManager

ROOT = Path(__file__).resolve().parents[1]


# 1 -----------------------------------------------------------------------------------------------------------------
def test_strategy_readiness_is_account_scoped(config, tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    base = {"kind": "PA", "result_confirmed": 1, "side": "LONG", "session": "LONDON", "symbol": "XAUUSD", "config_fingerprint": "fp",
            "open_time": "2026-09-01T08:00:00+00:00", "close_time": "2026-09-01T09:00:00+00:00", "pnl": 1.0}
    for i in range(20): logger.trade({**base, "ticket": i, "account": "A"})
    assert logger.confirmed_trade_count("fp", account="A", symbol="XAUUSD") == 20
    assert logger.confirmed_trade_count("fp", account="B", symbol="XAUUSD") == 0
    assert logger.confirmed_trade_count("fp", account="A", symbol="XAGUSD") == 0
    src = (ROOT / "src" / "xau_mt5_bot" / "engine.py").read_text()
    assert "confirmed_trade_count(self.strategy_fingerprint, account=self.account_key, symbol=self.config.symbol)" in src
    assert 'closed_trade.get("account") == self.account_key' in src


# 2 -----------------------------------------------------------------------------------------------------------------
def test_go_tally_is_per_session_instance_not_per_session_name(config, tmp_path: Path):
    client = FakeClient()
    engine = TradingEngine(client, config, _Logger()); engine.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    friday_london = datetime(2026, 9, 4, 9, tzinfo=UTC)          # London Sep 4
    engine._record_session_go(SessionName.LONDON, "GO", friday_london)
    # bot misses Friday's close, restarts during Monday London
    monday_london = datetime(2026, 9, 7, 9, tzinfo=UTC)
    engine2 = TradingEngine(client, config, _Logger()); engine2.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    engine2._record_session_go(SessionName.LONDON, "NO-GO", monday_london)
    report = engine2._session_go_report(SessionName.LONDON, monday_london + timedelta(hours=3))
    assert report["go"] == "NO-GO" and "LONDON@2026-09-07" in report["session_instance"]
    old = engine2._session_go_report(SessionName.LONDON, friday_london + timedelta(hours=3))
    assert old["go"] == "GO" and "LONDON@2026-09-04" in old["session_instance"]


# 3 -----------------------------------------------------------------------------------------------------------------
def test_pending_stats_from_another_fingerprint_are_dropped(config, tmp_path: Path):
    client = FakeClient(); events = []
    old = ScoutManager(client, config); old.state_path = str(tmp_path / "s.json"); old.account_key = "A"; old.fingerprint = "OLD"
    old._pending_stats = {"session": "LONDON", "account": "A", "symbol": "XAUUSD", "config_fingerprint": "OLD", "leader": "BUY"}
    old._persist()
    new = ScoutManager(client, config, audit=lambda k, p: events.append(k)); new.state_path = old.state_path; new.account_key = "A"; new.fingerprint = "NEW"
    new.restore()
    assert new._pending_stats is None and "scout_pending_stats_dropped" in events and new.calibration_sessions == 0


# 4 -----------------------------------------------------------------------------------------------------------------
def test_event_trading_date_comes_from_the_running_cycle_or_now(config):
    block = _load_outbox(); calls = []
    class _Discord:
        def is_eligible(self, kind): return False
    class _Sink:
        def event(self, *a): calls.append(a)
    ns = _ns(_Discord(), _Sink()); exec(block, ns)
    ns["fanout"]("startup", {})                                                    # no engine yet
    ns["engine_ref"]["engine"] = SimpleNamespace(cycle_tdate=datetime(2026, 9, 8).date())   # inside a rollover cycle
    ns["fanout"]("session_transition", {})
    ns["firestore_box"].drain(5)
    assert calls[0][2] is not None                                                # startup event dated, never null
    assert calls[1][2] == datetime(2026, 9, 8).date()                             # cycle's own trading date, not the previous one
    src = (ROOT / "src" / "xau_mt5_bot" / "engine.py").read_text()
    assert src.index("self.cycle_tdate = self.sessions.broker_trading_date(now)") < src.index("self._validate_clock(now)")


# 5 -----------------------------------------------------------------------------------------------------------------
def test_drain_is_bounded_even_when_queue_is_full_and_worker_stalled():
    block = _load_outbox(); gate = threading.Event()
    class _Discord:
        def is_eligible(self, kind): return True
        def event(self, kind, payload): gate.wait(30); return True
    class _Sink:
        def event(self, *a): pass
        def mark_discord(self, *a): pass
    ns = _ns(_Discord(), _Sink()); exec(block, ns)
    box = ns["discord_box"]
    box.put(ns["_discord_then_mark"], "cycle_slow", {}, "e0")                     # worker now stalls on this
    time.sleep(0.2)
    while not box.q.full(): box.q.put_nowait((lambda: None,))                       # fill the queue behind it
    t0 = time.time(); drained = box.drain(timeout=1.0); elapsed = time.time() - t0
    assert drained is False and elapsed < 3.0                                       # bounded, no indefinite block
    gate.set(); box.thread.join(5)
    src = (ROOT / "src" / "xau_mt5_bot" / "main.py").read_text()
    assert src.index("drained_discord = discord_box.drain") < src.index("drained_firestore = firestore_box.drain")
    assert "shutdown_discord_pending" in src


# minors ---------------------------------------------------------------------------------------------------------------
def test_duplicate_session_stats_count_once_in_sqlite_recovery(config, tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    stat = {"session": "ASIA", "account": "A", "symbol": "XAUUSD", "config_fingerprint": "fp", "session_id": "ASIA@2026-09-04T00:00:00+00:00"}
    logger.event("scout_session_stats", stat); logger.event("scout_session_stats", stat)
    logger.event("scout_session_stats", {**stat, "session_id": "ASIA@2026-09-07T00:00:00+00:00"})
    assert logger.scoped_event_count("scout_session_stats", "A", "XAUUSD", "fp") == 2


def test_stats_event_is_idempotent_and_written_before_state_is_cleared():
    src = (ROOT / "src" / "xau_mt5_bot" / "scouts.py").read_text()
    a = src.index('self.audit_once("scout_session_stats", stats.get("stats_key")')
    b = src.index("self._pending_stats = None; self.calibration_sessions += 1")
    assert a < b < src.index("self._persist()", b)


def test_weekly_go_filter_rejects_legacy_reports():
    src = (ROOT / "src" / "xau_mt5_bot" / "engine.py").read_text()
    assert 'item.get("account") == self.account_key and item.get("symbol") == self.config.symbol' in src
    assert 'item.get("account") in (None, self.account_key)' not in src


def test_schema_documents_discord_fields_and_null():
    contract = json.loads((ROOT / "FIRESTORE_SCHEMA.json").read_text())
    assert "discord_eligible" in contract["collections"]["events"]["required"]
    assert None in contract["collections"]["events"]["posted_discord_values"]
    md = (ROOT / "FIRESTORE_SCHEMA.md").read_text()
    assert "boolean or null" in md and "discord_eligible" in md


def test_discord_eligibility_expression_is_clean():
    src = (ROOT / "src" / "xau_mt5_bot" / "notify.py").read_text()
    assert "ELIGIBLE_EVENTS or" not in src
