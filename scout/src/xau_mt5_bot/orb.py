from __future__ import annotations

from datetime import timedelta

import pandas as pd

from .models import LiquidityLevel, SessionName
from .sessions import SessionEngine


def opening_ranges(
    m1: pd.DataFrame,
    sessions: SessionEngine,
    durations: tuple[int, ...] = (5, 15, 30, 60),
) -> list[LiquidityLevel]:
    if m1.empty:
        return []
    data = m1.copy()
    data["time"] = pd.to_datetime(data.time, utc=True)
    first, last = data.time.min(), data.time.max()
    starts = []
    for day in pd.date_range(first.floor("D"), last.ceil("D"), freq="D", tz="UTC"):
        starts.extend(event for event in sessions.boundaries_for_utc_day(day.date()) if event.kind == "START")
    levels: list[LiquidityLevel] = []
    for start in starts:
        if start.session == SessionName.CLOSED:
            continue
        for minutes in durations:
            valid_from = start.timestamp + timedelta(minutes=minutes)
            if valid_from > last.to_pydatetime():
                continue
            sample = data[(data.time >= start.timestamp) & (data.time < valid_from)]
            if sample.empty:
                continue
            name = start.session.value
            levels.extend(
                [
                    LiquidityLevel(float(sample.high.max()), f"ORB{minutes}_{name}_HIGH", "M1", valid_from, 2.5),
                    LiquidityLevel(float(sample.low.min()), f"ORB{minutes}_{name}_LOW", "M1", valid_from, 2.5),
                ]
            )
    return levels

