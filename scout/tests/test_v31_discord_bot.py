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
    assert isinstance(status, dict) and "WAIT / NO MANUAL ENTRY" in status["title"]
    fields = {f["name"]: f["value"] for f in status["fields"]}
    assert "COUPLED" in fields["Silver"] and "UNAVAILABLE" in fields["Fakeout assessment"]
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


def test_explicit_events_mode_suppresses_status_and_noise(monkeypatch):
    from xau_mt5_bot.notify import Discord
    d = Discord("NOPE", 0, 0, status_mode="events"); d.url = "https://example.invalid/hook"; sent = []
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
