"""v3.3.0 — broker clock offset.

The live failure this covers: a UTC+3 MT5 server made every tick look 10 799 s in the future,
the clock guard blocked every order, and every bar stamp was three hours out.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import xau_mt5_bot.mt5_client as adapter
from conftest import FakeClient, bars
from test_mt5_adapter_contract import MockMT5
from test_v18_features import _Logger
from xau_mt5_bot.config import BotConfig, SafetyConfig, load_config
from xau_mt5_bot.engine import TradingEngine
from xau_mt5_bot.features import freshness_age_seconds
from xau_mt5_bot.models import SessionName
from xau_mt5_bot.mt5_client import BrokerClock, broker_epoch_to_utc
from xau_mt5_bot.scouts import ScoutManager
from xau_mt5_bot.sessions import SessionEngine
from xau_mt5_bot.telemetry import Telemetry

ROOT = Path(__file__).resolve().parents[1]
BROKER_HOURS = 3.0                      # the live broker in the report: MT5 server on UTC+3


@pytest.fixture
def config(tmp_path) -> BotConfig:
    """Overrides the shared fixture: same shipped config.yaml, state isolated from the repo's data/."""
    cfg = load_config(ROOT / "config.yaml")
    cfg.safety.allow_scout_orders = True
    cfg.project_dir = str(tmp_path)
    cfg.logging.sqlite_path = str(tmp_path / "logs" / "t.sqlite3")
    cfg.logging.jsonl_path = str(tmp_path / "logs" / "a.jsonl")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return cfg


def _engine(client, config) -> TradingEngine:
    return TradingEngine(client, config, _Logger())


# ---- (a) a UTC+3 broker is detected, not treated as a fault ------------------------------------------
def test_offset_is_detected_residual_is_tiny_and_orders_are_not_blocked(config):
    client = FakeClient(broker_offset_hours=BROKER_HOURS)
    engine = _engine(client, config)
    engine.cycle_now = client.tick.time
    engine._validate_clock(client.tick.time)

    assert client.clock.offset_hours == 3.0
    assert client.clock.source == "auto"
    assert abs(client.clock.residual_seconds) < 5
    assert abs(engine.clock_skew) < 5                        # what the guard sees is the residual, not the timezone
    assert engine.clock_ok
    allowed, why = engine.orders_allowed()
    assert allowed, why


def test_scout_pair_opens_on_a_utc_plus_three_broker(config):
    client = FakeClient(broker_offset_hours=BROKER_HOURS)
    engine = _engine(client, config)
    engine._validate_clock(client.tick.time)
    engine.cycle_now = client.tick.time

    manager = ScoutManager(client, config)
    manager.orders_ok = engine.orders_allowed
    result = manager.open_session(SessionName.ASIA, client.tick.time)

    assert result.success, result.message
    assert len(client.positions("XAUUSD", config.magic.scout_asia)) == 2


def test_tick_handed_to_the_bot_is_true_utc_not_broker_wall_clock():
    client = FakeClient(broker_offset_hours=BROKER_HOURS)
    raw = datetime.fromtimestamp(client.raw_tick_epoch(), tz=UTC)      # what MetaTrader5 returns
    assert raw - client.tick.time == timedelta(hours=3)
    assert client.get_tick("XAUUSD").time == client.tick.time          # what the bot sees


# ---- (b) a genuine skew on top of the offset still blocks --------------------------------------------
def test_twenty_five_minute_skew_after_the_offset_blocks_orders(config):
    client = FakeClient(broker_offset_hours=BROKER_HOURS)
    engine = _engine(client, config)
    engine.cycle_now = client.tick.time
    engine._validate_clock(client.tick.time)                           # timezone detected while the clock is sane
    assert engine.clock_ok

    client.extra_skew_seconds = 25 * 60                                # the PC clock now drifts 25 minutes
    client.clock.measured_at = client.tick.time - timedelta(hours=2)   # allow the hourly re-measurement
    engine._validate_clock(client.tick.time)

    assert client.clock.offset_hours == 3.0                            # the timezone is NOT re-rounded to absorb the fault
    assert engine.clock_skew == pytest.approx(1500, abs=2)
    assert not engine.clock_ok
    allowed, why = engine.orders_allowed()
    assert not allowed
    assert "clock skew" in why and "1500s" in why and "600s" in why
    assert "+3.0h" in why                                              # the message says what the bot detected

    manager = ScoutManager(client, config)
    manager.orders_ok = engine.orders_allowed
    result = manager.open_session(SessionName.ASIA, client.tick.time)
    assert not result.success and "clock skew" in result.message


def test_a_dst_step_is_adopted_but_a_random_drift_is_not():
    clock = BrokerClock()
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    clock.measure((now + timedelta(hours=3)).timestamp(), now)
    assert clock.offset_hours == 3.0

    clock.measure((now + timedelta(hours=2)).timestamp(), now)          # DST: exactly one hour
    assert clock.offset_hours == 2.0 and abs(clock.residual_seconds) < 1

    clock.measure((now + timedelta(hours=2, minutes=25)).timestamp(), now)   # drift: not a DST step
    assert clock.offset_hours == 2.0 and clock.residual_seconds == pytest.approx(1500, abs=1)


# ---- (c) bar times reach the analysis in true UTC ------------------------------------------------------
class _OffsetBarClient(FakeClient):
    """Broker-time bars, converted by the client exactly as MT5Client does."""

    def __init__(self, frames: dict[str, pd.DataFrame], **kwargs) -> None:
        super().__init__(**kwargs)
        self.bar_frames = dict(frames)


def _m1_frame(end: datetime, count: int = 90) -> pd.DataFrame:
    start = (end - timedelta(minutes=count - 1)).strftime("%Y-%m-%d %H:%M")
    return bars(start, [(2500.0, 2500.5, 2499.5, 2500.2)] * count)


def test_history_bar_times_are_converted_so_sessions_and_freshness_are_correct(config):
    now = datetime(2026, 9, 7, 6, 30, tzinfo=UTC)                       # 15:30 Tokyo → ASIA; +3 h would read as LONDON
    frame = _m1_frame(now)
    client = _OffsetBarClient({"M1": frame}, broker_offset_hours=BROKER_HOURS)
    client.tick = type(client.tick)(now, 2500.0, 2500.2)

    served = client.get_bars("XAUUSD", "M1", 90)
    raw = client.raw_bars("M1")

    assert pd.Timestamp(raw.time.iloc[-1]) - pd.Timestamp(served.time.iloc[-1]) == pd.Timedelta(hours=3)
    assert pd.Timestamp(served.time.iloc[-1]) == pd.Timestamp(frame.time.iloc[-1])

    sessions = SessionEngine(config.sessions)
    assert sessions.session_at(pd.Timestamp(served.time.iloc[-1]).to_pydatetime()) == SessionName.ASIA
    assert sessions.session_at(pd.Timestamp(raw.time.iloc[-1]).to_pydatetime()) == SessionName.LONDON   # the bug being fixed

    assert freshness_age_seconds(served, pd.Timestamp(now)) == 0.0
    assert freshness_age_seconds(raw, pd.Timestamp(now)) == 0.0          # future bars clamp to 0 …
    assert pd.Timestamp(raw.time.iloc[-1]) > pd.Timestamp(now)           # … while actually being 3 h in the future


def test_position_open_time_is_converted_through_the_same_clock():
    client = FakeClient(broker_offset_hours=BROKER_HOURS)
    client.measure_broker_clock()
    epoch = (client.tick.time + timedelta(hours=BROKER_HOURS)).timestamp()   # broker-stamped position.time
    assert broker_epoch_to_utc(client, epoch) == client.tick.time


# ---- (d) a manual override wins over auto-detection ---------------------------------------------------
def test_manual_override_wins_over_auto_detect(config):
    client = FakeClient(broker_offset_hours=BROKER_HOURS, manual_offset_hours=2.0)
    engine = _engine(client, config)
    engine._validate_clock(client.tick.time)

    assert client.clock.offset_hours == 2.0                              # the configured value, not the detected 3.0
    assert client.clock.source == "manual"
    assert engine.clock_skew == pytest.approx(3600, abs=2)               # the hour it was told to ignore shows up as skew
    assert not engine.clock_ok


def test_a_correct_manual_override_leaves_no_residual(config):
    client = FakeClient(broker_offset_hours=BROKER_HOURS, manual_offset_hours=BROKER_HOURS)
    engine = _engine(client, config)
    engine._validate_clock(client.tick.time)
    assert client.clock.source == "manual" and abs(engine.clock_skew) < 1 and engine.clock_ok


def test_config_accepts_null_and_half_hours_but_rejects_other_values():
    assert SafetyConfig().broker_utc_offset_hours is None
    assert SafetyConfig(broker_utc_offset_hours=2.5).broker_utc_offset_hours == 2.5
    assert SafetyConfig(broker_utc_offset_hours=-5).broker_utc_offset_hours == -5
    with pytest.raises(ValueError):
        SafetyConfig(broker_utc_offset_hours=0.7)
    with pytest.raises(ValueError):
        SafetyConfig(broker_utc_offset_hours=25)
    assert load_config(ROOT / "config.yaml").safety.broker_utc_offset_hours is None     # shipped default = auto


# ---- re-measurement policy, reporting surfaces --------------------------------------------------------
def test_offset_is_remeasured_at_most_once_per_hour():
    client = FakeClient(broker_offset_hours=BROKER_HOURS)
    client.refresh_broker_clock()
    assert client.clock.measurements == 1
    client.refresh_broker_clock(); client.get_tick("XAUUSD")
    assert client.clock.measurements == 1
    client.clock.measured_at = client.tick.time - timedelta(hours=1, seconds=1)
    client.refresh_broker_clock()
    assert client.clock.measurements == 2


def test_mt5_client_validate_detects_the_offset_instead_of_calling_the_tick_stale(monkeypatch):
    fake = MockMT5()
    fake.initialize = lambda **kw: True
    fake.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    fake.symbol_info = lambda symbol: SimpleNamespace(visible=True, volume_min=0.01, volume_step=0.01, volume_max=100.0,
                                                      trade_mode=4, point=.01, trade_stops_level=0, trade_freeze_level=0,
                                                      filling_mode=2)
    fake.symbol_info_tick = lambda symbol: SimpleNamespace(
        time=int((datetime.now(UTC) + timedelta(hours=BROKER_HOURS)).timestamp()), bid=2500.0, ask=2500.2)
    monkeypatch.setattr(adapter, "mt5", fake)

    client = adapter.MT5Client(None, symbol="XAUUSD")
    validation = client.initialize()

    assert validation["broker_utc_offset_hours"] == 3.0
    assert validation["broker_clock_source"] == "auto"
    assert abs(validation["broker_clock_residual_seconds"]) < 5
    assert validation["tick_age_seconds"] < 5                       # would have been ~10 800 s before v3.3.0
    assert client.get_tick("XAUUSD").time <= datetime.now(UTC)


def test_mt5_client_bars_are_shifted_back_to_utc(monkeypatch):
    fake = MockMT5()
    fake.terminal_info = lambda: SimpleNamespace(connected=True, trade_allowed=True)
    fake.symbol_info = lambda symbol: SimpleNamespace(visible=True, volume_min=0.01, volume_step=0.01, volume_max=100.0,
                                                      trade_mode=4, point=.01, trade_stops_level=0, trade_freeze_level=0,
                                                      filling_mode=2)
    base = datetime(2026, 9, 7, 1, 0, tzinfo=UTC)
    broker_epochs = [int((base + timedelta(minutes=i, hours=BROKER_HOURS)).timestamp()) for i in range(3)]
    fake.copy_rates_from_pos = lambda symbol, tf, start, count: [
        {"time": e, "open": 2500.0, "high": 2501.0, "low": 2499.0, "close": 2500.5, "tick_volume": 10} for e in broker_epochs]
    monkeypatch.setattr(adapter, "mt5", fake)

    client = adapter.MT5Client(None, symbol="XAUUSD")
    client.clock = BrokerClock(offset=timedelta(hours=BROKER_HOURS), measured_at=base)
    frame = client.get_bars("XAUUSD", "M1", 3)

    assert list(pd.to_datetime(frame.time, utc=True)) == [base, base + timedelta(minutes=1), base + timedelta(minutes=2)]


def test_engine_reports_the_offset_once_and_exposes_it_to_cards_and_heartbeat(config, tmp_path):
    client = FakeClient(broker_offset_hours=BROKER_HOURS)
    logger = _Logger()
    engine = TradingEngine(client, config, logger)
    engine._validate_clock(client.tick.time)
    engine._validate_clock(client.tick.time + timedelta(seconds=5))

    offset_events = [payload for kind, payload in logger.events if kind == "broker_clock_offset"]
    assert len(offset_events) == 1
    assert offset_events[0]["broker_utc_offset_hours"] == 3.0
    assert offset_events[0]["source"] == "auto"

    view = engine.broker_clock()
    assert view["broker_utc_offset_hours"] == 3.0 and view["clock_ok"] and view["max_clock_skew_seconds"] == 600

    telemetry = Telemetry(heartbeat_path=str(tmp_path / "heartbeat.json"))
    telemetry.record_broker_clock(view)
    assert telemetry.payload()["broker_utc_offset_hours"] == 3.0
    assert telemetry.payload()["broker_clock_source"] == "auto"
