from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd

from .models import LiquidityLevel, SessionName, SweepEvent
from .sessions import SessionEngine


HIGH_KINDS = {"PDH", "PWH", "PMH", "ASIA_HIGH", "LONDON_HIGH", "NEW_YORK_HIGH", "SWING_HIGH", "EQUAL_HIGH", "ORB_HIGH"}
NEUTRAL_PREFIXES = ("ROUND_", "DAILY_OPEN", "PDC", "VWAP", "WEEKLY_OPEN", "MONTHLY_OPEN")   # item 4: sweepable from both sides


def _period_levels(frame: pd.DataFrame, period: str, high_kind: str, low_kind: str, strength: float) -> list[LiquidityLevel]:
    if frame.empty:
        return []
    data = frame.copy().set_index("time")
    grouped = list(data.groupby(pd.Grouper(freq=period, label="left", closed="left")))
    levels: list[LiquidityLevel] = []
    offset = pd.tseries.frequencies.to_offset(period)
    for start, group in grouped:
        if group.empty:
            continue
        valid = (pd.Timestamp(start) + offset).to_pydatetime()
        levels.extend(
            [
                LiquidityLevel(float(group.high.max()), high_kind, period, valid, strength),
                LiquidityLevel(float(group.low.min()), low_kind, period, valid, strength),
            ]
        )
    return levels


def previous_period_levels(d1: pd.DataFrame) -> list[LiquidityLevel]:
    """PDH/PDL from NATIVE D1 bars (broker day boundary); weekly/monthly grouped by broker trading date."""
    data = d1.copy()
    data["time"] = pd.to_datetime(data["time"], utc=True)
    levels: list[LiquidityLevel] = []
    spacing = data.time.diff().median() if len(data) > 1 else pd.Timedelta(days=1)
    for i in range(len(data)):                           # a D1 bar becomes PDH/PDL when the next bar opens
        bar = data.iloc[i]
        valid = pd.Timestamp(data.iloc[i + 1].time if i + 1 < len(data) else bar.time + spacing).to_pydatetime()
        levels += [LiquidityLevel(float(bar.high), "PDH", "D1", valid, 4.0), LiquidityLevel(float(bar.low), "PDL", "D1", valid, 4.0),
                   LiquidityLevel(float(bar.close), "PDC", "D1", valid, 2.0)]
    tdates = pd.to_datetime([SessionEngine.broker_trading_date(t.to_pydatetime()) for t in data.time])
    data["week"] = tdates.to_period("W-SUN").start_time; data["month"] = tdates.to_period("M").start_time
    for col, hk, lk, st in (("week", "PWH", "PWL", 5.0), ("month", "PMH", "PML", 6.0)):
        groups = list(data.groupby(col, sort=True))
        for (_, g), (_, ng) in zip(groups, groups[1:]):
            valid = pd.Timestamp(ng.iloc[0].time).to_pydatetime()
            levels += [LiquidityLevel(float(g.high.max()), hk, col.upper(), valid, st), LiquidityLevel(float(g.low.min()), lk, col.upper(), valid, st)]
    data["year"] = tdates.year
    year_groups = list(data.groupby("year", sort=True))
    for (_, group), (_, next_group) in zip(year_groups, year_groups[1:]):
        valid = pd.Timestamp(next_group.iloc[0].time).to_pydatetime()
        levels += [LiquidityLevel(float(group.high.max()), "PYH", "YEAR", valid, 7.0),
                   LiquidityLevel(float(group.low.min()), "PYL", "YEAR", valid, 7.0)]
    return levels


def swing_and_equal_levels(pivots, atr: float, tolerance_atr: float = 0.10, recent: int = 12) -> list[LiquidityLevel]:
    """Recent confirmed swings as liquidity pools; two swings within tolerance = EQUAL_HIGH/LOW (valid when the 2nd confirms)."""
    levels: list[LiquidityLevel] = []
    for kind, name, eq in (("HIGH", "SWING_HIGH", "EQUAL_HIGH"), ("LOW", "SWING_LOW", "EQUAL_LOW")):
        pts = [p for p in pivots if p.kind == kind][-recent:]
        for p in pts:
            levels.append(LiquidityLevel(float(p.price), name, "M5", p.confirmation_timestamp, 2.0))
        for a in range(len(pts)):
            for b in range(a + 1, len(pts)):
                if abs(pts[a].price - pts[b].price) <= tolerance_atr * atr:
                    price = (pts[a].price + pts[b].price) / 2
                    valid = max(pts[a].confirmation_timestamp, pts[b].confirmation_timestamp)
                    levels.append(LiquidityLevel(round(float(price), 2), eq, "M5", valid, 3.5))
    return levels


def session_levels(frame: pd.DataFrame, sessions: SessionEngine) -> list[LiquidityLevel]:
    if frame.empty:
        return []
    data = frame.copy()
    data["session"] = [sessions.session_at(pd.Timestamp(t).to_pydatetime()) for t in data.time]
    data["segment"] = (data.session != data.session.shift()).cumsum()
    levels: list[LiquidityLevel] = []
    groups = list(data.groupby("segment", sort=True))
    for (_, group), (_, next_group) in zip(groups, groups[1:]):
        name = group.iloc[0].session
        if name == SessionName.CLOSED:
            continue
        valid_from = pd.Timestamp(next_group.iloc[0].time).to_pydatetime()
        prefix = name.value
        levels.extend(
            [
                LiquidityLevel(float(group.high.max()), f"{prefix}_HIGH", "SESSION", valid_from, 3.0),
                LiquidityLevel(float(group.low.min()), f"{prefix}_LOW", "SESSION", valid_from, 3.0),
            ]
        )
    return levels


def round_number_levels(price: float, now: datetime, increments: tuple[int, ...] = (1, 5, 10, 20, 25, 50, 100),
                        valid_from: datetime | None = None) -> list[LiquidityLevel]:
    """Round numbers are timeless: valid_from defaults to the broker day start so closed candles can sweep them (item 3)."""
    levels: dict[float, LiquidityLevel] = {}
    since = (valid_from or now - timedelta(days=1)).astimezone(UTC)
    for increment in increments:
        anchor = round(price / increment) * increment
        for value in (anchor - increment, anchor, anchor + increment):
            levels[value] = LiquidityLevel(float(value), f"ROUND_{increment}", "PRICE", since, 1.0 + increment / 100)
    return list(levels.values())


def is_neutral(kind: str) -> bool:
    return kind.startswith(NEUTRAL_PREFIXES)


def detect_sweeps(
    frame: pd.DataFrame,
    levels: list[LiquidityLevel],
    reclaim_bars: int = 3,
    atr_column: str = "atr",
    max_age_bars: int = 48,
) -> list[SweepEvent]:
    """Vectorised: wick beyond the level, close back within reclaim_bars. Neutral levels are scanned from both sides."""
    events: list[SweepEvent] = []
    if frame.empty:
        return events
    times = pd.to_datetime(frame.time, utc=True).values
    highs, lows, closes = frame.high.values.astype(float), frame.low.values.astype(float), frame.close.values.astype(float)
    atrs = frame[atr_column].values.astype(float) if atr_column in frame else np.zeros(len(frame))
    n = len(frame); seen: set[tuple] = set()
    jobs = []
    for level in levels:
        if is_neutral(level.kind):
            jobs += [(level, True), (level, False)]
        else:
            jobs.append((level, level.kind in HIGH_KINDS or level.kind.endswith("_HIGH")))
    for level, high_level in jobs:
        start = int(np.searchsorted(times, np.datetime64(pd.Timestamp(level.valid_from).tz_convert("UTC").tz_localize(None))))
        if start >= n:
            continue
        crossed = np.where((highs[start:] > level.price) if high_level else (lows[start:] < level.price))[0] + start
        pos = 0
        while pos < len(crossed):
            i = int(crossed[pos])
            window = closes[i : i + reclaim_bars + 1]
            back = np.where((window < level.price) if high_level else (window > level.price))[0]
            if len(back) == 0:
                pos += 1; continue
            r = i + int(back[0])
            extreme = float(highs[i] if high_level else lows[i]); distance = abs(extreme - level.price)
            atr = float(atrs[i]) if not np.isnan(atrs[i]) else 0.0
            key = (level.kind, round(float(level.price), 8), "BEARISH" if high_level else "BULLISH", times[i])
            if key not in seen:
                seen.add(key)
                events.append(SweepEvent(level.kind, "BEARISH" if high_level else "BULLISH", level.price, extreme,
                                         pd.Timestamp(times[i], tz="UTC").to_pydatetime(), pd.Timestamp(times[r], tz="UTC").to_pydatetime(),
                                         distance, distance / atr if atr > 0 else 0.0, n - 1 - i, (n - 1 - i) <= max_age_bars))
            pos = int(np.searchsorted(crossed, r + 1))
    return sorted(events, key=lambda e: e.sweep_time)

def current_daily_levels(m1: pd.DataFrame, d1: pd.DataFrame | None = None) -> list[LiquidityLevel]:
    """Daily open / today's H-L from the broker day = open time of the last native D1 bar."""
    if m1.empty:
        return []
    last = pd.Timestamp(m1.iloc[-1].time)
    start = pd.Timestamp(d1.iloc[-1].time) if d1 is not None and len(d1) else last.floor("D")
    today = m1[pd.to_datetime(m1.time, utc=True) >= start]
    if today.empty:
        return []
    valid = pd.Timestamp(today.iloc[0].time).to_pydatetime()
    return [
        LiquidityLevel(float(today.iloc[0].open), "DAILY_OPEN", "D1", valid, 3.0),
        LiquidityLevel(float(today.high.max()), "TODAY_HIGH", "M1", valid, 2.0),
        LiquidityLevel(float(today.low.min()), "TODAY_LOW", "M1", valid, 2.0),
    ]
