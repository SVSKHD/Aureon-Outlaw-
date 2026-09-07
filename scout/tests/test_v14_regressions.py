from __future__ import annotations

import inspect
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd

from conftest import FakeClient, bars
from xau_mt5_bot.decision_router import final_decision_router
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.execution import execute_pa_trade, send_with_retry
from xau_mt5_bot.liquidity import detect_sweeps
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import (
    Action, Decision, DecisionInput, EntryState, Freshness, LiquidityLevel, ScoutSnapshot,
    SessionName, Side, SpreadState, StructureResult, StructureState, TargetRealism,
    TradePlan, TriggerResult, Zone,
)
from xau_mt5_bot.position_manager import PositionManager
from xau_mt5_bot.scouts import ScoutManager


def _plan(side=Side.LONG, volume=.12):
    entry = 2500.2 if side == Side.LONG else 2500.0
    sl = 2495.0 if side == Side.LONG else 2505.0
    tps = [2505.2, 2510.2, 2515.2] if side == Side.LONG else [2495.0, 2490.0, 2485.0]
    return TradePlan(side, entry, sl, tps, "test", "test", [1, 2, 3], TargetRealism.REALISTIC,
                     volume, entry, entry, sl)


def _manager(tmp_path, config, side=Side.LONG):
    client = FakeClient(); config.management.partial_tp1_percent = 50; config.management.partial_tp2_percent = 25
    p = client.send_market("XAUUSD", side.value, .12, config.magic.pa, "PA", _plan(side).stop_loss, 0)
    position = client.positions("XAUUSD", config.magic.pa)[0]
    manager = PositionManager(client, config, state_dir=str(tmp_path), account_key="demo")
    manager.track(position, "PA", "LONDON", side=side.value, setup_id="s", plan=asdict(_plan(side)))
    return client, manager


def test_partial_tp_percentages_and_sl_sequence(tmp_path, config):
    client, manager = _manager(tmp_path, config)
    client.tick = client.tick.__class__(client.tick.time, 2505.3, 2505.5)
    manager.update(pd.DataFrame(), bid=client.tick.bid, ask=client.tick.ask)
    rec = manager.tracked[next(iter(manager.tracked))]
    assert client.positions("XAUUSD", config.magic.pa)[0].volume == .06
    assert rec["tp1_done"] and rec["breakeven_done"] and rec["sl"] > rec["actual_entry"]
    client.tick = client.tick.__class__(client.tick.time, 2510.3, 2510.5)
    manager.update(pd.DataFrame(), bid=client.tick.bid, ask=client.tick.ask)
    assert client.positions("XAUUSD", config.magic.pa)[0].volume == .03
    assert rec["tp2_done"] and rec["sl"] == 2505.2


def test_breakeven_waits_for_confirmed_partial(tmp_path, config):
    client, manager = _manager(tmp_path, config)
    client.close_partial = lambda *a, **k: __import__("xau_mt5_bot.mt5_client", fromlist=["OrderResult"]).OrderResult(False, 10006, a[0], None, "reject")
    client.tick = client.tick.__class__(client.tick.time, 2505.3, 2505.5)
    manager.update(pd.DataFrame(), bid=client.tick.bid, ask=client.tick.ask)
    rec = manager.tracked[next(iter(manager.tracked))]
    assert not rec["tp1_done"] and not rec["breakeven_done"] and rec["sl"] == 2495.0


def test_long_uses_bid_and_short_uses_ask(tmp_path, config):
    client, manager = _manager(tmp_path / "l", config, Side.LONG)
    client.tick = client.tick.__class__(client.tick.time, 2505.1, 2505.3)
    manager.update(pd.DataFrame(), bid=client.tick.bid, ask=client.tick.ask)
    assert not next(iter(manager.tracked.values()))["tp1_done"]
    client2, manager2 = _manager(tmp_path / "s", config, Side.SHORT)
    client2.tick = client2.tick.__class__(client2.tick.time, 2494.8, 2495.1)
    manager2.update(pd.DataFrame(), bid=client2.tick.bid, ask=client2.tick.ask)
    assert not next(iter(manager2.tracked.values()))["tp1_done"]


def test_partial_management_opens_without_broker_tp(config):
    client = FakeClient(); config.safety.allow_pa_orders = True
    decision = Decision(Action.LONG, "ok", datetime.now(UTC)); plan = _plan()
    result = execute_pa_trade(client, config, decision, plan)
    assert result.success and client.sent[-1]["tp"] == 0.0


def test_partial_fill_is_accepted_without_topup(config):
    client = FakeClient(); original = client.send_market
    def half(symbol, side, volume, magic, comment, sl=0, tp=0):
        return original(symbol, side, volume / 2, magic, comment, sl, tp)
    client.send_market = half
    result = send_with_retry(client, config, "XAUUSD", "LONG", .10, config.magic.pa, "PA_LONG")
    assert result.success and result.volume_filled == .05 and len(client.sent) == 1


def test_engine_passes_confirmed_m5_pivots():
    source = inspect.getsource(TradingEngine.run_cycle)
    assert 'structures["M5"].pivots' in source


def test_trigger_ownership_and_consumption_are_hard_vetoes():
    trigger = TriggerResult(True, "M5", fresh=True, setup_id="old", direction=Side.LONG)
    value = DecisionInput(Side.LONG, True, EntryState.CONFIRMED, trigger, ScoutSnapshot(SessionName.LONDON),
                          Freshness.LIVE, SpreadState.NORMAL, True, 2, 1, TargetRealism.REALISTIC, 70, 55, "new")
    assert final_decision_router(value).action == Action.NO_TRADE
    trigger.setup_id = "new"; trigger.consumed = True
    assert final_decision_router(value).action == Action.NO_TRADE


def test_sweep_identity_keeps_price_and_direction():
    frame = bars("2026-09-01T10:00:00", [(100, 102, 98, 100), (100, 101, 99, 100)], "5min")
    levels = [LiquidityLevel(99, "ROUND_1", "PRICE", frame.iloc[0].time.to_pydatetime(), 1),
              LiquidityLevel(101, "ROUND_1", "PRICE", frame.iloc[0].time.to_pydatetime(), 1)]
    events = detect_sweeps(frame, levels, 1)
    identities = {(e.level_price, e.direction, e.sweep_time) for e in events}
    assert (99, "BULLISH", frame.iloc[0].time.to_pydatetime()) in identities
    assert (101, "BEARISH", frame.iloc[0].time.to_pydatetime()) in identities
    assert len(identities) >= 2


def test_latest_levels_keep_multiple_swing_equal_and_round_prices():
    now = datetime.now(UTC)
    levels = [LiquidityLevel(p, kind, "M5", now, 2) for kind in ("SWING_HIGH", "EQUAL_LOW", "ROUND_5") for p in (100, 105)]
    kept = TradingEngine._latest_levels(levels, now)
    assert len(kept) == 6


def test_invalid_zones_do_not_add_confluence():
    structures = {k: StructureResult(k, StructureState.NEUTRAL) for k in ("D1", "H4", "H1", "M15", "M5")}
    now = datetime.now(UTC); active = Zone(99, 100, "SUPPORT", Side.LONG, now, now, score=9, status="active")
    invalid = Zone(99, 100, "SUPPORT", Side.LONG, now, now, score=99, status="invalidated")
    _, active_score = TradingEngine._price_action_direction(structures, [], [active], [], price=100, atr=1)
    _, invalid_score = TradingEngine._price_action_direction(structures, [], [invalid], [], price=100, atr=1)
    assert active_score > invalid_score


def test_deal_aggregation_includes_partials_costs_and_survives_restart(tmp_path, config):
    client, manager = _manager(tmp_path, config)
    client.tick = client.tick.__class__(client.tick.time, 2505.3, 2505.5); manager.update(pd.DataFrame(), bid=2505.3, ask=2505.5)
    ticket = int(next(iter(manager.tracked))); client.tick = client.tick.__class__(client.tick.time, 2511, 2511.2)
    manager._close_full(manager.tracked[str(ticket)], "MANUAL", 2511, {})
    records = manager.update(pd.DataFrame(), bid=2511, ask=2511.2)
    assert records and records[0]["pnl"] == sum(d["net"] for d in client.deals[ticket])
    # An unresolved close remains persistent with no retry limit.
    client2, manager2 = _manager(tmp_path / "pending", config); key = next(iter(manager2.tracked))
    manager2.pending_finalize[key] = manager2.tracked.pop(key); manager2._save()
    for _ in range(8): manager2._finalize_pending(0)
    restored = PositionManager(client2, config, state_dir=str(tmp_path / "pending"), account_key="demo")
    assert key in restored.pending_finalize


def test_scout_unequal_fills_are_rolled_back(config):
    client = FakeClient(); original = client.send_market
    def unequal(symbol, side, volume, magic, comment, sl=0, tp=0):
        return original(symbol, side, volume if side == "BUY" else volume / 2, magic, comment, sl, tp)
    client.send_market = unequal
    result = ScoutManager(client, config).open_session(SessionName.LONDON, datetime.now(UTC))
    assert not result.success and client.positions("XAUUSD", config.magic.scout_london) == []


def test_failed_scout_close_stays_queued_until_absence(config):
    client = FakeClient(); manager = ScoutManager(client, config)
    assert manager.open_session(SessionName.ASIA, datetime.now(UTC)).success
    original = client.close_position
    client.close_position = lambda ticket, magic: __import__("xau_mt5_bot.mt5_client", fromlist=["OrderResult"]).OrderResult(False, 10006, ticket, None, "busy")
    assert not manager.close_session(SessionName.ASIA).success and len(manager.pending_closures) == 2
    client.close_position = original; manager.retry_pending_closures()
    assert manager.pending_closures == [] and client.positions("XAUUSD", config.magic.scout_asia) == []


def test_sqlite_v12_trade_table_migrates(tmp_path):
    db = tmp_path / "old.sqlite3"
    with sqlite3.connect(db) as c: c.execute("CREATE TABLE trades(ticket INTEGER PRIMARY KEY, kind TEXT NOT NULL)")
    AuditLogger(str(db), str(tmp_path / "a.jsonl"))
    with sqlite3.connect(db) as c: columns = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
    assert {"requested_entry", "actual_entry", "initial_sl", "final_sl", "tp1", "partial_exits_json", "total_realized_pnl"} <= columns


def test_legacy_position_state_migrates(tmp_path, config):
    (tmp_path / "positions.json").write_text('{"pending_finalize":{"7":{"ticket":7}}}')
    manager = PositionManager(FakeClient(), config, state_dir=str(tmp_path), account_key="demo")
    assert "7" in manager.pending_finalize and not (tmp_path / "positions.json").exists()


def test_live_account_can_never_send_pa_order(config):
    client = FakeClient(demo=False); config.safety.allow_pa_orders = True; config.safety.allow_live_account = False
    result = execute_pa_trade(client, config, Decision(Action.LONG, "ok", datetime.now(UTC)), _plan())
    assert not result.success and client.sent == []


def test_session_end_uses_boundary_event_not_poll_window():
    source = inspect.getsource(TradingEngine.run_cycle)
    assert "session_end=self.pa_session_end_event" in source
