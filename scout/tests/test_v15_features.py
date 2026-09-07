from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import FakeClient, bars
from xau_mt5_bot.decision_router import final_decision_router
from xau_mt5_bot.liquidity import previous_period_levels
from xau_mt5_bot.logger import AuditLogger
from xau_mt5_bot.models import (
    Action, DecisionInput, EntryState, Freshness, ScoutSnapshot, ScoutVerdict,
    SessionName, Side, SpreadState, TargetRealism, TriggerResult,
)
from xau_mt5_bot.notify import Discord
from xau_mt5_bot.scouts import ScoutManager


def _decision(scout, **changes):
    values = dict(pa_side=Side.LONG, setup_valid=True, entry_state=EntryState.CONFIRMED,
                  trigger=TriggerResult(True, "M5", fresh=True), scout=scout, freshness=Freshness.LIVE,
                  spread_state=SpreadState.NORMAL, account_safe=True, rr=2, min_rr=1,
                  target_realism=TargetRealism.REALISTIC, confluence=70, min_confluence=55,
                  scout_contradiction_threshold=8, hold_when_slow=True)
    values.update(changes)
    return DecisionInput(**values)


def test_slow_market_gets_hold_next_session_guidance(config):
    client = FakeClient(); manager = ScoutManager(client, config)
    now = client.tick.time
    manager.open_session(SessionName.LONDON, now - timedelta(minutes=20))
    manager.session_open_price = 2500.0
    manager.price_samples = [((now - timedelta(minutes=10)).isoformat(), 2500.15)]
    client.tick = client.tick.__class__(now, 2500.10, 2500.20)
    scout = manager.snapshot(Side.LONG)
    assert scout.market_speed == "SLOW" and "next session" in scout.guidance
    decision = final_decision_router(_decision(scout))
    assert decision.action == Action.WAIT and "next session" in decision.reason


def test_fast_market_and_strength_are_configurable(config):
    config.scout_analysis.fast_velocity_price_per_min = .10
    config.scout_analysis.strength_price_step = 2.0
    config.scout_analysis.min_verdict_strength = 3
    client = FakeClient(); manager = ScoutManager(client, config); now = client.tick.time
    manager.open_session(SessionName.LONDON, now - timedelta(minutes=20)); manager.session_open_price = 2500
    manager.price_samples = [((now - timedelta(minutes=10)).isoformat(), 2500.0)]
    client.tick = client.tick.__class__(now, 2502.0, 2502.2)
    positions = client.positions("XAUUSD", config.magic.scout_london)
    positions[0].profit, positions[1].profit = 5.0, -5.0
    scout = manager.snapshot(Side.LONG)
    assert scout.market_speed == "FAST" and scout.strength == 2 and scout.verdict == ScoutVerdict.NEUTRAL


def test_previous_year_high_low_are_valid_next_year():
    frame = bars("2024-01-02", [(100, 130, 80, 110), (110, 140, 90, 120), (120, 125, 100, 121)], "365D")
    levels = previous_period_levels(frame)
    pyh = [x for x in levels if x.kind == "PYH"]
    pyl = [x for x in levels if x.kind == "PYL"]
    assert pyh and pyl and pyh[0].price == 130 and pyl[0].price == 80
    assert pyh[0].valid_from.year == 2025


def test_discord_timezone_is_dynamic_and_scout_lifecycle_is_forwarded(monkeypatch):
    discord = Discord("MISSING_ENV"); sent = []
    monkeypatch.setattr(discord, "send", lambda value="", embed=None: sent.append(embed or value) or True)
    discord.event("scout_session_open", {"session": "LONDON"})
    discord.event("scout_rollback", {"reason": "test"})
    assert len(sent) == 2
    source = __import__("inspect").getsource(Discord.format_snapshot)
    assert 'strftime("%H:%M %Z")' in source and "%H:%M IST" not in source


def test_performance_reports_are_aggregated_and_once_only(tmp_path):
    logger = AuditLogger(str(tmp_path / "t.sqlite3"), str(tmp_path / "a.jsonl"))
    start = datetime(2026, 9, 1, tzinfo=UTC); end = start + timedelta(days=7)
    base = {"kind": "PA", "side": "LONG", "session": "LONDON", "magic": 12001, "volume": .1,
            "open_time": start, "close_time": start + timedelta(hours=1), "open_price": 2500, "close_price": 2505,
            "sl": 2495, "tp": 2505, "mfe": 60, "mae": -10, "duration_s": 3600, "exit_reason": "TP3",
            "account": "demo", "symbol": "XAUUSD", "setup_id": "a", "result_confirmed": 1}
    logger.trade({**base, "ticket": 1, "pnl": 50})
    logger.trade({**base, "ticket": 2, "pnl": -20})
    summary = logger.performance_summary(start, end)
    assert summary["pa_trades"] == 2 and summary["wins"] == 1 and summary["losses"] == 1 and summary["net_pnl"] == 30
    emitted = []; logger.event = lambda kind, payload: emitted.append((kind, payload))
    assert logger.report_once("weekly_report", "2026-09-01", summary)
    assert not logger.report_once("weekly_report", "2026-09-01", summary)
    assert len(emitted) == 1


def test_weekly_and_next_week_reports_have_engine_hooks():
    import inspect
    from xau_mt5_bot.engine import TradingEngine
    source = inspect.getsource(TradingEngine._emit_reports)
    assert '"session_summary"' in source and '"weekly_report"' in source and '"next_week_open_report"' in source
