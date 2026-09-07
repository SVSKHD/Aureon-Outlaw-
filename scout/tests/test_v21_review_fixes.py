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
from test_v18_features import _BarClient, _CycleLogger, _Logger
from xau_mt5_bot.config import BotConfig, load_config
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.history import reset_history_cache
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import SessionName
from xau_mt5_bot.position_manager import PositionManager
from xau_mt5_bot.scouts import ScoutManager

ROOT = Path(__file__).resolve().parents[1]


# 1 -----------------------------------------------------------------------------------------------------------------
def test_real_session_stats_carry_symbol_and_recover_from_sqlite(config, tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    client = FakeClient(); manager = ScoutManager(client, config, audit=logger.event)
    manager.account_key = "A"; manager.fingerprint = "fpA"; manager.current_session = SessionName.LONDON
    magic = manager.magic(SessionName.LONDON)
    client.send_market("XAUUSD", "BUY", 0.01, magic, "a"); client.send_market("XAUUSD", "SELL", 0.01, magic, "b")
    assert manager.close_session(SessionName.LONDON).success
    assert logger.scoped_event_count("scout_session_stats", "A", "XAUUSD", "fpA") == 1


# 2 -----------------------------------------------------------------------------------------------------------------
def test_weekly_scout_pnl_is_account_scoped(config, tmp_path: Path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    base = {"kind": "SCOUT", "result_confirmed": 1, "side": "LONG", "session": "ASIA", "volume": 0.01, "symbol": "XAUUSD",
            "open_time": "2026-09-01T00:00:00+00:00", "close_time": "2026-09-01T08:00:00+00:00"}
    logger.trade({**base, "ticket": 1, "account": "A", "config_fingerprint": "fpA", "pnl": 1.0})
    logger.trade({**base, "ticket": 2, "account": "B", "config_fingerprint": "fpA", "pnl": 99.0})
    start, end = datetime(2026, 8, 31, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
    scoped = logger.scout_performance_summary(start, end, 1.0, account="A", config_fingerprint="fpA", symbol="XAUUSD")
    assert scoped["scout_legs"] == 1 and scoped["scout_net_pnl"] == pytest.approx(1.0)


# 3 / 4 --------------------------------------------------------------------------------------------------------------
def _load_outbox():
    """Execute main.py's Outbox/fanout wiring against stub Discord/Firestore objects."""
    src = (ROOT / "src" / "xau_mt5_bot" / "main.py").read_text()
    block = src[src.index("    import queue, threading\n    class Outbox:"):src.index("    logger.event = fanout\n")]
    block = "\n".join(line[4:] for line in block.splitlines())
    return block


def _ns(discord, sink):
    from xau_mt5_bot.sessions import SessionEngine
    return {"discord": discord, "sink": sink, "logger": SimpleNamespace(event=lambda k, p: None), "Any": object,
            "SessionEngine": SessionEngine, "datetime": datetime, "UTC": UTC}


def test_firestore_receives_event_even_when_discord_is_blocked():
    block = _load_outbox()
    firestore_calls, discord_started = [], threading.Event()
    class _Discord:
        def is_eligible(self, kind): return True
        def event(self, kind, payload): discord_started.set(); time.sleep(3); return True
    class _Sink:
        def event(self, *a): firestore_calls.append(("event", a))
        def mark_discord(self, *a): firestore_calls.append(("mark", a))
    ns = _ns(_Discord(), _Sink()); exec(block, ns)
    ns["fanout"]("cycle_slow", {"x": 1})
    assert discord_started.wait(2)
    deadline = time.time() + 1.0
    while time.time() < deadline and not any(c[0] == "event" for c in firestore_calls): time.sleep(0.01)
    assert any(c[0] == "event" for c in firestore_calls), "Firestore write waited on Discord"
    ev = next(c for c in firestore_calls if c[0] == "event")[1]
    assert ev[0] == "cycle_slow" and ev[2] is not None and ev[3] is True and isinstance(ev[4], str)   # v2.2.0: startup events dated
    ns["discord_box"].drain(10); ns["firestore_box"].drain(10)
    assert any(c[0] == "mark" and c[1][1] is True for c in firestore_calls)


def test_ineligible_event_skips_discord_queue_but_reaches_firestore():
    block = _load_outbox(); calls = []
    class _Discord:
        def is_eligible(self, kind): return False
        def event(self, *a): raise AssertionError("must not be called")
    class _Sink:
        def event(self, *a): calls.append(a)
        def mark_discord(self, *a): raise AssertionError("must not be called")
    ns = _ns(_Discord(), _Sink()); exec(block, ns); ns["fanout"]("clock_check", {})
    ns["discord_box"].drain(5); ns["firestore_box"].drain(5)
    assert len(calls) == 1 and calls[0][3] is False


def test_shutdown_drains_both_queues_without_short_circuit():
    src = (ROOT / "src" / "xau_mt5_bot" / "main.py").read_text()
    a = src.index("drained_discord = discord_box.drain(timeout=20.0)"); b = src.index("drained_firestore = firestore_box.drain(timeout=20.0)")
    assert a < b and " and firestore_box.drain" not in src


# 5 -----------------------------------------------------------------------------------------------------------------
class _OvernightClient(FakeClient):
    def closed_deals(self, ticket, *a, **k):
        return [{"entry": 1, "ticket": 9, "price": 2480.0, "profit": -1200.0, "commission": 0, "swap": 0,
                 "time": datetime(2026, 9, 4, 1, 30, tzinfo=UTC), "reason": "SL", "volume": 0.01}]   # 21:30 NY Sep 3 → trading date Sep 4


def test_overnight_loss_is_charged_to_the_closing_trading_date(config, tmp_path: Path):
    config.risk.daily_max_loss_percent = 2.0
    client = _OvernightClient()
    pm = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    pos = SimpleNamespace(ticket=7, symbol="XAUUSD", magic=config.magic.pa, volume=0.01, type=0, price_open=2500.0, sl=2480.0, tp=0.0, profit=0.0,
                          time=int(datetime(2026, 9, 3, 15, tzinfo=UTC).timestamp()))
    pm.track(pos, "PA", "NEW_YORK", side="LONG", setup_id="s", tdate="2026-09-03")           # opened on the Sep 3 trading day
    closed = pm.update(__import__("pandas").DataFrame(), bid=2480.0, ask=2480.2)
    assert closed and closed[0]["close_trading_date"] == "2026-09-04"
    assert pm.risk_allowed("2026-09-04", 50000)[0] is False


# 6 -----------------------------------------------------------------------------------------------------------------
def test_go_tally_survives_restart(config, tmp_path: Path):
    client = FakeClient()
    e1 = TradingEngine(client, config, _Logger()); e1.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    t = datetime(2026, 9, 4, 9, tzinfo=UTC)
    e1._record_session_go(SessionName.LONDON, "GO", t)
    e2 = TradingEngine(client, config, _Logger()); e2.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    assert e2._session_go_report(SessionName.LONDON)["go"] == "GO"


def test_friday_close_go_is_recorded_and_used_by_the_outlook(config, tmp_path: Path):
    client = FakeClient()
    engine = TradingEngine(client, config, _Logger()); engine.positions = PositionManager(client, config, state_dir=str(tmp_path), account_key="t")
    close_time = datetime(2026, 9, 4, 21, tzinfo=UTC)
    engine._remember_friday_close_go(close_time, "GO")
    assert engine._friday_close_go(close_time, "NO-GO")["go"] == "GO"
    assert engine._friday_close_go(close_time + timedelta(days=7), "NO-GO")["go_basis"].startswith("CATCH-UP")


# 7 -----------------------------------------------------------------------------------------------------------------
def test_reports_and_keys_are_scoped_per_account(config):
    class ReportLogger(_Logger):
        def __init__(self): super().__init__(); self.reports = []; self.calls = []
        def performance_summary(self, start, end, session, *a, **k): self.calls.append(k); return {"pa_trades": 0}
        def scout_performance_summary(self, *a, **k): return {}
        def report_once(self, kind, key, payload): self.reports.append((kind, key, payload)); return True
        def report_exists(self, kind, key): return False
        def reports_between(self, *a, **k): return []
    logger = ReportLogger(); engine = TradingEngine(FakeClient(), config, logger)
    engine.account_key = "ACC1"; engine.startup_cycle = True; engine.session_boundary_event = False; engine.closed_sessions = []
    snap = SimpleNamespace(pa_side=None, scout=SimpleNamespace(leader="NONE", market_speed="SLOW"))
    engine._emit_reports(datetime(2026, 9, 7, 12, tzinfo=UTC), snap, [])
    assert logger.reports and all(key.startswith(f"ACC1:XAUUSD:{engine.strategy_fingerprint}:") for _, key, _ in logger.reports)
    assert all(c.get("account") == "ACC1" and c.get("symbol") == "XAUUSD" for c in logger.calls if c)


# 9 -----------------------------------------------------------------------------------------------------------------
class _SecondLegStuck(FakeClient):
    def __init__(self): super().__init__(); self.fail_ticket = None
    def close_position(self, ticket, magic):
        from xau_mt5_bot.mt5_client import OrderResult
        if ticket == self.fail_ticket: return OrderResult(False, 10006, ticket, None, "rejected")
        return super().close_position(ticket, magic)


def test_partial_pair_close_keeps_the_original_pair_summary(config, tmp_path: Path):
    client = _SecondLegStuck(); events = []
    m = ScoutManager(client, config, audit=lambda k, p: events.append((k, p))); m.state_path = str(tmp_path / "s.json")
    m.account_key = "A"; m.current_session = SessionName.LONDON; magic = m.magic(SessionName.LONDON)
    client.send_market("XAUUSD", "BUY", 0.01, magic, "a"); client.send_market("XAUUSD", "SELL", 0.01, magic, "b")
    client.fail_ticket = client.positions("XAUUSD", magic)[1].ticket
    assert m.close_session(SessionName.LONDON).success is False
    pair_summary = dict(m._pending_stats)
    assert len(client.positions("XAUUSD", magic)) == 1
    assert m.close_session(SessionName.LONDON).success is False                      # retry with one leg
    assert m._pending_stats == pair_summary                                            # not rebuilt from the single leg
    fresh = ScoutManager(client, config); fresh.state_path = m.state_path; fresh.account_key = "A"; fresh.restore()
    assert fresh._pending_stats == pair_summary                                        # persisted across restart
    client.fail_ticket = None
    assert m.close_session(SessionName.LONDON).success is True
    assert [k for k, _ in events].count("scout_session_stats") == 1 and m.calibration_sessions == 1


# 10 ----------------------------------------------------------------------------------------------------------------
def test_live_strategy_samples_ignore_old_fingerprint_results():
    src = (ROOT / "src" / "xau_mt5_bot" / "engine.py").read_text()
    assert src.count('closed_trade.get("config_fingerprint") == self.strategy_fingerprint') == 3
    assert 'if closed_trade["kind"] == "PA": self.strategy_samples += 1' not in src


# 11 ----------------------------------------------------------------------------------------------------------------
def test_legacy_unscoped_calibration_file_is_not_trusted(config, tmp_path: Path):
    import json as _json
    path = tmp_path / "s.json"; path.write_text(_json.dumps({"calibration_sessions": 20, "session": "CLOSED"}))
    events = []; m = ScoutManager(FakeClient(), config, audit=lambda k, p: events.append(k))
    m.state_path = str(path); m.account_key = "A"; m.fingerprint = "fp"; m.restore()
    assert m.calibration_sessions == 0 and "pace_calibration_reset" in events


# 12 ----------------------------------------------------------------------------------------------------------------
def test_unknown_config_keys_are_rejected(tmp_path: Path):
    import yaml
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["reporting"]["max_manual_risk_to_daily_target_ratio"] = 3.0
    with pytest.raises(Exception):
        BotConfig.model_validate(raw)
    raw["reporting"].pop("max_manual_risk_to_daily_target_ratio"); raw["typo_key"] = 1
    with pytest.raises(Exception):
        BotConfig.model_validate(raw)


# 13 ----------------------------------------------------------------------------------------------------------------
def test_docs_do_not_carry_stale_values():
    checklist = (ROOT / "FORWARD_TEST_CHECKLIST.md").read_text()
    schema_md = (ROOT / "FIRESTORE_SCHEMA.md").read_text()
    readme = (ROOT / "README.md").read_text()
    assert "$5 daily target" not in schema_md and "1.9" not in schema_md.split("\n")[0]
    assert "manual risk limit is $5" not in checklist and "risk limit $5" not in checklist
    assert "21 tests" not in readme
