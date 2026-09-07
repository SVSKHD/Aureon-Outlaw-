"""v3.4.0 — the decision card: one structure that answers "go or not" in a glance.

`DecisionExplanation` is built once per cycle in decision_router, stored on the snapshot as
`analysis.decision_trace`, and rendered as THE status card by the webhook, `!status` and `!why`.
"""
from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import FakeClient
from test_v33_broker_clock import ASIA_NOW, _BrokerBarClient, _CycleLogger
from xau_mt5_bot.cards import decision_card, detections_card, order_card, snapshot_dict
from xau_mt5_bot.decision_router import (
    GATE_LABELS,
    GATE_ORDER,
    DecisionExplanation,
    explain_decision,
    final_decision_router,
)
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.models import (
    Action,
    Decision,
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
from xau_mt5_bot.mt5_client import Tick

ROOT = Path(__file__).resolve().parents[1]
GREEN, RED, GREY, AMBER = 0x1D9E75, 0xD85A30, 0x8A8A8A, 0xEF9F27


def _input(**over) -> DecisionInput:
    scout = ScoutSnapshot(SessionName.ASIA)
    scout.market_speed = over.pop("market_speed", "NORMAL")
    scout.leader = over.pop("leader", "BUY")
    scout.strength = over.pop("strength", 4)
    scout.verdict = over.pop("verdict", ScoutVerdict.NEUTRAL)
    trigger = TriggerResult(over.pop("confirmed", True), over.pop("source", "M1_ENGULFING"))
    trigger.fresh = over.pop("fresh", True)
    trigger.consumed = over.pop("consumed", False)
    base = dict(pa_side=Side.LONG, setup_valid=True, entry_state=EntryState.INSIDE, trigger=trigger, scout=scout,
                freshness=Freshness.LIVE, spread_state=SpreadState.NORMAL, account_safe=True, rr=2.0, min_rr=1.0,
                target_realism=TargetRealism.REALISTIC, confluence=72, min_confluence=55, setup_id=None,
                scout_contradiction_threshold=8, hold_when_slow=False)
    base.update(over)
    return DecisionInput(**base)


def _ctx(**over) -> dict:
    ctx = {
        "session": "ASIA", "time_label": "05:00", "price": 2500.10, "bid": 2500.0, "ask": 2500.2, "atr": 2.0,
        "spread": 0.2, "max_spread": 0.6, "m1_age_seconds": 12, "max_m1_age_seconds": 300,
        "clock_ok": True, "clock_residual_seconds": 0.4, "max_clock_skew_seconds": 600,
        "is_demo": True, "is_hedging": True, "side_gap": 14, "min_side_gap": 8,
        "long_score": 72, "short_score": 58,
        "zone": {"low": 2499.0, "high": 2502.0, "kind": "DEMAND_OB"}, "zone_distance_atr": 0.0,
        "families": {"long": {"htf": 14, "structure": 10, "liquidity": 8}, "short": {"structure": 5}},
        "caps": {"htf": 20, "structure": 20, "liquidity": 20},
        "penalties": ["counter-trend vs D1/H4 −10"], "bonuses": ["discount location +5"],
        "pace_price_per_min": 0.08, "slow_threshold": 0.03,
        "remaining_minutes": 180, "minimum_remaining_minutes": 30,
        "session_target_verdict": "ACHIEVABLE", "session_target_move": 10,
        "plan": {"stop_loss": 2495.0, "take_profits": [2506.0, 2512.0], "actual_rr": [1.2, 2.4]},
        "go_today": "NO-GO", "go_cycles": 0, "cycles_observed": 42, "calibration_status": "COLLECTING",
    }
    ctx.update(over)
    return ctx


def _explain(value: DecisionInput, ctx: dict) -> DecisionExplanation:
    return explain_decision(value, final_decision_router(value, ASIA_NOW), ctx)


# --- required: decision_trace present in every snapshot ---------------------------------------------------------------
def test_decision_trace_is_on_every_snapshot(config):
    config.project_dir = tempfile.mkdtemp()
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    engine = TradingEngine(client, config, _CycleLogger())
    first = engine.run_cycle(ASIA_NOW)
    second = engine.run_cycle(ASIA_NOW + timedelta(seconds=5))
    engine.shutdown()

    for snapshot in (first, second):
        trace = snapshot.analysis["decision_trace"]
        assert set(trace) >= {"verdict", "colour", "title", "sentence", "gates", "flips",
                              "evidence_for", "evidence_against", "levels", "footer", "blocking_gate"}
        assert [g["gate"] for g in trace["gates"]] == list(GATE_ORDER)      # every gate, in evaluation order
        assert trace["verdict"] in {"PLACED", "GO", "WAIT", "NO_TRADE", "CLOSED"}
        assert trace["colour"] in {"green", "amber", "red", "grey"}
        # and it survives the trip through the snapshot → dict path the cards use
        assert snapshot_dict(snapshot)["analysis"]["decision_trace"]["title"] == trace["title"]


# --- required: exactly one first-blocking gate marked -------------------------------------------------------------------
def test_exactly_one_gate_is_marked_blocking():
    explanation = _explain(_input(confirmed=False, fresh=False), _ctx())
    blocking = [g for g in explanation.gates if g.blocking]
    assert len(blocking) == 1 and blocking[0].key == "trigger"
    assert explanation.to_dict()["blocking_gate"] == "trigger"

    states = [g.state for g in explanation.gates]
    index = [g.key for g in explanation.gates].index("trigger")
    assert set(states[:index]) == {"pass"}                                 # everything before it passed
    assert states[index] == "fail"
    assert set(states[index + 1:]) == {"skipped"}                          # nothing after it was evaluated
    assert [g.mark for g in explanation.gates][index] == "❌"
    assert explanation.gates[index + 1].mark == "—"


def test_every_gate_can_be_the_blocking_one():
    """Each gate must be reachable as the first failure, or the checklist has an unreportable state."""
    cases = {
        "data": (_input(freshness=Freshness.STALE), {}),
        "clock_account": (_input(account_safe=False), {}),
        "spread": (_input(spread_state=SpreadState.ABNORMAL), {}),
        "confluence": (_input(confluence=40), {}),
        "side_gap": (_input(setup_valid=False), {}),
        "zone": (_input(entry_state=EntryState.APPROACHING), {}),
        "trigger": (_input(confirmed=False), {}),
        "pace": (_input(market_speed="SLOW", hold_when_slow=True), {}),
        "scouts": (_input(verdict=ScoutVerdict.CONTRADICTS, strength=9), {}),
        "rr": (_input(rr=0.5), {}),
        "target": (_input(target_realism=TargetRealism.UNLIKELY), {}),
        "session_target": (_input(), {"session_target_unlikely": True}),
        "risk_locks": (_input(), {"day_lock": True, "risk_locks_detail": "daily max loss -120.00"}),
        "session_time": (_input(), {"session_time_short": True}),
    }
    assert set(cases) == set(GATE_ORDER)
    for key, (value, extra) in cases.items():
        explanation = _explain(value, _ctx(**extra))
        blocking = [g for g in explanation.gates if g.blocking]
        assert len(blocking) == 1 and blocking[0].key == key, f"{key} -> {[g.key for g in blocking]}"
        assert blocking[0].label == GATE_LABELS[key]


def test_all_gates_pass_leaves_no_blocking_gate():
    explanation = _explain(_input(), _ctx())
    assert [g.state for g in explanation.gates] == ["pass"] * len(GATE_ORDER)
    assert explanation.blocking_gate is None
    assert explanation.flips == []
    # the router authorised but nothing was sent: still a GO, never a red NO TRADE
    assert explanation.verdict == "GO" and explanation.colour == "green"
    assert "every gate clear, no order sent" in explanation.title
    assert "Every gate is clear" in explanation.sentence

    placed = _explain(_input(), _ctx(order_ticket=771, order_volume=0.01))
    assert placed.verdict == "PLACED" and placed.colour == "green"


# --- required: verdict sentence contains the score and the blocking reason ----------------------------------------------
def test_verdict_sentence_carries_the_score_and_the_blocker():
    explanation = _explain(_input(confluence=49, confirmed=False), _ctx(long_score=49, short_score=30))
    sentence = explanation.sentence
    assert "49/100" in sentence                                            # the score
    assert "confluence" in sentence.lower()                                # the blocking reason
    assert "55" in sentence                                                # the threshold it missed
    assert "2500.10" in sentence                                           # where price is
    assert len(sentence.split()) <= 30, sentence
    assert sentence.endswith(".")


def test_verdict_sentence_is_clipped_to_thirty_words():
    long_detail = " ".join(f"word{i}" for i in range(60))
    explanation = _explain(_input(account_safe=False), _ctx(account_reason=long_detail))
    assert len(explanation.sentence.split()) <= 30


def test_titles_match_the_three_verdicts():
    waiting = _explain(_input(confirmed=False, fresh=False), _ctx())
    assert waiting.title.startswith("🟠 WAIT · LONG bias 72/100 · inside zone, trigger pending")
    assert waiting.colour == "amber" and "ASIA" in waiting.title and "05:00" in waiting.title

    refused = _explain(_input(freshness=Freshness.STALE, confluence=49), _ctx())
    assert refused.title.startswith("🔴 NO TRADE · LONG bias 49/100")
    assert refused.colour == "red"

    placed = _explain(_input(), _ctx(order_ticket=771, order_volume=0.01, order_entry=2500.5, risk_currency=3.5))
    assert placed.title.startswith("🟢 LONG PLACED · #771 · 0.01 lot")
    assert placed.colour == "green" and placed.verdict == "PLACED"

    shut = _explain(_input(), _ctx(session="CLOSED"))
    assert shut.title.startswith("⚪ CLOSED") and shut.colour == "grey"


# --- what flips it, evidence, levels, footer ------------------------------------------------------------------------------
def test_what_flips_it_is_concrete_and_numbered():
    explanation = _explain(_input(confluence=47), _ctx(confluence_gap=8, best_missing_family="liquidity",
                                                       best_missing_headroom=12))
    assert 1 <= len(explanation.flips) <= 3
    assert "8 more points" in explanation.flips[0] and "55" in explanation.flips[0]
    assert "liquidity" in explanation.flips[1] and "12" in explanation.flips[1]

    zoned = _explain(_input(entry_state=EntryState.APPROACHING), _ctx(zone_distance_atr=1.4))
    assert "2499.00–2502.00" in zoned.flips[0] and "DEMAND_OB" in zoned.flips[0]
    assert "1.40 ATR" in zoned.flips[1]

    spread = _explain(_input(spread_state=SpreadState.ABNORMAL), _ctx(spread=0.95))
    assert "0.60" in spread.flips[0] and "0.95" in spread.flips[0]


def test_evidence_splits_for_and_against():
    explanation = _explain(_input(), _ctx())
    assert explanation.evidence_for[0] == "higher timeframes +14"          # biggest family first
    assert "discount location +5" in explanation.evidence_for
    assert "counter-trend vs D1/H4 −10" in explanation.evidence_against
    assert "opposing structure +5" in explanation.evidence_against


def test_levels_and_footer():
    explanation = _explain(_input(confirmed=False), _ctx())
    levels = explanation.levels
    assert levels["price"] == 2500.10 and levels["zone_kind"] == "DEMAND_OB"
    assert levels["stop_loss"] == 2495.0 and levels["take_profit_1"] == 2506.0
    assert levels["take_profit_1_rr"] == 1.2

    footer = explanation.footer
    assert footer["go_today"] == "NO-GO" and footer["cycles_observed"] == 42
    assert "!why" in footer["hint"]
    # only the gates AFTER the zone can still be pending once price arrives
    assert "Confluence" not in footer["remaining_vetoes"]
    assert "Trigger" in footer["remaining_vetoes"]


# --- rendering ------------------------------------------------------------------------------------------------------------
def test_decision_card_renders_the_whole_trace():
    explanation = _explain(_input(confirmed=False, fresh=False), _ctx())
    card = decision_card({"analysis": {"decision_trace": explanation.to_dict()}}, "UTC")
    assert card["color"] == AMBER and card["title"] == explanation.title
    assert card["description"] == explanation.sentence
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert set(fields) == {"Gates", "What flips it", "Evidence FOR", "Evidence AGAINST", "Levels"}
    assert fields["Gates"].count("\n") == len(GATE_ORDER) - 1              # one row per gate
    assert "❌ Trigger" in fields["Gates"] and "✅ Data fresh" in fields["Gates"]
    assert fields["What flips it"].startswith("1. ")
    assert "2499.00–2502.00 DEMAND_OB" in fields["Levels"] and "TP1 2506.00 (1.20R)" in fields["Levels"]
    assert "GO today: NO-GO" in card["footer"]["text"] and "!why" in card["footer"]["text"]


def test_status_card_is_the_decision_card_and_falls_back_without_a_trace():
    from xau_mt5_bot.cards import status_card
    explanation = _explain(_input(), _ctx(order_ticket=771, order_volume=0.01))
    payload = {"analysis": {"decision_trace": explanation.to_dict()}}
    assert status_card(payload, "UTC") == decision_card(payload, "UTC")
    assert status_card(payload, "UTC")["color"] == GREEN

    legacy = status_card({"go_status": "NO-GO", "decision": {"action": "WAIT", "reason": "no trigger"},
                          "confluence": 40, "analysis": {"blocked_by": []}}, "UTC")
    assert legacy["title"].startswith("🟠 NO-GO · WAIT")                   # pre-v3.4.0 snapshots still render


def test_placed_order_card_carries_every_tp_and_the_gates_it_cleared():
    card = order_card("order", {
        "side": "LONG", "success": True, "ticket": 771, "volume": 0.01, "entry": 2500.5, "stop_loss": 2497.0,
        "take_profits": [2504.0, 2508.0, 2515.0], "actual_rr": [1.0, 2.1, 4.1], "risk_currency": 3.5,
        "m1_age_seconds": 8, "freshness": "LIVE", "spread": 0.2, "max_spread": 0.6,
        "confluence": 72, "min_confluence": 55, "trigger_reason": "M1 engulfing reclaim",
        "rr": 1.0, "min_rr": 1.0, "target_realism": "REALISTIC", "invalidation": "M5 close below 2496",
        "breakeven_at_rr": 1.0, "trailing_start_rr": 1.5, "partial_tp1_percent": 50, "partial_tp2_percent": 25,
        "silver": "COUPLED r=0.91 · SMT NONE"})
    assert card["color"] == GREEN
    assert card["title"] == "🟢 LONG PLACED · #771 · 0.01 lot"
    assert "2500.50" in card["description"] and "3.50" in card["description"] and "2497.00" in card["description"]
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert fields["Take profits"] == "TP1 2504.00 (1.00R) · TP2 2508.00 (2.10R) · TP3 2515.00 (4.10R)"
    assert fields["Gates cleared"].count("✅") == 5
    assert "72 / 55" in fields["Gates cleared"] and "1.00 / 1.00" in fields["Gates cleared"]
    assert "break-even at 1.0R" in fields["Management plan"]
    assert "M5 close below 2496" in fields["Management plan"]
    assert "COUPLED" in card["footer"]["text"]


def test_detections_card_is_compact_with_a_full_variant():
    snap = {"session": "ASIA", "timestamp": "2026-09-07T05:00:00+00:00", "bid": 4410.0, "pa_side": "LONG",
            "confluence": 62, "analysis": {"atr": 2.0},
            "structures": {"D1": {"state": "BULLISH", "events": []}, "H4": {"state": "BULLISH", "events": []},
                           "H1": {"state": "BEARISH", "events": []}, "M15": {"state": "BULLISH", "events": []},
                           "M5": {"state": "BULLISH", "events": [{"event": "BOS", "level": 4405.0,
                                                                  "timestamp": "2026-09-07T04:50:00+00:00"}]}},
            "patterns": [{"name": f"P{i}", "timestamp": "2026-09-07T04:5%d:00+00:00" % (i % 10)} for i in range(9)],
            "sweeps": [{"level_type": "PDL", "level_price": 4380.0, "sweep_price": 4379.0, "direction": "BULLISH",
                        "age_bars": 2, "active": True},
                       {"level_type": "PDH", "level_price": 4420.0, "sweep_price": 4421.0, "direction": "BEARISH",
                        "age_bars": 1, "active": True}],
            "zones": [{"side": "LONG", "kind": "DEMAND_OB", "low": 4400.0, "high": 4404.0, "score": 8.0,
                       "status": "FRESH"}]}
    compact = detections_card(snap, "UTC")
    assert compact["description"] == "D1 ▲ H4 ▲ H1 ▼ M15 ▲ M5 ▲"
    fields = {f["name"]: f["value"] for f in compact["fields"]}
    assert set(fields) == {"Newest patterns", "Sweeps on the LONG side", "Zones (distance in ATR)"}
    assert len(fields["Newest patterns"].split("\n")) == 3                 # 3 newest only
    assert "PDL" in fields["Sweeps on the LONG side"]
    assert "PDH" not in fields["Sweeps on the LONG side"]                  # opposing side dropped
    assert "ATR away" in fields["Zones (distance in ATR)"]
    assert "!detected full" in compact["footer"]["text"]

    full = detections_card(snap, "UTC", full=True)
    assert "full" in full["title"]
    assert set(f["name"] for f in full["fields"]) == {
        "Candle / chart patterns", "Structure events (2 newest / TF)",
        "Active sweeps (6 newest)", "Zones (best first, distance in ATR)"}
    assert len(full["fields"][0]["value"].split("\n")) == 8                # the long form is back


def test_commands_render_the_trace(tmp_path: Path):
    import json
    import shutil
    import sqlite3
    from xau_mt5_bot.config import load_config
    from xau_mt5_bot.discord_bot import BotState, dispatch
    from xau_mt5_bot.logger import AuditLogger

    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    cfg = load_config(tmp_path / "config.yaml")
    AuditLogger(cfg.logging.sqlite_path, cfg.logging.jsonl_path)
    trace = _explain(_input(confirmed=False, fresh=False), _ctx()).to_dict()
    payload = {"timestamp": "2026-09-07T05:00:00+00:00", "session": "ASIA", "go_status": "NO-GO",
               "confluence": 72, "pa_side": "LONG", "decision": {"action": "WAIT", "reason": "no trigger"},
               "analysis": {"decision_trace": trace}}
    with sqlite3.connect(cfg.logging.sqlite_path) as con:
        con.execute("INSERT INTO analysis_snapshots(timestamp,symbol,session,action,payload_json) VALUES(?,?,?,?,?)",
                    (payload["timestamp"], "XAUUSD", "ASIA", "WAIT", json.dumps(payload)))
    state = BotState(tmp_path)

    card = dispatch(state, "!status")
    assert isinstance(card, dict) and card["title"] == trace["title"]

    why = dispatch(state, "!why")
    assert trace["title"] in why and "❌ **Trigger**" in why
    assert "**What flips it**" in why and why.rstrip().endswith(trace["flips"][0])
    for gate in GATE_ORDER:
        assert GATE_LABELS[gate] in why                                    # every gate is listed, not just the blocker


def test_webhook_pushes_when_the_blocking_gate_changes(monkeypatch):
    """The card is the status push: a new blocking gate is a new card, even at the same action."""
    from xau_mt5_bot.notify import Discord

    sent: list = []
    discord = Discord("NOPE", 300, 0, status_mode="changes")
    discord.url = "https://example.invalid/hook"
    monkeypatch.setattr(discord, "send", lambda text="", embed=None: sent.append(embed or text) or True)

    class _Snap:
        class decision:
            action = type("A", (), {"value": "WAIT"})()
        entry_state = type("E", (), {"value": "INSIDE"})()
        pa_side = "LONG"
        session = type("S", (), {"value": "ASIA"})()
        go_status = "NO-GO"

    def _with(gate: str) -> dict:
        return {"analysis": {"decision_trace": {"verdict": "WAIT", "blocking_gate": gate,
                                                "title": f"🟠 WAIT · {gate}", "sentence": "s",
                                                "gates": [], "flips": [], "evidence_for": [], "evidence_against": [],
                                                "levels": {}, "footer": {}, "colour": "amber"}}}

    monkeypatch.setattr("xau_mt5_bot.notify.snapshot_dict", lambda s: _with("trigger"))
    discord.on_snapshot(_Snap(), "UTC")
    discord.on_snapshot(_Snap(), "UTC")
    assert len(sent) == 1                                                  # same gate, same card, no repeat

    monkeypatch.setattr("xau_mt5_bot.notify.snapshot_dict", lambda s: _with("rr"))
    discord.on_snapshot(_Snap(), "UTC")
    assert len(sent) == 2 and sent[-1]["title"] == "🟠 WAIT · rr"          # gate changed → new card
