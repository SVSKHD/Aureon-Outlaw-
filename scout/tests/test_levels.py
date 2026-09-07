from datetime import UTC, datetime, timedelta

import pandas as pd

from conftest import bars
from xau_mt5_bot.features import with_candle_features
from xau_mt5_bot.liquidity import detect_sweeps, previous_period_levels
from xau_mt5_bot.models import LiquidityLevel
from xau_mt5_bot.orb import opening_ranges
from xau_mt5_bot.sessions import SessionEngine


def test_orb_is_not_valid_until_formation_finishes(config):
    sessions = SessionEngine(config.sessions)
    london = [e for e in sessions.boundaries_for_utc_day(datetime(2026, 7, 15).date()) if e.session.value == "LONDON"][0]
    values = [(100, 101 + i, 99, 100.5) for i in range(20)]
    frame = bars(london.timestamp.isoformat(), values)
    orb15 = [level for level in opening_ranges(frame, sessions, (15,)) if level.kind == "ORB15_LONDON_HIGH"][0]
    assert orb15.valid_from == london.timestamp + timedelta(minutes=15)
    assert orb15.price == 115.0  # minutes 0..14 only; minute 15 is excluded


def test_level_cannot_be_swept_before_valid_from_and_all_sweeps_are_kept():
    values = [
        (99, 101, 98, 99),   # before level exists
        (99, 99.5, 98.5, 99),
        (99, 101.2, 98.8, 99.7),  # sweep/reclaim 1
        (99.7, 100, 99, 99.4),
        (99.4, 99.8, 99, 99.5),
        (99.5, 101.5, 99, 100.5), # cross 2
        (100.5, 100.7, 99, 99.6), # reclaim 2
    ]
    frame = with_candle_features(bars("2026-07-15T09:58:00", values), 2)
    level = LiquidityLevel(100.0, "PDH", "D1", datetime(2026, 7, 15, 10, 0, tzinfo=UTC), 4)
    events = detect_sweeps(frame, [level], reclaim_bars=2)
    assert len(events) == 2
    assert all(event.sweep_time >= level.valid_from for event in events)


def test_latest_closed_day_becomes_previous_day_level():
    frame = bars(
        "2026-07-13T00:00:00",
        [(100, 105, 95, 101), (101, 110, 98, 108)],
        "1D",
    )
    levels = previous_period_levels(frame)
    pdh = max((level for level in levels if level.kind == "PDH"), key=lambda level: level.valid_from)
    assert pdh.price == 110
    assert pdh.valid_from == datetime(2026, 7, 15, tzinfo=UTC)
