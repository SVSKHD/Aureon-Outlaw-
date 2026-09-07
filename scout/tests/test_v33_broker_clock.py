"""v3.3.0 — the broker server clock is not UTC.

MetaTrader5 reports `symbol_info_tick().time` and `copy_rates_*()['time']` as epoch seconds of the BROKER
SERVER's wall clock. Treating them as UTC made a UTC+3 broker look like a 10 799 s clock skew, which blocked
every scout and PA order forever and pushed every bar timestamp three hours into the future.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from conftest import FakeClient
from test_v18_features import _Logger
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.features import freshness_age_seconds
from xau_mt5_bot.models import SessionName
from xau_mt5_bot.mt5_client import BrokerClock, Tick, quantize_offset
from xau_mt5_bot.scouts import ScoutManager
from xau_mt5_bot.sessions import SessionEngine

ROOT = Path(__file__).resolve().parents[1]
# Monday 7 Sep 2026, 05:00 UTC — inside the Asia session (Tokyo 09:00 JST open = 00:00 UTC; London opens 07:00 UTC).
# Three hours later is 08:00 UTC, which is LONDON — so an unconverted broker stamp lands in the wrong session.
ASIA_NOW = datetime(2026, 9, 7, 5, 0, tzinfo=UTC)


class _CycleLogger(_Logger):
    def snapshot(self, *a): pass
    def order(self, *a): pass
    def trade(self, *a): pass

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]

    def payload(self, kind: str) -> dict:
        return next(p for k, p in self.events if k == kind)


class _BrokerBarClient(FakeClient):
    """Synthetic native history generated in BROKER-SERVER time and handed back through the clock, exactly
    the path MT5Client takes: raw epoch → minus the detected offset → true UTC."""
    FREQ = {"M1": "1min", "M5": "5min", "M15": "15min", "H1": "1h", "H4": "4h", "D1": "1D"}

    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        rng = np.random.default_rng(abs(hash(timeframe)) % 1000)
        broker_end = self.tick.time + timedelta(hours=self.broker_offset_hours)     # what the server calls "now"
        t = pd.date_range(end=broker_end, periods=count, freq=self.FREQ[timeframe], tz="UTC")
        p = 2500 + np.cumsum(rng.normal(0, 0.3, count))
        raw = pd.DataFrame({"time": t, "open": p, "high": p + 0.5, "low": p - 0.5,
                            "close": p + rng.normal(0, 0.1, count), "tick_volume": 100})
        raw["time"] = self.clock.frame_to_utc(raw["time"])                          # the single conversion point
        return raw


@pytest.fixture(autouse=True)
def _clear_history_cache():
    from xau_mt5_bot.history import reset_history_cache
    reset_history_cache()
    yield
    reset_history_cache()


# --- 1. offset detection --------------------------------------------------------------------------------------------
def test_quantize_rounds_to_the_nearest_half_hour():
    assert quantize_offset(timedelta(seconds=10799)) == timedelta(hours=3)
    assert quantize_offset(timedelta(seconds=10801)) == timedelta(hours=3)
    assert quantize_offset(timedelta(seconds=-7195)) == timedelta(hours=-2)
    assert quantize_offset(timedelta(minutes=330)) == timedelta(hours=5.5)          # UTC+5:30 brokers exist
    assert quantize_offset(timedelta(seconds=45)) == timedelta(0)


def test_broker_clock_detects_three_hours_and_leaves_no_residual():
    clock = BrokerClock()
    now = ASIA_NOW
    broker_epoch = (now + timedelta(hours=3, seconds=1)).timestamp()
    info = clock.measure(broker_epoch, now, server="Broker-Demo03")
    assert info["offset_hours"] == 3.0
    assert abs(info["residual_seconds"]) < 5
    assert info["raw_delta_seconds"] == pytest.approx(10801, abs=1)
    assert info["source"] == "auto" and info["server"] == "Broker-Demo03"
    # and the conversion undoes it exactly
    assert clock.epoch_to_utc(broker_epoch) == now + timedelta(seconds=1)


# --- (a) offset detected, orders NOT blocked, scout pair opens -------------------------------------------------------
def test_utc_plus_three_broker_is_detected_and_scouts_open(config):
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    logger = _CycleLogger()
    engine = TradingEngine(client, config, logger)
    engine.cycle_now = ASIA_NOW                              # run_cycle() normally sets this before the gate
    engine._validate_clock(ASIA_NOW)

    assert engine.broker_clock["offset_hours"] == 3.0
    assert abs(engine.clock_skew) < 5
    assert engine.clock_ok is True

    ok, why = engine.orders_allowed()
    assert ok, why
    assert "clock skew" not in why

    result = engine.scouts.open_session(SessionName.ASIA, ASIA_NOW)
    assert result.success, result.message
    assert len(client.positions(config.symbol, config.magic.scout_asia)) == 2

    event = logger.payload("broker_clock_offset")
    assert event["offset_hours"] == 3.0 and abs(event["residual_skew_seconds"]) < 5
    assert event["server"] == "FakeBroker-Demo"
    assert "UTC+3" in event["message"]


def test_before_the_fix_the_raw_delta_would_have_tripped_the_guard(config):
    """The exact live symptom: 10 799 s > 600 s. The raw delta is still that big; the guard no longer reads it."""
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW - timedelta(seconds=1), 2500.00, 2500.20)
    engine = TradingEngine(client, config, _CycleLogger())
    engine.cycle_now = ASIA_NOW
    engine._validate_clock(ASIA_NOW)
    assert engine.broker_clock["raw_delta_seconds"] == pytest.approx(10799, abs=1)
    assert abs(engine.broker_clock["raw_delta_seconds"]) > config.safety.max_clock_skew_seconds
    assert engine.clock_ok is True and abs(engine.clock_skew) < 5


# --- (b) genuine skew after the offset still blocks -------------------------------------------------------------------
def test_twentyfive_minute_skew_after_the_offset_blocks_orders(config):
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    logger = _CycleLogger()
    engine = TradingEngine(client, config, logger)
    engine.cycle_now = ASIA_NOW
    engine._validate_clock(ASIA_NOW)                       # offset pinned at +3 h with no residual
    assert engine.clock_ok is True

    later = ASIA_NOW + timedelta(minutes=1)
    client.tick = Tick(later, 2500.00, 2500.20)
    client.clock_skew_seconds = 25 * 60                     # PC clock drifts 25 minutes inside the hour
    engine.cycle_now = later
    engine._validate_clock(later)

    assert engine.broker_clock["offset_hours"] == 3.0       # not re-measured inside the hour, so skew is visible
    assert engine.clock_skew == pytest.approx(1500, abs=2)
    assert engine.clock_ok is False

    ok, why = engine.orders_allowed()
    assert not ok
    assert "broker clock skew" in why and "1500s > 600s" in why
    assert "broker offset +3h already removed" in why

    scouts = engine.scouts
    result = scouts.open_session(SessionName.ASIA, ASIA_NOW)
    assert not result.success
    assert "Scout orders blocked" in result.message and "broker clock skew" in result.message
    assert client.positions(config.symbol, config.magic.scout_asia) == []


def test_offset_is_remeasured_at_most_once_per_hour_and_on_reconnect(config):
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    client.refresh_broker_offset(now=ASIA_NOW)
    assert client.offset_measurements == 1
    client.refresh_broker_offset(now=ASIA_NOW + timedelta(minutes=59))
    assert client.offset_measurements == 1                                  # inside the hour → no re-measure
    client.refresh_broker_offset(now=ASIA_NOW + timedelta(minutes=61))
    assert client.offset_measurements == 2
    client.clock.invalidate()                                               # what reconnect() does
    client.refresh_broker_offset(now=ASIA_NOW + timedelta(minutes=62))
    assert client.offset_measurements == 3


# --- (c) bar times are converted, so sessions and freshness are right -------------------------------------------------
def test_history_bar_times_are_converted_to_utc(config):
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    client.refresh_broker_offset(now=ASIA_NOW)

    m1 = client.get_bars(config.symbol, "M1", 300)
    latest = pd.Timestamp(m1.iloc[-1]["time"]).to_pydatetime()
    assert latest == ASIA_NOW                                               # not ASIA_NOW + 3 h

    sessions = SessionEngine(config.sessions)
    assert sessions.session_at(latest) == SessionName.ASIA                  # +3 h would land in a different session
    assert sessions.session_at(latest + timedelta(hours=3)) == SessionName.LONDON   # what the old code saw

    assert freshness_age_seconds(m1, pd.Timestamp(ASIA_NOW)) == 0.0         # LIVE, not a 3 h-in-the-future STALE


def test_uncorrected_bars_would_break_freshness_and_session(config):
    """Guard against a regression that stops applying the offset in get_bars()."""
    raw = _BrokerBarClient(broker_offset_hours=3.0)
    raw.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    frame = raw.get_bars(config.symbol, "M1", 60)                            # offset never measured → still broker time
    latest = pd.Timestamp(frame.iloc[-1]["time"]).to_pydatetime()
    assert latest == ASIA_NOW + timedelta(hours=3)
    assert freshness_age_seconds(frame, pd.Timestamp(ASIA_NOW)) == 0.0       # clamped, so the age alone hides it
    assert SessionEngine(config.sessions).session_at(latest) == SessionName.LONDON


def test_full_cycle_on_a_utc_plus_three_broker_places_scouts(config, tmp_path: Path):
    config.project_dir = str(tmp_path)                      # isolate the positions/scouts state files
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    logger = _CycleLogger()
    engine = TradingEngine(client, config, logger)
    snapshot = engine.run_cycle(ASIA_NOW)

    assert snapshot.session == SessionName.ASIA
    assert snapshot.freshness.value == "LIVE"
    assert snapshot.timestamp == ASIA_NOW
    assert snapshot.analysis["broker_clock"]["offset_hours"] == 3.0
    assert not any(v["veto"] == "clock" for v in snapshot.analysis["blocked_by"])
    assert len(client.positions(config.symbol, config.magic.scout_asia)) == 2
    assert "scout_session_open" in logger.kinds()


# --- (d) manual override wins over auto-detect -------------------------------------------------------------------------
def test_manual_override_wins_over_auto_detection(config):
    client = _BrokerBarClient(broker_offset_hours=3.0, manual_offset_hours=2.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    info = client.refresh_broker_offset(now=ASIA_NOW)
    assert info["offset_hours"] == 2.0 and info["source"] == "manual"        # NOT the 3.0 h that auto-detect would find
    assert info["residual_seconds"] == pytest.approx(3600, abs=2)            # the extra hour shows up as skew

    engine = TradingEngine(client, config, _CycleLogger())
    engine.cycle_now = ASIA_NOW
    engine._validate_clock(ASIA_NOW)
    assert engine.broker_clock["offset_hours"] == 2.0
    assert engine.clock_ok is False                                          # 3600 s > 600 s: a wrong pin is visible
    ok, why = engine.orders_allowed()
    assert not ok and "broker offset +2h already removed" in why


def test_manual_override_is_never_replaced_by_a_later_measurement(config):
    client = _BrokerBarClient(broker_offset_hours=3.0, manual_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    client.refresh_broker_offset(now=ASIA_NOW)
    later = ASIA_NOW + timedelta(hours=2)
    client.tick = Tick(later, 2500.00, 2500.20)
    client.broker_offset_hours = 4.0                                         # broker moves to summer time
    client.refresh_broker_offset(now=later, force=True)
    assert client.clock.info()["offset_hours"] == 3.0                        # pinned value stands
    assert client.clock.info()["residual_seconds"] == pytest.approx(3600, abs=2)


def test_config_accepts_null_and_a_float_offset(tmp_path: Path):
    import yaml
    from xau_mt5_bot.config import load_config
    base = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    assert base["safety"]["broker_utc_offset_hours"] is None                 # shipped default is auto-detect

    cfg = load_config(ROOT / "config.yaml")
    assert cfg.safety.broker_utc_offset_hours is None
    assert cfg.safety.broker_offset_remeasure_seconds == 3600

    base["safety"]["broker_utc_offset_hours"] = 3.0
    pinned = tmp_path / "pinned.yaml"; pinned.write_text(yaml.safe_dump(base), encoding="utf-8")
    assert load_config(pinned).safety.broker_utc_offset_hours == 3.0

    base["safety"]["broker_utc_offset_hours"] = 30.0
    bad = tmp_path / "bad.yaml"; bad.write_text(yaml.safe_dump(base), encoding="utf-8")
    with pytest.raises(Exception):
        load_config(bad)


# --- audit: no module compares a raw MT5 timestamp with datetime.now(UTC) ---------------------------------------------
def test_no_module_reads_a_raw_mt5_epoch_outside_the_conversion_point():
    """Every broker timestamp must pass through BrokerClock. Only mt5_client.py may call fromtimestamp()."""
    offenders = []
    for path in sorted((ROOT / "src" / "xau_mt5_bot").glob("*.py")):
        if path.name == "mt5_client.py":
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "datetime.fromtimestamp(" in line:
                offenders.append(f"{path.name}:{i}")
    assert offenders == [], f"raw MT5 epoch conversions outside mt5_client.py: {offenders}"


def test_mt5_client_applies_the_offset_to_ticks_bars_and_deal_history(monkeypatch):
    """Drive the REAL MT5Client against a mocked UTC+3 terminal: a source grep cannot prove the conversion."""
    from test_mt5_adapter_contract import MockMT5
    import xau_mt5_bot.mt5_client as adapter

    fake = MockMT5(broker_offset_hours=3.0)
    true_now = datetime.now(UTC).replace(microsecond=0)
    fake.tick_epoch_utc = int(true_now.timestamp())        # the tick's TRUE UTC instant is now
    monkeypatch.setattr(adapter, "mt5", fake)
    client = adapter.MT5Client(symbol="XAUUSD")

    validation = client.validate_terminal()
    assert validation["broker_utc_offset_hours"] == 3.0                    # detected, not assumed
    assert abs(validation["broker_clock_residual_seconds"]) < 5
    assert validation["tick_age_seconds"] < 5                              # not the 10 800 s the raw stamp implies

    # 1. the tick comes back as true UTC, not the broker's wall clock
    assert fake.symbol_info_tick("XAUUSD").time == int(true_now.timestamp()) + 3 * 3600   # what MT5 actually reports
    assert client.get_tick("XAUUSD").time == true_now

    # 2. bar times are converted, and stay a correct bar grid
    bars = client.get_bars("XAUUSD", "M5", 12)
    assert len(bars) == 12
    latest = pd.Timestamp(bars.iloc[-1]["time"]).to_pydatetime()
    assert latest == true_now - timedelta(seconds=int(true_now.timestamp()) % 300)
    assert latest <= true_now, "converted bars must not sit in the future"
    assert bars.time.is_monotonic_increasing
    assert (bars.time.diff().dropna() == pd.Timedelta(minutes=5)).all()

    # 3. deal history is QUERIED in broker time and REPORTED in UTC
    opened = true_now - timedelta(hours=2)
    deals = client.closed_deals(77, opened)
    start, end, _ = fake.history_window
    assert start == opened - timedelta(days=1) + timedelta(hours=3)        # bounds shifted into broker time
    assert end - start > timedelta(0)
    assert deals[0]["time"] == datetime.fromtimestamp(int(end.timestamp()) - 1, tz=UTC) - timedelta(hours=3)

    # 4. with no offset the same calls are identity — the conversion cannot skew a UTC broker
    plain = MockMT5(broker_offset_hours=0.0)
    plain.tick_epoch_utc = int(true_now.timestamp())
    monkeypatch.setattr(adapter, "mt5", plain)
    plain_client = adapter.MT5Client(symbol="XAUUSD")
    plain_client.validate_terminal()
    assert plain_client.broker_clock()["offset_hours"] == 0.0
    assert plain_client.get_tick("XAUUSD").time == true_now


def test_uncorrected_get_bars_would_return_future_timestamps(monkeypatch):
    """The regression this guards: without the offset, MT5Client bars land 3 h in the future."""
    from test_mt5_adapter_contract import MockMT5
    import xau_mt5_bot.mt5_client as adapter

    fake = MockMT5(broker_offset_hours=3.0)
    true_now = datetime.now(UTC).replace(microsecond=0)
    fake.tick_epoch_utc = int(true_now.timestamp())
    monkeypatch.setattr(adapter, "mt5", fake)
    client = adapter.MT5Client(symbol="XAUUSD")          # offset never measured → still 0
    latest = pd.Timestamp(client.get_bars("XAUUSD", "M5", 6).iloc[-1]["time"]).to_pydatetime()
    assert latest > true_now and (latest - true_now) >= timedelta(hours=2, minutes=55)


def test_mt5_client_source_no_longer_claims_timestamps_are_utc():
    src = (ROOT / "src" / "xau_mt5_bot" / "mt5_client.py").read_text(encoding="utf-8")
    assert "never apply a broker-offset subtraction" not in src            # the v3.2.0 assumption is gone


# --- the Firestore contract really carries what it claims -------------------------------------------------------------
def test_firestore_summary_carries_the_offset_and_the_veto_list(config, tmp_path: Path):
    import json as _json
    from xau_mt5_bot.firestore_sink import FirestoreSink

    config.project_dir = str(tmp_path)
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    engine = TradingEngine(client, config, _CycleLogger())
    snapshot = engine.run_cycle(ASIA_NOW)
    engine.shutdown()

    summary = FirestoreSink("missing")._summary(snapshot)
    contract = _json.loads((ROOT / "FIRESTORE_SCHEMA.json").read_text(encoding="utf-8"))["collections"]["sessions"]
    assert summary["schema_version"] == "3.3.0"
    assert set(contract["price_required"]) <= set(summary["price"])
    assert summary["price"]["broker_utc_offset_hours"] == 3.0
    assert set(contract["analysis_required"]) <= set(summary["analysis"])
    assert set(contract["broker_clock_fields"]) <= set(summary["analysis"]["broker_clock"])
    for veto in summary["analysis"]["router_vetoes"]:
        assert set(contract["veto_fields"]) <= set(veto)
    order = [v["veto"] for v in summary["analysis"]["router_vetoes"]]
    from xau_mt5_bot.decision_router import VETO_ORDER
    assert tuple(order) == VETO_ORDER                                    # evaluation order, always the same


# --- review follow-ups: the veto list must match the decision the router actually reached ------------------------------
def test_veto_list_covers_every_router_no_trade_branch():
    """`!why` must never say "nothing blocks" while the router returns NO_TRADE."""
    import inspect
    from xau_mt5_bot import decision_router as dr

    source = inspect.getsource(dr.final_decision_router)
    returns = source.count("return Decision(")
    # every branch except the final authorising `return Decision(Action(value.pa_side.value), ...)`
    assert returns - 1 == 14
    covered = set(dr.VETO_ORDER)
    for name in ("trigger_consumed", "trigger_ownership", "data_stale", "account_safety", "spread", "setup",
                 "confluence", "zone", "trigger", "slow", "scouts", "rr", "target"):
        assert name in covered, name
    # and the engine-level overrides that run after the router
    for name in ("higher_tf_conflict", "session_target", "session_feasibility", "cold_start", "send_time",
                 "clock", "day_lock"):
        assert name in covered, name

    from xau_mt5_bot.cards import VETO_LABELS
    assert set(VETO_LABELS) == covered, "every veto needs a card label"


def test_engine_overrides_appear_in_blocked_by(config, tmp_path: Path):
    """A cold-start NO_TRADE is a real veto and has to show up, not read as "nothing blocks"."""
    config.project_dir = str(tmp_path)
    client = _BrokerBarClient(broker_offset_hours=3.0)
    client.tick = Tick(ASIA_NOW, 2500.00, 2500.20)
    engine = TradingEngine(client, config, _CycleLogger())
    snapshot = engine.run_cycle(ASIA_NOW)                       # first cycle is always the cold start
    engine.shutdown()

    order = [v["veto"] for v in snapshot.analysis["router_vetoes"]]
    from xau_mt5_bot.decision_router import VETO_ORDER
    assert tuple(order) == VETO_ORDER
    active = {v["veto"] for v in snapshot.analysis["blocked_by"]}
    assert active, "a NO-GO cycle must name at least one veto"
    if snapshot.go_status == "NO-GO":
        from xau_mt5_bot.cards import blocked_by_text
        assert blocked_by_text(snapshot.analysis and {"analysis": snapshot.analysis}) != "nothing — every router veto is clear"


def test_blocked_by_names_the_post_router_overrides():
    from xau_mt5_bot.cards import blocked_by_text, why_text
    from xau_mt5_bot.decision_router import blocked_by
    from xau_mt5_bot.models import (DecisionInput, EntryState, Freshness, ScoutSnapshot, SessionName, Side,
                                    SpreadState, TargetRealism, TriggerResult)

    scout = ScoutSnapshot(SessionName.ASIA)
    scout.market_speed = "NORMAL"
    trigger = TriggerResult(True, "M1_ENGULFING"); trigger.fresh = True
    good = DecisionInput(Side.LONG, True, EntryState.INSIDE, trigger, scout, Freshness.LIVE, SpreadState.NORMAL,
                         True, 2.0, 1.0, TargetRealism.REALISTIC, 80, 55, None, 8, False)
    assert blocked_by(good, {"clock_ok": True}) == []            # nothing active: the router would authorise

    for key, veto in (("cold_start", "cold_start"), ("send_time_withheld", "send_time"),
                      ("session_target_unlikely", "session_target"), ("higher_tf_conflict", "higher_tf_conflict")):
        items = blocked_by(good, {"clock_ok": True, key: True})
        assert [i["veto"] for i in items] == [veto], key
        snap = {"analysis": {"blocked_by": items}}
        assert blocked_by_text(snap) != "nothing — every router veto is clear"
        assert "flips when:" in why_text(snap)


def test_router_early_vetoes_are_reported(config):
    from xau_mt5_bot.decision_router import blocked_by, final_decision_router
    from xau_mt5_bot.models import (Action, DecisionInput, EntryState, Freshness, ScoutSnapshot, SessionName,
                                    Side, SpreadState, TargetRealism, TriggerResult)

    scout = ScoutSnapshot(SessionName.ASIA); scout.market_speed = "NORMAL"
    trigger = TriggerResult(True, "M1_ENGULFING"); trigger.fresh = True
    base = dict(pa_side=Side.LONG, setup_valid=True, entry_state=EntryState.INSIDE, trigger=trigger, scout=scout,
                freshness=Freshness.LIVE, spread_state=SpreadState.NORMAL, account_safe=True, rr=2.0, min_rr=1.0,
                target_realism=TargetRealism.REALISTIC, confluence=80, min_confluence=55, setup_id=None,
                scout_contradiction_threshold=8, hold_when_slow=False)

    stale = DecisionInput(**{**base, "freshness": Freshness.STALE})
    assert final_decision_router(stale).action == Action.NO_TRADE
    assert [i["veto"] for i in blocked_by(stale, {"clock_ok": True})] == ["data_stale"]

    unsafe = DecisionInput(**{**base, "account_safe": False})
    assert final_decision_router(unsafe).action == Action.NO_TRADE
    items = blocked_by(unsafe, {"clock_ok": True, "account_reason": "No free margin"})
    assert [i["veto"] for i in items] == ["account_safety"] and "No free margin" in items[0]["detail"]

    no_setup = DecisionInput(**{**base, "setup_valid": False})
    assert final_decision_router(no_setup).action == Action.NO_TRADE
    assert [i["veto"] for i in blocked_by(no_setup, {"clock_ok": True})] == ["setup"]

    consumed = TriggerResult(True, "M1_ENGULFING"); consumed.fresh = True; consumed.consumed = True
    used = DecisionInput(**{**base, "trigger": consumed})
    assert final_decision_router(used).action == Action.NO_TRADE
    assert [i["veto"] for i in blocked_by(used, {"clock_ok": True})] == ["trigger_consumed"]


def test_repeat_close_does_not_re_emit_a_stale_pair_card(config):
    """A second close_session() with nothing open must not replay the last pair's tickets and P/L."""
    from types import SimpleNamespace
    events: list[tuple[str, dict]] = []
    client = FakeClient()
    manager = ScoutManager(client, config, lambda k, p: events.append((k, p)))
    magic = config.magic.scout_asia
    for side, kind in (("BUY", 0), ("SELL", 1)):
        client._positions.append(SimpleNamespace(ticket=900 + kind, symbol=config.symbol, magic=magic, volume=0.01,
                                                 type=kind, price_open=2500.0, profit=1.0, comment=side, sl=0.0, tp=0.0))
    manager.current_session = SessionName.ASIA
    manager.session_open_price = 2500.0
    manager.session_open_time = ASIA_NOW

    first = manager.close_session(SessionName.ASIA)
    assert first.success
    closes = [p for k, p in events if k == "scout_session_close"]
    assert len(closes) == 1 and closes[0]["buy_ticket"] == 900 and closes[0]["sell_ticket"] == 901

    events.clear()
    second = manager.close_session(SessionName.ASIA)
    assert second.success and "No open" in second.message
    assert [k for k, _ in events if k == "scout_session_close"] == []       # no duplicate, no stale tickets


def test_detection_signature_moves_when_a_zone_changes():
    from xau_mt5_bot.cards import detection_signature
    base = {"patterns": [], "structures": {}, "sweeps": [],
            "zones": [{"kind": "DEMAND_OB", "side": "LONG", "low": 2400.0, "high": 2404.0, "score": 8.0, "status": "FRESH"}]}
    same = detection_signature(base)
    assert detection_signature(dict(base)) == same                          # stable when nothing changed

    moved = {**base, "zones": [{**base["zones"][0], "low": 2401.0, "high": 2405.0}]}
    assert detection_signature(moved) != same                               # a new/changed zone pushes the card

    mitigated = {**base, "zones": [{**base["zones"][0], "status": "MITIGATED"}]}
    assert detection_signature(mitigated) != same

    rescored = {**base, "zones": [{**base["zones"][0], "score": 8.4}]}
    assert detection_signature(rescored) == same                            # score drift alone is not a detection


# --- review nitpicks: quantisation must not silently swallow real drift ------------------------------------------------
def test_established_offset_is_not_moved_by_sub_hour_drift():
    """A slow PC must show as skew, not be absorbed into a new half-hour timezone."""
    clock = BrokerClock()
    clean = clock.measure((ASIA_NOW + timedelta(hours=3)).timestamp(), ASIA_NOW)
    assert clean["offset_hours"] == 3.0 and clean["confident"] is True

    drifted = clock.measure((ASIA_NOW + timedelta(hours=3, seconds=1500)).timestamp(), ASIA_NOW)
    assert drifted["offset_hours"] == 3.0                       # held: off-grid, and a sub-hour change anyway
    assert drifted["residual_seconds"] == pytest.approx(1500, abs=2)
    assert drifted["confident"] is False

    dst = clock.measure((ASIA_NOW + timedelta(hours=2)).timestamp(), ASIA_NOW)
    assert dst["offset_hours"] == 2.0                           # a whole-hour DST change IS adopted
    assert abs(dst["residual_seconds"]) < 5 and dst["confident"] is True


def test_an_off_grid_difference_is_refused_not_invented_into_a_timezone():
    """Every MT5 server timezone sits on the half-hour grid. A difference that does not is a broken clock:
    refuse it, so the whole difference shows as skew and the guard blocks, rather than inventing an offset."""
    broken = BrokerClock().measure((ASIA_NOW + timedelta(seconds=1500)).timestamp(), ASIA_NOW)
    assert broken["offset_hours"] == 0.0                                # NOT rounded up to 0.5 h
    assert broken["residual_seconds"] == pytest.approx(1500, abs=2)     # the full error is visible
    assert broken["confident"] is False

    half = BrokerClock().measure((ASIA_NOW + timedelta(hours=3, minutes=30)).timestamp(), ASIA_NOW)
    assert half["offset_hours"] == 3.5 and half["confident"] is True    # a real half-hour zone still works

    tidy = BrokerClock().measure((ASIA_NOW + timedelta(hours=3, seconds=4)).timestamp(), ASIA_NOW)
    assert tidy["offset_hours"] == 3.0 and tidy["confident"] is True


def test_sweep_lines_place_collapsed_rounds_by_age():
    """Old ROUND_1 sweeps must not consume the 6-line budget ahead of fresher sweeps."""
    from xau_mt5_bot.cards import _sweep_lines
    sweeps = [{"level_type": "ROUND_1", "level_price": 4400.0 + i, "sweep_price": 4400.4 + i,
               "direction": "BULLISH", "age_bars": 40 + i, "active": True} for i in range(3)]
    sweeps += [{"level_type": lt, "level_price": p, "sweep_price": p + 1, "direction": "BEARISH",
                "age_bars": age, "active": True}
               for lt, p, age in (("PDH", 4420.0, 1), ("PDL", 4380.0, 2), ("ASIA_HIGH", 4415.0, 3),
                                  ("ASIA_LOW", 4390.0, 4), ("PWH", 4450.0, 5), ("PWL", 4350.0, 6))]
    lines = _sweep_lines(sweeps, "UTC")
    assert len(lines) == 6
    assert "ROUND_1" not in "\n".join(lines)                    # 40 bars old: rightly pushed out by fresher sweeps
    assert lines[0].startswith("▼ PDH")

    fresh_rounds = [{**sw, "age_bars": 0} for sw in sweeps[:3]]
    lines = _sweep_lines(fresh_rounds + sweeps[3:], "UTC")
    assert lines[0] == "▲ ROUND_1 ×3 (4400.00–4402.00) · newest 0 bars"   # newest → first


def test_management_retry_events_are_full_cards():
    """The docs promise every management card carries P/L, remaining volume and the new SL."""
    from xau_mt5_bot.cards import event_card
    from xau_mt5_bot.notify import Discord
    for kind in ("pa_breakeven_retry", "pa_tp2_lock_retry"):
        card = event_card(kind, {"side": "LONG", "ticket": 55, "realized_pnl": 12.5,
                                 "remaining_volume": 0.005, "new_sl": 2500.6, "r": 1.03})
        fields = {f["name"]: f["value"] for f in card["fields"]}
        assert fields["Realised P/L so far"] == "12.50", kind
        assert fields["Remaining volume"] == "0.005 lot", kind
        assert fields["New SL"] == "2500.60", kind
        assert "retry" in card["title"].lower(), kind
        assert Discord("NOPE", 0, 0).is_eligible(kind), kind


def test_clock_command_reports_the_real_broker_reading(tmp_path: Path):
    """Broker time must include the residual: showing system UTC + offset would hide the very skew !clock exists for."""
    import json
    import sqlite3
    from xau_mt5_bot.config import load_config
    from xau_mt5_bot.discord_bot import BotState, dispatch

    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    cfg = load_config(tmp_path / "config.yaml")
    from xau_mt5_bot.logger import AuditLogger
    AuditLogger(cfg.logging.sqlite_path, cfg.logging.jsonl_path)
    payload = {"timestamp": "2026-09-07T05:00:00+00:00", "session": "ASIA", "go_status": "NO-GO",
               "decision": {"action": "NO_TRADE", "reason": "clock"},
               "analysis": {"blocked_by": [{"veto": "clock", "detail": "residual skew 1500s > 600s",
                                            "flips_when": "the clocks agree"}],
                            "broker_clock": {"offset_hours": 3.0, "residual_seconds": 1500.0,
                                             "raw_delta_seconds": 12300.0, "source": "auto", "confident": False,
                                             "server": "Broker-Demo03", "measured_at": "2026-09-07T05:00:00+00:00"}}}
    with sqlite3.connect(cfg.logging.sqlite_path) as con:
        con.execute("INSERT INTO analysis_snapshots(timestamp,symbol,session,action,payload_json) VALUES(?,?,?,?,?)",
                    (payload["timestamp"], "XAUUSD", "ASIA", "NO_TRADE", json.dumps(payload)))

    reply = dispatch(BotState(tmp_path), "!clock")
    now = datetime.now(UTC)
    expected = (now + timedelta(hours=3, seconds=1500)).strftime("%Y-%m-%d %H:%M")
    offset_only = (now + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M")
    assert expected in reply and offset_only not in reply       # the 25-minute skew is visible, not hidden
    assert "+1500s skew" in reply
    assert "guard BLOCKED" in reply
    assert "pin `safety.broker_utc_offset_hours`" in reply      # unconfident reading is called out


# --- coverage ported from master's PR #3 suite before its API-bound tests were removed ---------------------------------
def test_validate_terminal_refuses_a_tick_from_the_future(monkeypatch):
    """A 25-minute-ahead tick is a broken PC clock, not a timezone: refuse to start rather than trade on it."""
    from test_mt5_adapter_contract import MockMT5
    import xau_mt5_bot.mt5_client as adapter

    fake = MockMT5()
    fake.tick_epoch_utc = int(datetime.now(UTC).timestamp()) + 1500          # ahead, and off the half-hour grid
    monkeypatch.setattr(adapter, "mt5", fake)
    with pytest.raises(RuntimeError, match="future"):
        adapter.MT5Client(symbol="XAUUSD").validate_terminal()

    on_grid = MockMT5(broker_offset_hours=3.0)
    on_grid.tick_epoch_utc = int(datetime.now(UTC).timestamp())              # exactly +3 h: a real UTC+3 server
    monkeypatch.setattr(adapter, "mt5", on_grid)
    validation = adapter.MT5Client(symbol="XAUUSD").validate_terminal()
    assert validation["broker_utc_offset_hours"] == 3.0
    assert validation["broker_clock_source"] == "auto" and abs(validation["tick_age_seconds"]) < 5


def test_legacy_seconds_kwarg_still_pins_the_offset():
    """master PR #2/#3 pinned the offset in seconds; that call site must keep working."""
    import xau_mt5_bot.mt5_client as adapter
    from types import SimpleNamespace

    stub = SimpleNamespace(**{f"TIMEFRAME_{tf}": i for i, tf in enumerate(("M1", "M5", "M15", "H1", "H4", "D1"))})
    original = adapter.mt5
    adapter.mt5 = stub
    try:
        assert adapter.MT5Client(broker_timestamp_offset_seconds=10800).clock.manual_offset_hours == 3.0
        assert adapter.MT5Client(broker_utc_offset_hours=2.0,
                                 broker_timestamp_offset_seconds=10800).clock.manual_offset_hours == 2.0   # hours win
    finally:
        adapter.mt5 = original


def test_fakeout_text_refuses_to_invent_a_score():
    """Ported with cards.fakeout_text: an insufficient sample must say so, never produce a number."""
    from xau_mt5_bot.cards import fakeout_text

    thin = fakeout_text({"analysis": {"historical_pattern_reliability": {"status": "INSUFFICIENT", "samples": 4}}})
    assert "UNAVAILABLE" in thin and "n=4" in thin and "No invented score" in thin

    solid = fakeout_text({"analysis": {"historical_pattern_reliability": {
        "status": "SUFFICIENT_SAMPLE", "samples": 40, "fakeout_rate": 0.32,
        "confidence_interval_fakeout": [0.21, 0.45], "comparison_level": "LONDON/DEMAND_OB"}}})
    assert "32.0/100" in solid and "n=40" in solid
    assert "21.0–45.0%" in solid and "LONDON/DEMAND_OB" in solid
    assert "not a current-trade probability" in solid


def test_next_pattern_text_is_conditional_never_a_prediction():
    """Ported with cards.next_pattern_text."""
    from xau_mt5_bot.cards import next_pattern_text

    idle = next_pattern_text({"pa_side": None, "zones": []})
    assert "No directional setup yet" in idle

    live = next_pattern_text({"pa_side": "LONG", "zones": [
        {"side": "LONG", "kind": "DEMAND_OB", "low": 2496.0, "high": 2499.0}]})
    assert "2496.00–2499.00" in live and "DEMAND_OB" in live
    assert "M5 close below 2496.00 invalidates" in live
    assert "not a prediction" in live


def test_clock_command_falls_back_to_the_heartbeat(tmp_path: Path):
    """Ported: with no trace on the snapshot, !clock still answers from data/heartbeat.json."""
    import json
    import shutil
    from xau_mt5_bot.discord_bot import BotState, dispatch

    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    (tmp_path / "data" / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "heartbeat.json").write_text(json.dumps({
        "ts_epoch": 1, "broker_clock": {"offset_hours": 3.0, "residual_seconds": 2.0, "raw_delta_seconds": 10802.0,
                                        "source": "auto", "server": "Broker-Demo03", "confident": True,
                                        "measured_at": "2026-09-07T05:00:00+00:00"}}), encoding="utf-8")
    reply = dispatch(BotState(tmp_path), "!clock")
    assert "UTC+3" in reply and "Broker-Demo03" in reply and "Broker server time:" in reply


def test_status_mode_off_sends_nothing(monkeypatch):
    """Ported: discord_status_mode=off must suppress the periodic push entirely."""
    from xau_mt5_bot.notify import Discord

    sent: list = []
    discord = Discord("NOPE", 0, 0, status_mode="off")
    discord.url = "https://example.invalid/hook"
    monkeypatch.setattr(discord, "send", lambda text="", embed=None: sent.append(embed or text) or True)

    class _Snap:
        class decision:
            action = type("A", (), {"value": "WAIT"})()
        entry_state = type("E", (), {"value": "INSIDE"})()
        pa_side = "LONG"
        session = type("S", (), {"value": "ASIA"})()
        go_status = "NO-GO"

    discord.on_snapshot(_Snap(), "UTC")
    discord.on_snapshot(_Snap(), "UTC")
    assert sent == []
