from __future__ import annotations

from datetime import datetime
from dataclasses import replace

import pandas as pd

from .features import with_candle_features
from .models import Side, TriggerResult, Zone


def current_zone_touch_time(m1: pd.DataFrame, zone: Zone, current_price: float) -> datetime | None:
    if m1.empty or not (zone.low <= current_price <= zone.high):
        return None
    overlaps = (m1.low <= zone.high) & (m1.high >= zone.low)
    if not bool(overlaps.iloc[-1]):
        return None
    first = len(m1) - 1
    while first > 0 and bool(overlaps.iloc[first - 1]):
        first -= 1
    return pd.Timestamp(m1.iloc[first].time).to_pydatetime()


def detect_m1_trigger(
    m1_closed: pd.DataFrame,
    zone: Zone,
    side: Side,
    touch_time: datetime | None,
    atr_period: int,
    displacement_atr: float,
    expiry_bars: int,
) -> TriggerResult:
    if touch_time is None:
        return TriggerResult(False, "M1", reason="No current entry-zone visit")
    data = with_candle_features(m1_closed, atr_period)
    visit = data[pd.to_datetime(data.time, utc=True) >= pd.Timestamp(touch_time)]
    if visit.empty:
        return TriggerResult(False, "M1", touch_time=touch_time, reason="Waiting for closed M1 after touch")
    if len(visit) > expiry_bars:
        return TriggerResult(False, "M1", touch_time=touch_time, reason="Current M1 trigger expired")

    sweep_index: int | None = None
    for i in range(3, len(visit)):
        row = visit.iloc[i]
        prior = visit.iloc[i - 3 : i]
        long_sweep = float(row.low) < float(prior.low.min()) and float(row.close) > zone.low
        short_sweep = float(row.high) > float(prior.high.max()) and float(row.close) < zone.high
        if (side == Side.LONG and long_sweep) or (side == Side.SHORT and short_sweep):
            sweep_index = i
            break
    if sweep_index is None:
        return TriggerResult(False, "M1", touch_time, fresh=True, reason="Waiting for post-touch M1 sweep/rejection")

    structure_index: int | None = None
    for i in range(sweep_index + 1, len(visit)):
        row, prior = visit.iloc[i], visit.iloc[sweep_index:i]
        broken = float(row.close) > float(prior.high.max()) if side == Side.LONG else float(row.close) < float(prior.low.min())
        if broken:
            structure_index = i
            break
    if structure_index is None:
        return TriggerResult(False, "M1", touch_time, True, False, False, True, "Waiting for post-sweep M1 BOS/CHoCH")
    row = visit.iloc[structure_index]
    displacement = bool(pd.notna(row.atr) and float(row.range) >= displacement_atr * float(row.atr) and float(row.body_ratio) >= 0.55)
    return TriggerResult(
        displacement, "M1", touch_time, True, True, displacement, True,
        "Fresh sweep, structure break and displacement" if displacement else "Waiting for M1 displacement",
        trigger_bar_time=pd.Timestamp(row.time).to_pydatetime(),
    )


def detect_m5_confirmation(
    m5_closed: pd.DataFrame, zone: Zone, side: Side, touch_time: datetime | None, pivots: list | None = None,
) -> TriggerResult:
    """Break/retest of a confirmed pivot owned by this continuous zone visit."""
    if touch_time is None:
        return TriggerResult(False, "M5", reason="No current entry-zone visit")
    times = pd.to_datetime(m5_closed.time, utc=True)
    visit = m5_closed[times >= pd.Timestamp(touch_time)]
    if len(visit) < 3:
        return TriggerResult(False, "M5", touch_time, fresh=True, reason="Waiting for fresh M5 break/retest")
    kind = "HIGH" if side == Side.LONG else "LOW"
    visit_pivots = []
    for p in pivots or []:
        if p.kind != kind or pd.Timestamp(p.timestamp) < pd.Timestamp(touch_time):
            continue
        rows = m5_closed[pd.to_datetime(m5_closed.time, utc=True) == pd.Timestamp(p.timestamp)]
        if not rows.empty and float(rows.iloc[0].low) <= zone.high and float(rows.iloc[0].high) >= zone.low:
            visit_pivots.append(p)
    for i in range(1, len(visit) - 1):
        breakout, retest = visit.iloc[i], visit.iloc[i + 1]
        bt = pd.Timestamp(breakout.time)
        usable = [p for p in visit_pivots if pd.Timestamp(p.confirmation_timestamp) <= bt and pd.Timestamp(p.timestamp) < bt]
        if not usable:
            continue
        level, src = float(usable[-1].price), "confirmed pivot"
        if side == Side.LONG:
            valid = breakout.close > level and retest.low <= level and retest.close > level
        else:
            valid = breakout.close < level and retest.high >= level and retest.close < level
        if valid:
            return TriggerResult(True, "M5", touch_time, False, True, True, True, f"Fresh M5 break of {src} {level:.2f} and retest",
                                 trigger_bar_time=pd.Timestamp(retest.time).to_pydatetime())
    return TriggerResult(False, "M5", touch_time, fresh=True, reason="No fresh M5 break/retest of a confirmed pivot")


def choose_trigger(m1: TriggerResult, m5: TriggerResult) -> TriggerResult:
    if m1.confirmed:
        return m1
    if m5.confirmed:
        return m5
    return m1 if m1.touch_time is not None else m5



def carry_trigger(previous: TriggerResult | None, previous_time, current: TriggerResult, side: Side, zone: Zone,
                  price: float, atr: float, m1_closed: pd.DataFrame, grace_bars: int) -> TriggerResult:
    """A confirmed trigger stays valid for `grace_bars` closed M1 bars after price leaves the zone in the trade direction,
    as long as price has not run more than 1 ATR beyond the zone (entry missed)."""
    if current.confirmed or previous is None or not previous.confirmed or previous_time is None:
        return current
    bars_since = int((pd.to_datetime(m1_closed.time, utc=True) > pd.Timestamp(previous_time)).sum())
    beyond = (price - zone.high) if side == Side.LONG else (zone.low - price)
    if bars_since <= grace_bars and -0.1 * atr <= beyond <= 1.0 * atr:
        return replace(previous, fresh=True,
                       reason=f"Carried {previous.source} trigger ({bars_since}/{grace_bars} bars since confirmation)")
    return current
