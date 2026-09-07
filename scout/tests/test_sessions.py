from datetime import UTC, date, datetime

from xau_mt5_bot.sessions import SessionEngine


def test_london_open_is_dst_aware(config):
    engine = SessionEngine(config.sessions)
    winter = [e for e in engine.boundaries_for_utc_day(date(2026, 1, 15)) if e.session.value == "LONDON"][0]
    summer = [e for e in engine.boundaries_for_utc_day(date(2026, 7, 15)) if e.session.value == "LONDON"][0]
    assert winter.timestamp.hour == 8
    assert summer.timestamp.hour == 7


def test_new_york_open_is_dst_aware(config):
    engine = SessionEngine(config.sessions)
    winter = [e for e in engine.boundaries_for_utc_day(date(2026, 1, 15)) if e.session.value == "NEW_YORK" and e.kind == "START"][0]
    summer = [e for e in engine.boundaries_for_utc_day(date(2026, 7, 15)) if e.session.value == "NEW_YORK" and e.kind == "START"][0]
    assert winter.timestamp.hour == 13
    assert summer.timestamp.hour == 12


def test_broker_trading_date_rolls_at_new_york_1700(config):
    engine = SessionEngine(config.sessions)
    assert engine.broker_trading_date(datetime(2026, 7, 6, 20, 59, tzinfo=UTC)) == date(2026, 7, 6)
    assert engine.broker_trading_date(datetime(2026, 7, 6, 21, 0, tzinfo=UTC)) == date(2026, 7, 7)

