"""v3.3.0 — the decision has to be readable, and every card has to tell the whole story."""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from test_v18_features import _BarClient
from test_v31_discord_bot import _SilverClient, _project
from xau_mt5_bot.cards import (
    _sweep_lines,
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


# ---- the gate table ------------------------------------------------------------------------------------
def test_every_router_gate_is_reported_in_order():
    value = _input()
    trace = explain_decision(value, final_decision_router(value, datetime.now(UTC)), {})
    assert [g["name"] for g in trace["gates"]] == list(GATE_ORDER)
    assert trace["action"] == "LONG" and not trace["blocked_by"]
    assert trace["passed_count"] == trace["gate_count"] == len(GATE_ORDER)
    assert trace["verdict"].startswith("LONG authorised")
    assert "signal" in trace["go_meaning"]


def test_a_no_go_names_every_active_veto_and_what_would_flip_it():
    value = _input(confluence=46, entry_state=EntryState.APPROACHING,
                   trigger=TriggerResult(False, "NONE", reason="no confirmation yet"), rr=None,
                   target_realism=TargetRealism.UNLIKELY)
    decision = final_decision_router(value, datetime.now(UTC))
    trace = explain_decision(value, decision, {"zone_text": "FVG 4396.25–4402.88", "session_target_verdict": "UNLIKELY",
                                               "spread": 0.2, "max_spread_price": 0.6, "side_gap": 12})
    blocked = [g["name"] for g in trace["blocked_by"]]
    assert blocked == ["confluence", "inside zone", "trigger fresh", "RR", "target realism", "$ session feasibility"]
    assert blocked == [name for name in GATE_ORDER if name in blocked]           # router order preserved
    assert "9 more confluence points" in trace["next"]
    assert "FVG 4396.25–4402.88" in " ".join(trace["next"])
    assert trace["verdict"].startswith("NO TRADE — LONG bias 46/100; blocked by confluence")


def test_the_clock_gate_states_the_offset_it_already_applied():
    value = _input(account_safe=False)
    trace = explain_decision(value, final_decision_router(value, datetime.now(UTC)),
                             {"clock_ok": False, "residual_skew_seconds": 1500, "max_clock_skew_seconds": 600,
                              "broker_utc_offset_hours": 3.0, "account_reason": "broker clock skew 1500s"})
    clock = next(g for g in trace["gates"] if g["name"] == "clock")
    assert not clock["passed"] and clock["value"] == "1500s residual" and clock["threshold"] == "≤ 600s"
    assert "UTC+3h is applied before this check" in clock["note"]
    assert any("PC clock" in item for item in trace["next"])


def test_gates_after_a_missing_setup_are_reported_as_not_reached():
    value = _input(pa_side=None, setup_valid=False, entry_state=EntryState.NOT_IN_SETUP)
    trace = explain_decision(value, final_decision_router(value, datetime.now(UTC)), {"side_gap": 3, "min_side_gap": 8})
    by_name = {g["name"]: g for g in trace["gates"]}
    assert by_name["inside zone"]["value"] == "not reached" and by_name["trigger fresh"]["value"] == "not reached"
    assert not by_name["setup exists"]["passed"]
    assert not by_name["PA side gap"]["passed"] and by_name["PA side gap"]["value"] == 3


def test_evidence_lists_the_three_strongest_families_per_side():
    value = _input()
    detail = {"long_families": {"htf": 18, "structure": 12, "liquidity": 8, "candle": 2, "momentum": 0},
              "short_families": {"htf": 0, "structure": 4}, "long_score": 40, "short_score": 4}
    trace = explain_decision(value, final_decision_router(value, datetime.now(UTC)),
                             {"direction_detail": detail, "intermarket": {"status": "OK", "regime": "COUPLED",
                                                                          "correlation": 0.93, "smt": "NONE",
                                                                          "long_points": 3, "short_points": 0}})
    evidence = trace["evidence"]
    assert [item["family"] for item in evidence["long"]] == ["htf", "structure", "liquidity"]
    assert evidence["long"][0]["label"] == "higher-timeframe trend" and evidence["long"][0]["points"] == 18
    assert len(evidence["short"]) == 1 and evidence["long_score"] == 40
    assert "COUPLED" in evidence["silver"] and "+3L" in evidence["silver"]


def test_a_real_cycle_carries_the_trace_into_the_snapshot_report_and_sqlite(tmp_path):
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _SilverClient()
    engine = TradingEngine(client, cfg, logger)
    snapshot = engine.run_cycle(client.tick.time)
    engine.shutdown()

    trace = snapshot.analysis["decision_trace"]
    assert [g["name"] for g in trace["gates"]] == list(GATE_ORDER)
    assert trace["action"] == snapshot.decision.action.value
    assert snapshot.analysis["blocked_by"] == trace["blocked_by"]
    assert snapshot.analysis["broker_clock"]["broker_utc_offset_hours"] == 0.0

    report = format_report(snapshot, "UTC")
    assert "DECISION TRACE" in report and trace["verdict"] in report
    if trace["blocked_by"]:
        assert "Blocked by:" in report

    stored = json.loads(logger._connect().execute(
        "SELECT payload_json FROM analysis_snapshots ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert stored["analysis"]["decision_trace"]["verdict"] == trace["verdict"]


# ---- !why / !clock -------------------------------------------------------------------------------------
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
    assert all(name in why for name in ("data fresh", "clock", "RR", "day lock"))
    assert "Silver:" in why

    clock = dispatch(state, "!clock")
    assert clock == fmt_clock(state)
    assert "System UTC:" in clock and "Broker time:" in clock
    assert "Detected offset: UTC+0h" in clock and "Residual skew:" in clock
    assert "orders allowed" in clock

    assert "`!why`" in HELP and "`!clock`" in HELP
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


# ---- status card ---------------------------------------------------------------------------------------
def test_status_card_shows_verdict_blocked_by_and_next():
    snapshot = {"go_status": "NO-GO", "decision": {"action": "WAIT", "reason": "no fresh confirmation"},
                "confluence": 46, "pa_side": "LONG", "session": "ASIA", "bid": 4400.0, "ask": 4400.2, "spread": 0.2,
                "analysis": {"decision_trace": {"verdict": "WAIT — LONG bias 46/100; blocked by confluence (46)",
                                                "blocked_by": [{"name": "confluence", "value": 46, "threshold": 55},
                                                               {"name": "trigger fresh", "value": "waiting", "threshold": ""}],
                                                "next": ["9 more confluence points", "an M1 sweep+BOS"],
                                                "passed_count": 15, "gate_count": 17,
                                                "gates": [{"name": "clock", "passed": True}],
                                                "go_meaning": "GO = signal, not an order."},
                             "broker_clock": {"broker_utc_offset_hours": 3.0, "broker_clock_source": "auto",
                                              "residual_skew_seconds": 0.4}}}
    card = status_card(snapshot, "UTC")
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert fields["Verdict"].startswith("WAIT — LONG bias 46/100")
    assert "confluence — 46 (needs 55)" in fields["Blocked by"] and "trigger fresh" in fields["Blocked by"]
    assert "9 more confluence points" in fields["Next"]
    assert fields["Broker clock"] == "UTC+3h (auto) · residual 0s"
    assert "signal" in fields["What GO means"]
    assert "15/17 gates passed" in card["footer"]["text"]


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


# ---- order cards ---------------------------------------------------------------------------------------
def test_order_card_carries_the_whole_trade():
    payload = {"ticket": 501, "side": "LONG", "session": "LONDON", "entry": 4400.5, "stop_loss": 4396.0,
               "sl_reason": "below the FVG", "take_profits": [4405.0, 4410.0], "actual_rr": [1.0, 2.11],
               "volume": 0.02, "risk_price": 4.5, "risk_currency": 9.0, "zone_kind": "FVG", "zone": "4398.00–4401.00",
               "trigger_reason": "M1 sweep + BOS", "trigger_source": "M1", "confluence": 72,
               "scout_verdict": "CONFIRMS", "scout_leader": "BUY", "scout_strength": 6,
               "silver": "COUPLED r=0.93 · SMT NONE", "invalidation": "M5 close below 4396"}
    card = order_card("order", payload)
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["title"] == "📥 ORDER PLACED · LONG" and "#501" in card["description"]
    assert fields["Take profits"] == "4405.00 (1.00R) / 4410.00 (2.11R)"
    assert "9.00 at 0.02 lot" in fields["Risk"]
    assert "FVG 4398.00–4401.00" == fields["Zone"]
    assert "M1 sweep + BOS" in fields["Trigger"]
    assert "72/100" in fields["Confluence"] and "CONFIRMS 6/10" in fields["Confluence"]
    assert "COUPLED" in fields["Silver"]


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


def test_detected_card_dedupes_collapses_round_levels_and_caps_at_six():
    snapshot = {"session": "ASIA", "timestamp": "2026-09-07T01:00:00+00:00", "bid": 4410.0, "ask": 4410.2,
                "sweeps": [_sweep("ROUND_1", 4407.0, 1), _sweep("ROUND_1", 4408.0, 2), _sweep("ROUND_1", 4409.0, 3),
                           _sweep("ROUND_1", 4409.0, 9),                       # duplicate level_type+price
                           _sweep("PDH", 4420.0, 4), _sweep("PDL", 4390.0, 5), _sweep("ASIA_HIGH", 4415.0, 6),
                           _sweep("ASIA_LOW", 4405.0, 7), _sweep("EQUAL_HIGHS", 4418.0, 8),
                           _sweep("SWING_HIGH", 4419.0, 10)],
                "zones": [{"side": "LONG", "kind": "FVG", "low": 4400.0, "high": 4402.0, "score": 7, "status": "fresh"}],
                "analysis": {"atr": 2.0}, "patterns": [], "structures": {}}
    lines = _sweep_lines(snapshot, 6)
    assert len(lines) == 6
    assert lines[0] == "ROUND_1 ×3 (4407–4409)"                                  # newest group first (ages 1-3), collapsed
    assert sum(1 for line in lines if "ROUND_1" in line) == 1
    assert lines[1].startswith("▲ PDH") and "4 bars" in lines[1]                 # then the rest, oldest last
    assert "SWING_HIGH" not in " ".join(lines)                                   # the 10-bar sweep falls off the six

    card = detections_card(snapshot, "UTC")
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert "ROUND_1 ×3" in fields["Active sweeps"]
    assert "4.5 ATR away" in fields["Zones (best first)"]                        # 4410 bid vs 4401 mid over ATR 2.0


def test_detected_card_keeps_two_structure_events_per_timeframe():
    events = [{"event": f"Bullish BOS {i}", "level": 4400 + i, "timestamp": "2026-09-07T01:00:00+00:00"} for i in range(5)]
    snapshot = {"session": "ASIA", "timestamp": "2026-09-07T01:00:00+00:00", "sweeps": [], "zones": [], "patterns": [],
                "structures": {tf: {"events": events} for tf in ("H4", "H1", "M15", "M5")}}
    card = detections_card(snapshot, "UTC")
    lines = next(f["value"] for f in card["fields"] if f["name"] == "Structure events").split("\n")
    assert len(lines) == 8                                                       # 2 per timeframe × 4 timeframes
    assert lines[0].startswith("H4 Bullish BOS 3") and lines[1].startswith("H4 Bullish BOS 4")


# ---- the live evidence, end to end ---------------------------------------------------------------------
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
