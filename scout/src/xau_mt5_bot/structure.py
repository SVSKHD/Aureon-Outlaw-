from __future__ import annotations

import numpy as np
import pandas as pd

from .models import Pivot, StructureResult, StructureState


def detect_pivots(frame: pd.DataFrame, left: int = 2, right: int = 2) -> list[Pivot]:
    """Strict fractal pivots: bar i is a HIGH when its high is the unique maximum of bars [i-left, i+right]
    (mirror for LOW). Vectorised in v1.9.0 — identical output to the per-row version, ~50x faster."""
    pivots: list[Pivot] = []
    n = len(frame)
    if n < left + right + 1:
        return pivots
    highs = frame["high"].to_numpy(dtype=float); lows = frame["low"].to_numpy(dtype=float)
    times = pd.to_datetime(frame["time"]).to_numpy()
    width = left + right + 1
    high_windows = np.lib.stride_tricks.sliding_window_view(highs, width)   # row k covers bars [k, k+width)
    low_windows = np.lib.stride_tricks.sliding_window_view(lows, width)
    centre_high = highs[left:n - right]; centre_low = lows[left:n - right]
    is_high = (centre_high == high_windows.max(axis=1)) & ((high_windows == centre_high[:, None]).sum(axis=1) == 1)
    is_low = (centre_low == low_windows.min(axis=1)) & ((low_windows == centre_low[:, None]).sum(axis=1) == 1)
    for offset in np.flatnonzero(is_high | is_low):
        i = int(offset + left)
        event_time = pd.Timestamp(times[i]).to_pydatetime()
        confirmation_time = pd.Timestamp(times[i + right]).to_pydatetime()
        if is_high[offset]:
            pivots.append(Pivot(event_time, confirmation_time, float(highs[i]), "HIGH", i))
        if is_low[offset]:
            pivots.append(Pivot(event_time, confirmation_time, float(lows[i]), "LOW", i))
    return sorted(pivots, key=lambda item: (item.timestamp, item.kind))


def analyze_structure(frame: pd.DataFrame, timeframe: str, left: int = 2, right: int = 2) -> StructureResult:
    pivots = detect_pivots(frame, left, right)
    highs = [pivot for pivot in pivots if pivot.kind == "HIGH"]
    lows = [pivot for pivot in pivots if pivot.kind == "LOW"]
    state = StructureState.NEUTRAL
    if len(highs) >= 2 and len(lows) >= 2:
        higher_high = highs[-1].price > highs[-2].price
        higher_low = lows[-1].price > lows[-2].price
        lower_high = highs[-1].price < highs[-2].price
        lower_low = lows[-1].price < lows[-2].price
        if higher_high and higher_low:
            state = StructureState.BULLISH
        elif lower_high and lower_low:
            state = StructureState.BEARISH
        elif (higher_high and lower_low) or (lower_high and higher_low):
            state = StructureState.TRANSITION
        else:
            state = StructureState.RANGE

    events: list[dict[str, object]] = []
    previous_state = StructureState.NEUTRAL
    by_confirmation: dict[object, list[Pivot]] = {}
    for pivot in pivots:
        by_confirmation.setdefault(pivot.confirmation_timestamp, []).append(pivot)
    last_high: Pivot | None = None; last_low: Pivot | None = None      # newest confirmed pivot with index < i (v1.9.0: O(n))
    pending: list[Pivot] = []
    closes = frame["close"].to_numpy(dtype=float)
    times = pd.to_datetime(frame["time"]).to_numpy()
    for i in range(len(frame)):
        now = pd.Timestamp(times[i]).to_pydatetime()
        pending.extend(by_confirmation.get(now, ()))
        still_pending: list[Pivot] = []
        for pivot in pending:
            if pivot.index < i:
                if pivot.kind == "HIGH": last_high = pivot
                else: last_low = pivot
            else:
                still_pending.append(pivot)
        pending = still_pending
        close = float(closes[i])
        if last_high is not None and close > last_high.price:
            event = "Bullish CHoCH" if previous_state == StructureState.BEARISH else "Bullish BOS"
            if not events or events[-1]["level"] != last_high.price or events[-1]["event"] != event:
                events.append({"event": event, "timestamp": now, "level": last_high.price})
            previous_state = StructureState.BULLISH
        if last_low is not None and close < last_low.price:
            event = "Bearish CHoCH" if previous_state == StructureState.BULLISH else "Bearish BOS"
            if not events or events[-1]["level"] != last_low.price or events[-1]["event"] != event:
                events.append({"event": event, "timestamp": now, "level": last_low.price})
            previous_state = StructureState.BEARISH
    # retest / failed classification for each break
    for event in events:
        after = frame[pd.to_datetime(frame.time, utc=True) > pd.Timestamp(event["timestamp"])]
        if after.empty:
            event["status"] = "fresh"; continue
        bullish = str(event["event"]).startswith("Bullish"); level = float(event["level"])
        touched = bool((after.low <= level).any()) if bullish else bool((after.high >= level).any())
        failed = bool((after.close < level).any()) if bullish else bool((after.close > level).any())
        event["status"] = "failed" if failed else "retested" if touched else "unretested"
        if failed:
            event["name"] = f"Failed {event['event']}"
    return StructureResult(timeframe, state, pivots, events)
