from __future__ import annotations

import json
import shutil
import time
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from test_v18_features import _BarClient
from xau_mt5_bot.config import load_config
from xau_mt5_bot.discord_bot import HELP, BotState, allowed_user_ids, authorised, chunk, dispatch
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.history import reset_history_cache
from xau_mt5_bot.logger import AuditLogger

ROOT = Path(__file__).resolve().parents[1]


class _SilverClient(_BarClient):
    """Gold + a correlated silver series from the same generator, so the engine's intermarket path runs for real."""

    GOLD_COUNTS = {"M1": 10000, "M5": 30000, "M15": 8000, "H1": 3000, "H4": 1200, "D1": 730}

    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if symbol != "XAGUSD":
            return super().get_bars(symbol, timeframe, count)
        frame = super().get_bars("XAUUSD", timeframe, self.GOLD_COUNTS[timeframe]).tail(count).reset_index(drop=True).copy()
        for col in ("open", "high", "low", "close"):
            frame[col] = 40.0 + (frame[col] - 2500.0) * 0.01
        return frame


class _FailingSilverClient(_BarClient):
    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        if symbol == "XAGUSD":
            raise RuntimeError("Symbol not found: XAGUSD")
        return super().get_bars(symbol, timeframe, count)


def _project(tmp_path: Path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    cfg = load_config(tmp_path / "config.yaml")
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    return cfg, AuditLogger(cfg.logging.sqlite_path, cfg.logging.jsonl_path)


def test_engine_reports_silver_evidence_and_scores_it(tmp_path):
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _SilverClient(); engine = TradingEngine(client, cfg, logger)
    snap = engine.run_cycle(client.tick.time); engine.shutdown()
    im = snap.analysis["intermarket"]
    assert im["status"] == "OK" and im["symbol"] == "XAGUSD" and im["regime"] == "COUPLED" and im["correlation"] > 0.9
    assert im["long_points"] <= cfg.intermarket.max_weight and im["short_points"] <= cfg.intermarket.max_weight
    stored = json.loads(logger._connect().execute("SELECT payload_json FROM analysis_snapshots ORDER BY id DESC LIMIT 1").fetchone()[0])
    assert stored["analysis"]["intermarket"]["regime"] == "COUPLED"


def test_engine_degrades_to_unavailable_when_silver_missing(tmp_path):
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _FailingSilverClient(); engine = TradingEngine(client, cfg, logger)
    snap = engine.run_cycle(client.tick.time)
    snap2 = engine.run_cycle(client.tick.time + timedelta(seconds=5)); engine.shutdown()
    assert snap.analysis["intermarket"]["status"] == "UNAVAILABLE" and "XAGUSD" in snap.analysis["intermarket"]["reason"]
    assert snap2.decision is not None
    events = [r[0] for r in logger._connect().execute("SELECT event_type FROM events").fetchall()]
    assert events.count("intermarket_unavailable") == 1                       # reported once, cycle continues


def test_intermarket_disabled_yields_unavailable_without_fetch(tmp_path):
    reset_history_cache()
    cfg, logger = _project(tmp_path); cfg.intermarket.enabled = False
    client = _FailingSilverClient(); engine = TradingEngine(client, cfg, logger)
    snap = engine.run_cycle(client.tick.time); engine.shutdown()
    assert snap.analysis["intermarket"]["status"] == "UNAVAILABLE"
    assert not [r for r in logger._connect().execute("SELECT event_type FROM events WHERE event_type='intermarket_unavailable'").fetchall()]


def test_discord_commands_answer_from_bot_files(tmp_path):
    reset_history_cache()
    cfg, logger = _project(tmp_path)
    client = _SilverClient(); engine = TradingEngine(client, cfg, logger)
    engine.run_cycle(client.tick.time)
    (tmp_path / "data" / "heartbeat.json").write_text(json.dumps({"ts_epoch": time.time(), "pid": 1, "uptime_s": 120, "cycles": 3,
                                                                  "cycle_ms_avg": 800.0, "cycle_ms_max": 1200.0, "errors": 0, "last_errors": [],
                                                                  "last": {"action": "WAIT"}}))
    engine.shutdown()
    state = BotState(tmp_path)
    status = dispatch(state, "!status")
    assert isinstance(status, dict) and status["title"].split(" · ")[1] in {"LONG", "SHORT", "WAIT", "NO TRADE"} and "COUPLED" in status["fields"][2]["value"]
    text = dispatch(state, "!text")
    assert text.startswith("**[") and "Silver: COUPLED" in text
    det = dispatch(state, "!detected")
    assert isinstance(det, dict) and det["title"].startswith("🔍") and len(det["fields"]) == 4
    assert "XAU/XAGUSD" in dispatch(state, "!silver") and "COUPLED" in dispatch(state, "!silver")
    assert "GO tallies" in dispatch(state, "!go")
    assert "realised P/L" in dispatch(state, "!day")
    assert "Heartbeat OK" in dispatch(state, "!heartbeat")
    assert dispatch(state, "!scouts")                                                  # pair opened on demo hedging FakeClient
    assert dispatch(state, "!positions").startswith("No active PA position")
    assert dispatch(state, "!trades 3") == "No trades recorded."
    assert dispatch(state, "!events 2").startswith("**Last")
    assert dispatch(state, "!help") == HELP
    assert dispatch(state, "hello") is None and dispatch(state, "!nothing") is None
    assert "not implemented by design" in dispatch(state, "!buy 1 lot")


def test_discord_commands_without_bot_started(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    state = BotState(tmp_path)
    assert "No analysis snapshot" in dispatch(state, "!status")
    assert "No heartbeat" in dispatch(state, "!heartbeat")
    assert dispatch(state, "!day") == "No daily state yet."


def test_chunk_respects_discord_limit():
    text = "\n".join("x" * 100 for _ in range(50))
    parts = chunk(text, 1900)
    assert all(len(p) <= 1900 for p in parts) and "\n".join(parts) == text


def test_user_and_channel_authorisation():
    users = allowed_user_ids("783929361979277312, 1")
    assert users == {"783929361979277312", "1"}
    assert authorised("c1", "783929361979277312", "c1", users)
    assert not authorised("c2", "783929361979277312", "c1", users)
    assert not authorised("c1", "999", "c1", users)
    assert authorised("any", "any", "", set())


def test_quiet_discord_defaults_suppress_status_and_noise(monkeypatch):
    from xau_mt5_bot.notify import Discord
    d = Discord("NOPE", 0, 0); d.url = "https://example.invalid/hook"; sent = []
    monkeypatch.setattr(d, "send", lambda text="", embed=None: sent.append(embed or text) or True)
    assert d.status_mode == "events" and d.event_level == "trade"
    assert d.event("cycle_slow", {}) is False and d.event("mt5_validated", {}) is False and d.event("clock_check", {}) is False
    assert d.event("order", {"side": "LONG"}) is True and d.event("trade_closed", {}) is True and d.event("session_transition", {}) is True
    assert len(sent) == 3

    class _Snap:  # minimal snapshot stand-in (dict-like path through snapshot_dict → {})
        class decision: action = type("A", (), {"value": "WAIT"})()
        entry_state = type("E", (), {"value": "INSIDE"})(); pa_side = "LONG"; session = type("S", (), {"value": "LONDON"})(); go_status = "NO-GO"
    d.on_snapshot(_Snap(), "UTC")
    assert len(sent) == 3                                                            # events mode: no status push at all
    d2 = Discord("NOPE", 300, 0, status_mode="changes"); d2.url = "x"; sent2 = []
    monkeypatch.setattr(d2, "send", lambda text="", embed=None: sent2.append(embed or text) or True)
    d2.on_snapshot(_Snap(), "UTC"); d2.on_snapshot(_Snap(), "UTC")
    assert len(sent2) == 1 and isinstance(sent2[0], dict)                            # changes mode: one card until the key changes


def test_scout_failure_card_and_engine_event(tmp_path):
    from xau_mt5_bot.cards import scout_card
    card = scout_card("scout_open_failed", {"session": "ASIA", "message": "BUY scout failed: 10027 AutoTrading disabled", "retcode": 10027, "retryable": True})
    assert card["title"].startswith("🔴 SCOUTS NOT PLACED") and "Algo Trading" in card["fields"][1]["value"]
    reset_history_cache()
    cfg, logger = _project(tmp_path); cfg.safety.require_hedging_for_scouts = True
    client = _SilverClient(); client.hedging = False
    engine = TradingEngine(client, cfg, logger)
    engine.run_cycle(client.tick.time); engine.shutdown()
    rows = [json.loads(r[0]) for r in logger._connect().execute("SELECT payload_json FROM events WHERE event_type='scout_open_failed'").fetchall()]
    assert rows and rows[0]["session"] in {"ASIA", "LONDON", "NEW_YORK"} and "NETTING" in rows[0]["message"] and rows[0]["retryable"] is False


# --- v3.3.0: cards tell the whole story, and the bot can explain a NO-GO ---------------------------------------------
def test_scouts_not_placed_card_maps_reason_text_not_just_retcodes():
    from xau_mt5_bot.cards import scout_card
    card = scout_card("scout_open_failed", {
        "session": "ASIA", "message": "Scout orders blocked: broker clock skew 10799s > 600s", "retcode": None,
        "retryable": True, "attempt": 4, "broker_utc_offset_hours": 3.0, "broker_clock_source": "auto",
        "broker_clock_residual_seconds": 0.3, "max_clock_skew_seconds": 600,
        "spread": 0.28, "max_spread": 0.60, "margin_free": 45000.0, "margin_required": 500.0,
        "next_retry_local": "06:42:10 IST",
    })
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["title"].startswith("🔴 SCOUTS NOT PLACED")
    assert "PC clock" in fields["Fix"] and "broker-server wall-clock" in fields["Fix"]
    assert "UTC+3" in fields["Detected offset"] and "residual skew 0.3s" in fields["Detected offset"]
    assert fields["Spread"] == "0.28 vs limit 0.60"
    assert fields["Margin"] == "free 45000.00 vs required 500.00"
    assert fields["Attempt"] == "#4" and fields["Next retry"] == "06:42:10 IST"


def test_scouts_not_placed_hints_cover_every_required_reason():
    from xau_mt5_bot.cards import reason_hint
    assert "PC clock" in reason_hint("Scout orders blocked: broker clock skew 900s > 600s")
    assert "max_spread_price" in reason_hint("Scout pair blocked: spread 0.90 above maximum")
    assert "free margin" in reason_hint("Scout pair blocked: Not enough money/margin")
    assert "safety gate refused" in reason_hint("Scout pair blocked: account not safe")
    assert "HEDGING" in reason_hint("SCOUTS DISABLED — ACCOUNT IS NETTING MODE")
    assert "17:00 New York" in reason_hint("Scout orders blocked: day locked: daily max loss -120.00")
    assert "demo-only" in reason_hint("Live account not authorized")
    assert reason_hint("something entirely new") == ""


def test_scouts_placed_card_reports_the_recovery_after_failures():
    from xau_mt5_bot.cards import scout_card
    card = scout_card("scout_session_open", {
        "session": "ASIA", "magic": 11001, "lot": 0.01, "open_price": 2500.10,
        "buy_ticket": 101, "buy_entry": 2500.20, "sell_ticket": 102, "sell_entry": 2500.00,
        "emergency_sl_price_distance": 20.0, "after_failed_attempts": 3,
    })
    fields = {f["name"]: f["value"] for f in card["fields"]}
    assert card["color"] == 0x1D9E75 and "after 3 failed attempts" in card["title"]
    assert "#101 @ 2500.20" in fields["BUY leg"] and "#102 @ 2500.00" in fields["SELL leg"]
    assert fields["Session open price"] == "2500.10" and "20.00" in fields["Emergency SL distance"]
    plain = scout_card("scout_session_open", {"session": "ASIA", "lot": 0.01, "after_failed_attempts": 0})
    assert "after" not in plain["title"]


def test_scout_rollback_close_and_adopted_cards_carry_the_full_story():
    from xau_mt5_bot.cards import scout_card
    rollback = scout_card("scout_rollback", {"session": "LONDON", "failed_leg": "SELL", "retcode": 10019,
                                             "reason": "Not enough money", "closed_tickets": [101], "pending": 0})
    fields = {f["name"]: f["value"] for f in rollback["fields"]}
    assert rollback["title"].startswith("🔴 SCOUT LEG ROLLED BACK") and rollback["color"] == 0xD85A30
    assert "SELL — retcode 10019" in fields["Failed leg"] and "[101]" in fields["Closed"]

    closed = scout_card("scout_session_close", {"session": "ASIA", "buy_ticket": 101, "sell_ticket": 102,
                                                "buy_pnl": 4.0, "sell_pnl": -6.0, "pnl": -2.0,
                                                "buy_mfe": 7.0, "buy_mae": -1.0, "sell_mfe": 1.0, "sell_mae": -8.0,
                                                "leader": "BUY", "verdict": "SUPPORTS", "strength": 6})
    fields = {f["name"]: f["value"] for f in closed["fields"]}
    assert closed["color"] == 0x8A8A8A
    assert "#101" in fields["BUY"] and "MFE 7.00 / MAE -1.00" in fields["BUY"]
    assert "#102" in fields["SELL"] and "MFE 1.00 / MAE -8.00" in fields["SELL"]
    assert fields["Pair P/L"] == "-2.00" and fields["Leader"] == "BUY" and fields["Verdict"] == "SUPPORTS 6/10"

    adopted = scout_card("scout_adopted", {"session": "NEW_YORK", "tickets": [7, 8], "legs": 2,
                                           "mfe_mae_restored": [True, True], "open_price": 2499.5,
                                           "open_time": "2026-09-07T12:00:00+00:00"})
    assert adopted["color"] == 0xEF9F27 and "Restart recovery" in adopted["description"]
    assert "[7, 8]" in adopted["fields"][0]["value"]


def test_order_cards_carry_the_plan_and_the_running_result():
    from xau_mt5_bot.cards import order_card
    placed = order_card("order", {"side": "LONG", "ticket": 55, "entry": 2500.5, "stop_loss": 2497.0,
                                  "take_profits": [2504.0, 2508.0, 2515.0], "actual_rr": [1.0, 2.1, 4.1],
                                  "volume": 0.01, "risk_currency": 3.5, "zone_kind": "DEMAND_OB",
                                  "trigger_reason": "M1 engulfing reclaim", "confluence": 72,
                                  "scout_verdict": "SUPPORTS", "scout_strength": 6, "scout_leader": "BUY",
                                  "silver": "COUPLED r=0.91 · SMT NONE"})
    fields = {f["name"]: f["value"] for f in placed["fields"]}
    assert "#55 @ 2500.50" in fields["Ticket / entry"] and "0.01 lot" in fields["Volume / risk"]
    assert fields["Stop loss"] == "2497.00"
    assert fields["Take profits"] == "TP1 2504.00 (1.00R) · TP2 2508.00 (2.10R) · TP3 2515.00 (4.10R)"
    assert "DEMAND_OB" in fields["Zone / trigger"] and "M1 engulfing reclaim" in fields["Zone / trigger"]
    assert "72/100" in fields["Confluence / scouts"] and "SUPPORTS 6/10" in fields["Confluence / scouts"]
    assert "COUPLED" in fields["Silver"]

    for kind in ("pa_partial", "pa_breakeven", "pa_tp2_lock", "pa_trail", "pa_close", "trade_closed"):
        card = order_card(kind, {"side": "LONG", "ticket": 55, "realized_pnl": 12.5, "remaining_volume": 0.005,
                                 "new_sl": 2500.6, "r": 1.03})
        managed = {f["name"]: f["value"] for f in card["fields"]}
        assert managed["Realised P/L so far"] == "12.50", kind
        assert managed["Remaining volume"] == "0.005 lot", kind
        assert managed["New SL"] == "2500.60", kind


def test_status_card_lists_every_active_router_veto_in_order():
    from xau_mt5_bot.cards import blocked_by_text, status_card, why_text
    snap = {"go_status": "NO-GO", "decision": {"action": "NO_TRADE", "reason": "M1 data is stale"},
            "analysis": {"blocked_by": [
                {"veto": "spread", "detail": "spread 0.95 vs limit 0.6", "flips_when": "spread falls back"},
                {"veto": "confluence", "detail": "confluence 41/100 < 55", "flips_when": "confluence reaches 55"},
                {"veto": "clock", "detail": "residual skew 1500s > 600s", "flips_when": "clocks agree"},
            ]}}
    text = blocked_by_text(snap)
    assert text.index("Spread") < text.index("Confluence < min") < text.index("Broker clock")
    assert "confluence 41/100 < 55" in text
    assert blocked_by_text({"analysis": {"blocked_by": []}}) == "nothing — every router veto is clear"
    card = status_card(snap, "UTC")
    assert any(f["name"] == "Blocked by" and "Broker clock" in f["value"] for f in card["fields"])
    assert "flips when: spread falls back" in why_text(snap)


def test_detections_card_collapses_round_sweeps_and_shows_zone_distance():
    from xau_mt5_bot.cards import detections_card
    sweeps = [{"level_type": "ROUND_1", "level_price": 4407.0, "sweep_price": 4407.4, "direction": "BULLISH", "age_bars": 2, "active": True},
              {"level_type": "ROUND_1", "level_price": 4408.0, "sweep_price": 4408.4, "direction": "BULLISH", "age_bars": 5, "active": True},
              {"level_type": "ROUND_1", "level_price": 4409.0, "sweep_price": 4409.4, "direction": "BULLISH", "age_bars": 9, "active": True},
              {"level_type": "ROUND_1", "level_price": 4409.0, "sweep_price": 4409.4, "direction": "BULLISH", "age_bars": 9, "active": True},
              {"level_type": "PDH", "level_price": 4420.0, "sweep_price": 4421.0, "direction": "BEARISH", "age_bars": 1, "active": True},
              {"level_type": "PDL", "level_price": 4380.0, "sweep_price": 4379.0, "direction": "BULLISH", "age_bars": 12, "active": True},
              {"level_type": "ASIA_HIGH", "level_price": 4415.0, "sweep_price": 4416.0, "direction": "BEARISH", "age_bars": 20, "active": True},
              {"level_type": "ASIA_LOW", "level_price": 4390.0, "sweep_price": 4389.0, "direction": "BULLISH", "age_bars": 30, "active": True},
              {"level_type": "PWH", "level_price": 4450.0, "sweep_price": 4451.0, "direction": "BEARISH", "age_bars": 40, "active": True},
              {"level_type": "PWL", "level_price": 4350.0, "sweep_price": 4349.0, "direction": "BULLISH", "age_bars": 50, "active": True},
              {"level_type": "STALE", "level_price": 4300.0, "sweep_price": 4299.0, "direction": "BULLISH", "age_bars": 3, "active": False}]
    snap = {"session": "LONDON", "timestamp": "2026-09-07T08:00:00+00:00", "bid": 4410.0, "sweeps": sweeps,
            "analysis": {"atr": 2.0},
            "zones": [{"side": "LONG", "kind": "DEMAND_OB", "low": 4400.0, "high": 4404.0, "score": 8.0, "status": "FRESH"}],
            "structures": {"M5": {"events": [{"event": "BOS", "level": 4405.0, "timestamp": "2026-09-07T07:50:00+00:00"},
                                             {"event": "CHOCH", "level": 4402.0, "timestamp": "2026-09-07T07:55:00+00:00"},
                                             {"event": "BOS", "level": 4400.0, "timestamp": "2026-09-07T07:58:00+00:00"}]}}}
    card = detections_card(snap, "UTC")
    fields = {f["name"]: f["value"] for f in card["fields"]}
    sweep_lines = fields["Active sweeps (6 newest)"].split("\n")
    assert len(sweep_lines) == 6
    assert sweep_lines[0] == "▲ ROUND_1 ×3 (4407.00–4409.00) · newest 2 bars"        # deduped and collapsed
    assert "STALE" not in fields["Active sweeps (6 newest)"]
    assert sweep_lines[1].startswith("▼ PDH") and "PWL" not in fields["Active sweeps (6 newest)"]
    assert fields["Structure events (2 newest / TF)"].count("M5 ") == 2               # not all three
    assert "4.00 ATR away" in fields["Zones (best first, distance in ATR)"]           # |4410 - 4402| / 2.0


def test_why_and_clock_commands(tmp_path):
    import sqlite3
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    cfg = load_config(tmp_path / "config.yaml")
    logger = AuditLogger(cfg.logging.sqlite_path, cfg.logging.jsonl_path)
    payload = {
        "timestamp": "2026-09-07T01:00:00+00:00", "session": "ASIA", "go_status": "NO-GO",
        "decision": {"action": "WAIT", "reason": "Price-action confluence is below threshold"},
        "analysis": {
            "blocked_by": [{"veto": "confluence", "detail": "confluence 41/100 < 55", "flips_when": "confluence reaches 55"},
                           {"veto": "clock", "detail": "residual skew 1500s > 600s", "flips_when": "the clocks agree"}],
            "broker_clock": {"offset_hours": 3.0, "residual_seconds": 1500.0, "raw_delta_seconds": 12300.0,
                             "source": "auto", "server": "Broker-Demo03", "measured_at": "2026-09-07T01:00:00+00:00"},
        },
    }
    with sqlite3.connect(cfg.logging.sqlite_path) as con:
        con.execute("INSERT INTO analysis_snapshots(timestamp,symbol,session,action,payload_json) VALUES(?,?,?,?,?)",
                    (payload["timestamp"], "XAUUSD", "ASIA", "WAIT", json.dumps(payload)))
    state = BotState(tmp_path)

    why = dispatch(state, "!why")
    assert "Confluence < min" in why and "confluence 41/100 < 55" in why
    assert "flips when: confluence reaches 55" in why
    assert "Broker clock" in why and "the clocks agree" in why

    clock = dispatch(state, "!clock")
    assert "guard BLOCKED" in clock
    assert "System UTC:" in clock and "Broker server time:" in clock
    assert "UTC+3" in clock and "Broker-Demo03" in clock
    assert "Residual skew after removing the offset: 1500.0s" in clock
    assert dispatch(state, "!blocked") == why and dispatch(state, "!time") == clock
    assert "`!why`" in HELP and "`!clock`" in HELP


def test_no_order_commands_were_added():
    for cmd in ("buy", "sell", "close", "long", "short", "open", "modify"):
        reply = dispatch(BotState(ROOT), f"!{cmd}")
        assert "not implemented by design" in reply


def test_scout_lifecycle_and_clock_events_post_on_the_quiet_default(monkeypatch):
    from xau_mt5_bot.notify import Discord
    d = Discord("NOPE", 0, 0); d.url = "https://example.invalid/hook"; sent = []
    monkeypatch.setattr(d, "send", lambda text="", embed=None: sent.append(embed or text) or True)
    assert d.event_level == "trade"
    for kind in ("scout_session_open", "scout_session_close", "scout_rollback", "scout_adopted", "scout_open_failed"):
        assert d.event(kind, {"session": kind.upper()}) is True, kind
    assert d.event("broker_clock_offset", {"offset_hours": 3.0, "residual_skew_seconds": 0.2,
                                           "max_clock_skew_seconds": 600, "server": "Broker-Demo03",
                                           "message": "Broker server clock is UTC+3"}) is True
    assert d.event("clock_check", {}) is False                      # still quiet: the offset card replaces it
    clock_card = sent[-1]
    assert clock_card["title"] == "🕰️ BROKER CLOCK · UTC+3"
    assert "Broker-Demo03" in json.dumps(clock_card)
