"""v3.3.0 — the decision has to be readable, and every card has to tell the whole story."""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_v18_features import _BarClient
from test_v31_discord_bot import _SilverClient, _project
from xau_mt5_bot.cards import (
    _sweep_lines,
    checklist_block,
    blocked_by_line,
    detections_card,
    event_card,
    next_line,
    offset_line,
    order_card,
    reason_hint,
    scout_card,
    status_card,
)
from xau_mt5_bot.decision_router import GATE_ORDER, explain_decision, final_decision_router
from xau_mt5_bot.models import Decision
from xau_mt5_bot.discord_bot import HELP, BotState, dispatch, fmt_clock, fmt_why
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.history import reset_history_cache
from xau_mt5_bot.models import (
    Action,
    DecisionInput,
    EntryState,
    Freshness,
    ScoutSnapshot,
    ScoutVerdict,
    SessionName,
    Side,
    SpreadState,
    TargetRealism,
    TriggerResult,
)
from xau_mt5_bot.report import format_report

ROOT = Path(__file__).resolve().parents[1]


def _input(**over) -> DecisionInput:
    scout = ScoutSnapshot(SessionName.LONDON)
    scout.market_speed = "NORMAL"
    base = dict(pa_side=Side.LONG, setup_valid=True, entry_state=EntryState.CONFIRMED,
                trigger=TriggerResult(True, "M1", reason="sweep+BOS", fresh=True, setup_id="s1", direction=Side.LONG),
                scout=scout,
                freshness=Freshness.LIVE, spread_state=SpreadState.NORMAL, account_safe=True, rr=1.5,
                min_rr=1.0, target_realism=TargetRealism.REALISTIC, confluence=70, min_confluence=55,
                setup_id="s1", scout_contradiction_threshold=8, hold_when_slow=True)
    base.update(over)
    return DecisionInput(**base)


def test_every_snapshot_carries_the_trace_into_the_report_and_sqlite(tmp_path):
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _SilverClient()
    engine = TradingEngine(client, cfg, logger)
    first = engine.run_cycle(client.tick.time)
    second = engine.run_cycle(client.tick.time + timedelta(seconds=5))
    engine.shutdown()

    for snapshot in (first, second):
        trace = snapshot.analysis["decision_trace"]                       # present in EVERY snapshot
        assert [g["name"] for g in trace["gates"]] == list(GATE_ORDER)
        assert trace["action"] == snapshot.decision.action.value
        assert trace["headline"] and trace["verdict"]
        assert [g["status"] for g in trace["gates"]].count("fail") <= 1
        assert snapshot.analysis["blocked_by"] == trace["blocked_by"]
        assert trace["levels"]["price"] is not None

    report = format_report(second, "UTC")
    assert "DECISION TRACE" in report and second.analysis["decision_trace"]["verdict"] in report
    assert "Levels:" in report

def test_why_and_clock_commands_answer_from_the_snapshot(tmp_path):
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _SilverClient()
    engine = TradingEngine(client, cfg, logger)
    engine.run_cycle(client.tick.time)
    engine.shutdown()
    state = BotState(tmp_path)

    why = dispatch(state, "!why")
    assert why == fmt_why(state) == dispatch(state, "!decide")            # !decide is an alias
    assert "gates passed" in why and "```" in why
    assert all(name in why for name in ("data fresh", "clock + account", "RR", "risk locks"))
    assert "Silver:" in why

    clock = dispatch(state, "!clock")
    assert clock == fmt_clock(state)
    assert "System UTC:" in clock and "Broker time:" in clock
    assert "Detected offset: UTC+0h" in clock and "Residual skew:" in clock
    assert "orders allowed" in clock

    assert "`!why`" in HELP and "`!clock`" in HELP and "!detected full" in HELP
    assert "not implemented by design" in dispatch(state, "!close")        # still no order commands
def test_why_and_clock_are_graceful_before_the_bot_has_run(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    state = BotState(tmp_path)
    assert "No snapshot yet" in dispatch(state, "!why")
    assert "No broker clock reading yet" in dispatch(state, "!clock")


def test_clock_command_falls_back_to_the_heartbeat(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "heartbeat.json").write_text(json.dumps(
        {"ts_epoch": 0, "broker_utc_offset_hours": 3.0, "broker_clock_residual_seconds": 1.2,
         "broker_clock_source": "auto", "broker_clock_ok": True, "broker_server": "Demo-Server"}))
    text = fmt_clock(BotState(tmp_path))
    assert "UTC+3h" in text and "Demo-Server" in text


def _decision_snapshot(**over) -> dict:
    snapshot = {"go_status": "NO-GO", "decision": {"action": "WAIT", "reason": "no fresh confirmation"},
                "confluence": 58, "pa_side": "LONG", "session": "ASIA", "symbol": "XAUUSD",
                "bid": 4400.0, "ask": 4400.2, "spread": 0.2, "freshness": "LIVE",
                "timestamp": "2026-09-07T01:00:00+00:00",
                "trade_plan": {"side": "LONG", "entry": 4401.0, "stop_loss": 4396.0, "sl_reason": "below the FVG",
                               "take_profits": [4406.0, 4411.0], "actual_rr": [1.0, 2.0]},
                "zones": [{"side": "LONG", "kind": "FVG", "low": 4396.25, "high": 4402.88, "score": 7, "status": "fresh"}],
                "scout": {}, "structures": {}, "patterns": [], "sweeps": [],
                "analysis": {"decision_trace": {
                    "headline": "🟠 WAIT · LONG bias 58/100 · inside zone, trigger · ASIA 06:30",
                    "state": "WAIT", "verdict": "LONG bias 58/100: blocked by inside zone (APPROACHING vs inside FVG 4396.25–4402.88); price 0.8 ATR from FVG 4396.25–4402.88.",
                    "gates": [{"name": "spread", "status": "pass", "icon": "✅", "value": "0.20", "threshold": "≤ 0.60", "passed": True},
                              {"name": "inside zone", "status": "fail", "icon": "❌", "value": "APPROACHING · 0.8 ATR away",
                               "threshold": "inside FVG 4396.25–4402.88", "passed": False},
                              {"name": "trigger", "status": "skip", "icon": "—", "value": "not reached", "threshold": "", "passed": True}],
                    "first_blocking": "inside zone",
                    "blocked_by": [{"name": "inside zone", "value": "APPROACHING · 0.8 ATR away", "threshold": "inside FVG 4396.25–4402.88"},
                                   {"name": "trigger", "value": "waiting", "threshold": "fresh M1/M5 confirmation"}],
                    "remaining_vetoes": ["trigger"],
                    "flips": ["1. inside zone: price back inside FVG 4396.25–4402.88", "2. trigger: an M1 sweep+BOS"],
                    "passed_count": 5, "gate_count": 14,
                    "evidence": {"for": [{"family": "htf", "label": "higher-timeframe trend", "points": 18}],
                                 "against": [{"family": "range", "label": "daily range exhausted", "points": -10}],
                                 "for_score": 58, "against_score": 20, "silver": "XAG COUPLED r=0.9"},
                    "levels": {"price": 4400.0, "zone": "FVG 4396.25–4402.88", "zone_kind": "FVG", "stop_loss": 4396.0,
                               "take_profit_1": 4406.0, "rr_1": 1.0, "distance_atr": 0.8, "spread": 0.2},
                    "go_meaning": "GO = signal, not an order.",
                    "footer": {"go_meaning": "GO = signal, not an order.", "remaining_vetoes": ["trigger"],
                               "hint": "!why for the full gate table"}},
                    "broker_clock": {"broker_utc_offset_hours": 3.0, "broker_clock_source": "auto", "residual_skew_seconds": 0.4}}}
    snapshot.update(over)
    return snapshot


def test_decision_card_renders_headline_verdict_checklist_flips_evidence_and_levels():
    card = status_card(_decision_snapshot(), "UTC")
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["title"].startswith("🟠 WAIT · LONG bias 58/100")
    assert card["color"] == 0xEF9F27                                              # amber: setup alive
    assert fields["Verdict"].startswith("LONG bias 58/100: blocked by inside zone")
    assert "✅ spread · 0.20 vs ≤ 0.60" in fields["Gates"]
    assert "❌ inside zone · APPROACHING · 0.8 ATR away" in fields["Gates"]
    assert "— trigger · not reached" in fields["Gates"]
    assert fields["What flips it"].startswith("1. inside zone: price back inside FVG")
    assert "higher-timeframe trend +18" in fields["Evidence FOR"] and "score 58" in fields["Evidence FOR"]
    assert "daily range exhausted -10" in fields["Evidence AGAINST"]
    assert "price 4400.00" in fields["Levels"] and "0.8 ATR away" in fields["Levels"]
    assert "SL 4396.00 · TP1 4406.00 (1.00R)" in fields["Levels"]
    assert "5/14 gates" in card["footer"]["text"]
    assert "still to clear: trigger" in card["footer"]["text"]
    assert "!why" in card["footer"]["text"]


def test_decision_card_colours_follow_the_state():
    assert status_card(_decision_snapshot(), "UTC")["color"] == 0xEF9F27           # WAIT
    no_trade = _decision_snapshot()
    no_trade["analysis"]["decision_trace"]["state"] = "NO_TRADE"
    assert status_card(no_trade, "UTC")["color"] == 0xD85A30                        # red
    closed = _decision_snapshot()
    closed["analysis"]["decision_trace"]["state"] = "CLOSED"
    assert status_card(closed, "UTC")["color"] == 0x8A8A8A                          # grey
    placed = _decision_snapshot()
    placed["analysis"]["decision_trace"]["state"] = "ORDER_PLACED"
    assert status_card(placed, "UTC")["color"] == 0x1D9E75                          # green
def test_blocked_by_says_so_when_nothing_is_blocking():
    assert "nothing" in blocked_by_line({"analysis": {"decision_trace": {"blocked_by": []}}})
    assert next_line({}) == "—"


# ---- scout cards ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("message, needle", [
    ("Scout orders blocked: broker clock skew 10799s > 600s", "automatic time sync"),
    ("Scout pair blocked: spread 0.90 above max 0.60", "max_spread_price"),
    ("Scout pair blocked: insufficient free margin", "risk.scout_lot"),
    ("Scout pair blocked: account not safe", "execution-safety"),
    ("SCOUTS DISABLED — ACCOUNT IS NETTING MODE", "HEDGING demo account"),
    ("Scout orders blocked: day locked: daily max loss -120.00", "next trading date"),
    ("Live account not authorized", "refuses to trade live"),
])
def test_scouts_not_placed_maps_reason_text_to_a_fix(message, needle):
    assert needle in reason_hint({"message": message})


def test_scouts_not_placed_card_carries_offset_attempt_and_next_retry():
    payload = {"session": "ASIA", "message": "Scout orders blocked: broker clock skew 10799s > 600s (residual after broker offset +0.0h)",
               "retryable": True, "attempt": 3, "next_retry": "2026-09-07T05:20:00+00:00",
               "display_timezone": "Asia/Kolkata", "broker_utc_offset_hours": 3.0, "broker_clock_source": "auto",
               "residual_skew_seconds": 10799.0, "max_clock_skew_seconds": 600}
    card = scout_card("scout_open_failed", payload)
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["color"] == 0xD85A30
    assert "UTC+3h" in fields["Fix"] and "10799s" in fields["Fix"]
    assert fields["Detected offset"] == "UTC+3h (auto) · residual 10799s"
    assert fields["Attempt"] == "#3"
    assert fields["Next retry"] == "10:50 (Asia/Kolkata)"


def test_scouts_placed_card_reports_both_legs_and_earlier_failures():
    card = scout_card("scout_session_open", {"session": "ASIA", "lot": 0.01, "magic": 11001, "open_price": 4400.1,
                                             "buy_ticket": 101, "sell_ticket": 102, "buy_entry": 4400.2,
                                             "sell_entry": 4400.0, "buy_sl": 4380.2, "sell_sl": 4420.0,
                                             "emergency_sl_price": 20.0, "open_time": "2026-09-07T00:00:00+00:00",
                                             "failed_attempts": 4}, "UTC")
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert "after 4 failed attempts" in card["title"] and card["color"] == 0x1D9E75
    assert "#101 @ 4400.20" in fields["BUY"] and "#102 @ 4400.00" in fields["SELL"]
    assert "SL 4380.20" in fields["BUY"]
    assert fields["Session open"] == "4400.10 at 00:00"
    assert "20.00 price distance" in fields["Emergency SL"]
    assert "after" not in scout_card("scout_session_open", {"session": "ASIA", "failed_attempts": 0})["title"]


def test_scout_rollback_card_names_the_failed_leg_and_what_was_closed():
    card = scout_card("scout_rollback", {"session": "LONDON", "failed_leg": "SELL", "retcode": 10019,
                                         "reason": "Not enough money", "closed_tickets": [201]})
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["title"].startswith("🔴 SCOUT LEG ROLLED BACK")
    assert "SELL" in fields["Failed leg"] and "10019" in fields["Failed leg"]
    assert fields["Closed"] == "#201"


def test_scouts_closed_card_reports_both_legs_pair_pnl_and_verdict():
    card = scout_card("scout_session_close", {"session": "NEW_YORK", "buy_ticket": 301, "sell_ticket": 302,
                                              "buy_pnl": 12.5, "sell_pnl": -8.25, "pnl": 4.25, "leader": "BUY",
                                              "verdict": "CONFIRMS", "strength": 7, "buy_mfe": 15.0, "buy_mae": -2.0,
                                              "sell_mfe": 1.0, "sell_mae": -11.0, "market_speed": "NORMAL",
                                              "displacement": 3.2})
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["color"] == 0x8A8A8A
    assert "#301" in fields["BUY"] and "12.50" in fields["BUY"] and "MFE 15.00" in fields["BUY"]
    assert "#302" in fields["SELL"] and "-8.25" in fields["SELL"]
    assert fields["Pair P/L"] == "4.25"
    assert fields["Leader"] == "BUY · CONFIRMS 7/10"


def test_scouts_adopted_card_reports_the_restart():
    card = scout_card("scout_adopted", {"session": "ASIA", "tickets": [1, 2], "legs": 2, "buy_ticket": 1,
                                        "sell_ticket": 2, "open_price": 4400.0, "lot": 0.01,
                                        "open_time": "2026-09-07T00:00:00+00:00", "mfe_mae_restored": [True, True]}, "UTC")
    assert card["title"].startswith("🟠 SCOUTS ADOPTED ON RESTART") and card["color"] == 0xEF9F27
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert "BUY #1" in fields["Tickets"] and "SELL #2" in fields["Tickets"]


def test_order_card_reuses_the_decision_skeleton_with_every_target():
    payload = {"ticket": 501, "side": "LONG", "session": "LONDON", "entry": 4400.5, "stop_loss": 4396.0,
               "sl_reason": "below the FVG", "take_profits": [4405.0, 4410.0, 4415.0], "actual_rr": [1.0, 2.11, 3.2],
               "volume": 0.02, "risk_price": 4.5, "risk_currency": 9.0, "zone_kind": "FVG", "zone": "4398.00–4401.00",
               "trigger_reason": "M1 sweep + BOS", "trigger_source": "M1", "confluence": 72, "spread": 0.21,
               "scout_verdict": "CONFIRMS", "scout_leader": "BUY", "scout_strength": 6,
               "silver": "COUPLED r=0.93 · SMT NONE", "invalidation": "M5 close below 4396"}
    card = order_card("order", payload)
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["title"] == "🟢 LONG PLACED · #501 · 0.02 lot" and card["color"] == 0x1D9E75
    assert "entry 4400.50" in card["description"] and "9.00 at 0.02 lot" in card["description"]
    assert "4405.00 (1.00R)" in fields["Verdict"] and "4415.00 (3.20R)" in fields["Verdict"]
    assert "✅ confluence · 72/100" in fields["Gates passed"]
    assert "✅ trigger · M1 M1 sweep + BOS" in fields["Gates passed"]
    assert "✅ RR · 1.00 to TP1" in fields["Gates passed"]
    assert "TP1 4405.00 → partial close, SL to break-even" in fields["Management plan"]
    assert "SL locked at TP1" in fields["Management plan"] and "invalidation: M5 close below 4396" in fields["Management plan"]
    assert "COUPLED" in card["footer"]["text"]
    assert event_card("order", payload)["title"] == card["title"]


@pytest.mark.parametrize("kind", ["pa_partial", "pa_breakeven", "pa_tp2_lock", "pa_trail", "pa_close"])
def test_management_cards_show_realised_pnl_remaining_volume_and_new_sl(kind):
    payload = {"ticket": 501, "side": "LONG", "session": "LONDON", "entry": 4400.5, "label": "PA_TP1",
               "confirmed_volume": 0.01, "realized_pnl": 5.25, "remaining_volume": 0.01, "original_volume": 0.02,
               "new_sl": 4400.6, "previous_sl": 4396.0, "sl": 4400.6, "take_profits": [4405.0], "actual_rr": [1.0],
               "success": True}
    card = order_card(kind, payload)
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert fields["Realised so far"] == "5.25"
    assert fields["Remaining volume"] == "0.01 of 0.02 lot"
    assert fields["New SL"] == "4400.60 (was 4396.00)"
    assert event_card(kind, payload)["fields"] == card["fields"]


def test_broker_clock_offset_event_gets_its_own_card():
    card = event_card("broker_clock_offset", {"broker_utc_offset_hours": 3.0, "source": "auto",
                                              "broker_clock_source": "auto", "residual_skew_seconds": 0.4,
                                              "server": "Demo-Server", "message": "MT5 server clock is UTC+3h"})
    assert card["title"] == "🕒 BROKER CLOCK · UTC+3h"
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert fields["Server"] == "Demo-Server" and "UTC+3h" in fields["Detected offset"]


def test_offset_line_handles_a_missing_measurement():
    assert offset_line({}) == "not measured yet"


# ---- detections card -----------------------------------------------------------------------------------
def _sweep(level_type: str, price: float, age: int, direction: str = "BULLISH") -> dict:
    return {"level_type": level_type, "level_price": price, "sweep_price": price - 0.4, "age_bars": age,
            "direction": direction, "active": True}


def test_detected_card_is_compact_and_full_mode_shows_everything():
    snapshot = {"session": "ASIA", "timestamp": "2026-09-07T01:00:00+00:00", "bid": 4410.0, "ask": 4410.2,
                "pa_side": "LONG", "confluence": 61,
                "structures": {"D1": {"state": "BULLISH"}, "H4": {"state": "BULLISH"}, "H1": {"state": "BEARISH"},
                               "M15": {"state": "BULLISH"}, "M5": {"state": "BULLISH"}},
                "sweeps": [_sweep("ROUND_1", 4407.0, 1), _sweep("ROUND_1", 4408.0, 2), _sweep("ROUND_1", 4409.0, 3),
                           _sweep("ROUND_1", 4409.0, 9),                       # duplicate level_type+price
                           _sweep("PDH", 4420.0, 4), _sweep("PDL", 4390.0, 5, "BEARISH"),
                           _sweep("ASIA_HIGH", 4415.0, 6), _sweep("EQUAL_HIGHS", 4418.0, 8),
                           _sweep("SWING_HIGH", 4419.0, 10)],
                "zones": [{"side": "LONG", "kind": "FVG", "low": 4400.0, "high": 4402.0, "score": 7, "status": "fresh"}],
                "analysis": {"atr": 2.0},
                "patterns": [{"name": f"Bullish Pin Bar {i}", "timestamp": "2026-09-07T01:00:00+00:00"} for i in range(6)]}

    compact = detections_card(snapshot, "UTC")
    fields = {f["name"]: f["value"] for f in compact["fields"]}
    assert compact["description"] == "D1 ▲ H4 ▲ H1 ▼ M15 ▲ M5 ▲"
    assert len(fields["Newest patterns"].split("\n")) == 3
    assert len(fields["Newest sweeps (LONG)"].split("\n")) == 3
    assert "BEARISH" not in fields["Newest sweeps (LONG)"] and "PDL" not in fields["Newest sweeps (LONG)"]
    assert "4.5 ATR away" in fields["Zones"]
    assert "!detected full" in compact["footer"]["text"]

    lines = _sweep_lines(snapshot, 6)
    assert len(lines) == 6 and lines[0] == "ROUND_1 ×3 (4407–4409)"                # deduped and collapsed
    assert sum(1 for line in lines if "ROUND_1" in line) == 1

def test_full_detected_card_keeps_two_structure_events_per_timeframe():
    events = [{"event": f"Bullish BOS {i}", "level": 4400 + i, "timestamp": "2026-09-07T01:00:00+00:00"} for i in range(5)]
    snapshot = {"session": "ASIA", "timestamp": "2026-09-07T01:00:00+00:00", "sweeps": [], "zones": [], "patterns": [],
                "structures": {tf: {"events": events} for tf in ("H4", "H1", "M15", "M5")}}
    card = detections_card(snapshot, "UTC", full=True)
    lines = next(f["value"] for f in card["fields"] if f["name"] == "Structure events").split("\n")
    assert len(lines) == 8                                                       # 2 per timeframe × 4 timeframes
    assert lines[0].startswith("H4 Bullish BOS 3") and lines[1].startswith("H4 Bullish BOS 4")
def test_a_utc_plus_three_broker_no_longer_blocks_the_scouts_in_a_full_cycle(tmp_path):
    """The reported failure, reproduced: UTC+3 server, Monday Asia session, scouts must be placed."""
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _BarClient(broker_offset_hours=3.0)
    client.tick = type(client.tick)(datetime(2026, 9, 7, 1, 0, tzinfo=UTC), 2500.0, 2500.2)
    engine = TradingEngine(client, cfg, logger)
    snapshot = engine.run_cycle(client.tick.time)
    engine.shutdown()

    assert snapshot.session == SessionName.ASIA
    assert engine.broker_offset_hours == 3.0 and engine.clock_ok
    assert len(client.positions("XAUUSD", cfg.magic.scout_asia)) == 2
    failures = logger._connect().execute(
        "SELECT COUNT(*) FROM events WHERE event_type='scout_open_failed'").fetchone()[0]
    assert failures == 0
    opened = [json.loads(r[0]) for r in logger._connect().execute(
        "SELECT payload_json FROM events WHERE event_type='scout_session_open'").fetchall()]
    assert opened and opened[0]["buy_ticket"] and opened[0]["sell_ticket"] and opened[0]["failed_attempts"] == 0


# ---- v3.4.0 acceptance: the four checks the decision card has to survive ------------------------------
def test_decision_trace_is_present_in_every_snapshot(tmp_path):
    """Every cycle — cold start, warm cycle, and the one written to SQLite — carries a full trace."""
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _SilverClient()
    engine = TradingEngine(client, cfg, logger)
    snapshots = [engine.run_cycle(client.tick.time + timedelta(seconds=5 * i)) for i in range(3)]
    engine.shutdown()

    required = {"headline", "verdict", "gates", "flips", "evidence", "levels", "footer", "go_meaning",
                "first_blocking", "passed_count", "gate_count", "state"}
    for snapshot in snapshots:
        trace = snapshot.analysis.get("decision_trace")
        assert trace and required.issubset(trace), sorted(required.difference(trace or {}))
        assert len(trace["gates"]) == len(GATE_ORDER)

    rows = logger._connect().execute("SELECT payload_json FROM analysis_snapshots ORDER BY id").fetchall()
    assert len(rows) == 3
    for row in rows:
        assert json.loads(row[0])["analysis"]["decision_trace"]["headline"]


def test_exactly_one_first_blocking_gate_is_marked():
    """Several gates would fail, but the router stops at the first — and so does the checklist."""
    value = _input(confluence=40, entry_state=EntryState.ABOVE,
                   trigger=TriggerResult(False, "NONE", reason="none"), rr=0.2,
                   target_realism=TargetRealism.UNLIKELY, spread_state=SpreadState.ABNORMAL)
    trace = explain_decision(value, final_decision_router(value, datetime.now(UTC)),
                             {"spread": 0.9, "max_spread_price": 0.6, "session": "LONDON", "remaining_minutes": 60})
    statuses = [g["status"] for g in trace["gates"]]
    assert statuses.count("fail") == 1
    assert trace["first_blocking"] == "spread" == trace["gates"][statuses.index("fail")]["name"]
    assert statuses[statuses.index("fail") + 1:] == ["skip"] * (len(statuses) - statuses.index("fail") - 1)
    assert all(g["icon"] == "❌" for g in trace["gates"] if g["status"] == "fail")

    card_gates = checklist_block({"analysis": {"decision_trace": trace}})
    assert card_gates.count("❌") == 1


def test_verdict_sentence_contains_the_score_and_the_blocking_reason():
    value = _input(confluence=49, entry_state=EntryState.APPROACHING)
    trace = explain_decision(value, final_decision_router(value, datetime.now(UTC)),
                             {"zone_text": "FVG 4396.25–4402.88", "zone_distance_atr": 0.8,
                              "session": "LONDON", "remaining_minutes": 60})
    verdict = trace["verdict"]
    assert "49/100" in verdict                                   # the score
    assert "confluence" in verdict and "55" in verdict           # the blocking gate and its threshold
    assert "FVG 4396.25–4402.88" in verdict                      # where price is relative to the zone
    assert len(verdict.split()) <= 30
    assert "49/100" in trace["headline"]


def test_a_placed_order_produces_the_green_card_with_all_take_profits(tmp_path):
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _SilverClient()
    engine = TradingEngine(client, cfg, logger)
    snapshot = engine.run_cycle(client.tick.time)

    plan = SimpleNamespace(side=Side.LONG, entry=4400.5, stop_loss=4396.0, sl_reason="below the FVG",
                           take_profits=[4405.0, 4410.0, 4415.0], actual_rr=[1.0, 2.11, 3.2], volume=0.02,
                           requested_entry=4400.5, target_realism=TargetRealism.REALISTIC,
                           invalidation="M5 close below 4396")
    zone = SimpleNamespace(kind="FVG", low=4398.0, high=4401.0)
    result = SimpleNamespace(ticket=501, retcode=10009, message="done", success=True)
    payload = engine._order_payload(result, plan, snapshot, zone, snapshot.trigger, "setup-1",
                                    snapshot.analysis.get("intermarket"))
    engine.shutdown()

    card = order_card("order", payload)
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["color"] == 0x1D9E75 and card["title"] == "🟢 LONG PLACED · #501 · 0.02 lot"
    for tp, rr in zip(plan.take_profits, plan.actual_rr):
        assert f"{tp:.2f} ({rr:.2f}R)" in fields["Verdict"]
    assert "SL 4396.00" in card["description"]
    assert fields["Management plan"].count("TP") >= 2

    trace = explain_decision(
        DecisionInput(Side.LONG, True, EntryState.CONFIRMED, snapshot.trigger, snapshot.scout, Freshness.LIVE,
                      SpreadState.NORMAL, True, 1.0, 1.0, TargetRealism.REALISTIC, 72, 55, "setup-1", 8, True),
        Decision(Action.LONG, "authorised", datetime.now(UTC)),
        {"order": payload, "session": "LONDON", "remaining_minutes": 60})
    assert trace["state"] == "ORDER_PLACED"
    assert trace["headline"] == "🟢 LONG PLACED · #501 · 0.02 lot · LONDON"
    assert "4400.50" in trace["verdict"] and "4396.00" in trace["verdict"]
