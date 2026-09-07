from datetime import UTC, datetime

from conftest import bars
from xau_mt5_bot.models import Pivot, Side, Zone
from xau_mt5_bot.trigger import current_zone_touch_time, detect_m1_trigger, detect_m5_confirmation


def test_current_visit_does_not_reuse_old_m1_sweep():
    values = [
        (101, 101.2, 99.0, 100.8),  # old sweep in zone
        (101, 102, 100.8, 101.8),
        (103, 104, 102.5, 103.5),   # away from zone
        (103, 104, 102.5, 103.0),
        (101.2, 101.4, 100.4, 100.8), # new visit, no fresh sweep/BOS
        (100.8, 101.2, 100.5, 100.9),
    ]
    frame = bars("2026-07-15T10:00:00", values)
    zone = Zone(100, 101, "SUPPORT", Side.LONG, datetime.now(UTC), datetime.now(UTC))
    touch = current_zone_touch_time(frame, zone, 100.8)
    assert touch == frame.iloc[4].time.to_pydatetime()
    trigger = detect_m1_trigger(frame, zone, Side.LONG, touch, 2, 1.0, 12)
    assert not trigger.confirmed


def test_fresh_m5_break_and_retest_belongs_to_current_visit():
    values = [
        (100.5, 101.0, 100.0, 100.6),
        (100.6, 102.0, 100.5, 101.5),  # closes above prior high
        (101.5, 101.8, 100.9, 101.3),  # retests 101 then closes above
    ]
    frame = bars("2026-07-15T10:00:00", values, "5min")
    zone = Zone(100, 101, "SUPPORT", Side.LONG, datetime.now(UTC), datetime.now(UTC))
    pivot = Pivot(frame.iloc[0].time.to_pydatetime(), frame.iloc[0].time.to_pydatetime(), 101.0, "HIGH", 0)
    result = detect_m5_confirmation(frame, zone, Side.LONG, frame.iloc[0].time.to_pydatetime(), [pivot])
    assert result.confirmed and result.fresh
