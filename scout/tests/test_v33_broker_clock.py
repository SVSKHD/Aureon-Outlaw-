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
