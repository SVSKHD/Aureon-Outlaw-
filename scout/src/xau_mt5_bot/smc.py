from __future__ import annotations

import pandas as pd

from .features import with_candle_features
from .models import Side, StructureResult, Zone


def detect_fvgs(frame: pd.DataFrame, atr_period: int = 14, min_atr: float = 0.10) -> list[Zone]:
    data = with_candle_features(frame, atr_period)
    zones: list[Zone] = []
    for i in range(2, len(data)):
        first, middle, third = data.iloc[i - 2], data.iloc[i - 1], data.iloc[i]
        atr_creation = float(middle.atr) if pd.notna(middle.atr) else 0.0
        if atr_creation <= 0:
            continue
        if float(third.low) > float(first.high):
            low, high, side = float(first.high), float(third.low), Side.LONG
        elif float(third.high) < float(first.low):
            low, high, side = float(third.high), float(first.low), Side.SHORT
        else:
            continue
        if (high - low) / atr_creation < min_atr:
            continue
        later = data.iloc[i + 1 :]
        status = "untouched"
        if not later.empty:
            if side == Side.LONG:
                penetration = (high - later.low.min()) / (high - low)
            else:
                penetration = (later.high.max() - low) / (high - low)
            if penetration >= 1:
                status = "fully mitigated"
                through = later[(later.close < low)] if side == Side.LONG else later[(later.close > high)]
                if not through.empty:                                                          # item 30: inversion FVG
                    flip = Side.SHORT if side == Side.LONG else Side.LONG
                    t_inv = pd.Timestamp(through.iloc[0].time); after = later[pd.to_datetime(later.time, utc=True) > t_inv]
                    if flip == Side.SHORT:
                        inv_status = "invalidated" if (after.close > high).any() else "retested" if (after.high >= low).any() else "created"
                    else:
                        inv_status = "invalidated" if (after.close < low).any() else "retested" if (after.low <= high).any() else "created"
                    if inv_status != "invalidated":
                        zones.append(Zone(low, high, f"{flip.value}_INVERSION_FVG", flip, pd.Timestamp(middle.time).to_pydatetime(), t_inv.to_pydatetime(), "M5", 6.0, inv_status))
            elif penetration >= 0.75:
                status = "75% filled"
            elif penetration >= 0.50:
                status = "50% filled"
            elif penetration >= 0.25:
                status = "25% filled"
        zones.append(
            Zone(low, high, f"{side.value}_FVG", side, pd.Timestamp(middle.time).to_pydatetime(),
                 pd.Timestamp(third.time).to_pydatetime(), "M5", 7.0, status)
        )
    return zones


def detect_order_blocks(
    frame: pd.DataFrame,
    structure: StructureResult,
    atr_period: int = 14,
    displacement_atr: float = 1.2,
) -> list[Zone]:
    data = with_candle_features(frame, atr_period)
    zones: list[Zone] = []
    for event in structure.events:
        timestamp = pd.Timestamp(event["timestamp"])
        matches = data.index[data.time == timestamp].tolist()
        if not matches:
            continue
        i = matches[0]
        impulse = data.iloc[i]
        if pd.isna(impulse.atr) or float(impulse.range) < displacement_atr * float(impulse.atr) or float(impulse.body_ratio) < 0.55:
            continue
        bullish = str(event["event"]).startswith("Bullish")
        candidates = data.iloc[max(0, i - 6) : i]
        candidates = candidates[candidates.close < candidates.open] if bullish else candidates[candidates.close > candidates.open]
        if candidates.empty:
            continue
        candle = candidates.iloc[-1]
        side = Side.LONG if bullish else Side.SHORT
        later = data.iloc[i + 1 :]
        lo, hi = float(candle.low), float(candle.high); depth = max(hi - lo, 1e-9)
        status = _ob_state(later, lo, hi, depth, bullish)                                     # item 29
        zones.append(Zone(lo, hi, f"{side.value}_ORDER_BLOCK", side, pd.Timestamp(candle.time).to_pydatetime(), timestamp.to_pydatetime(), "M5", 9.0, status))
        if status == "broken":                                                                 # item 28: breaker lifecycle
            flip = Side.SHORT if bullish else Side.LONG
            brk = later[(later.close < lo) if bullish else (later.close > hi)]
            break_time = pd.Timestamp(brk.iloc[0].time); after = later[pd.to_datetime(later.time, utc=True) > break_time]
            b_status = _breaker_state(after, lo, hi, flip)
            if b_status != "invalidated":
                zones.append(Zone(lo, hi, f"{flip.value}_BREAKER", flip, pd.Timestamp(candle.time).to_pydatetime(), break_time.to_pydatetime(), "M5", 8.0, b_status))
    return zones


def _ob_state(later: pd.DataFrame, lo: float, hi: float, depth: float, bullish: bool) -> str:
    if later.empty: return "untouched"
    if bullish:
        if (later.close < lo).any(): return "broken"
        pen = (hi - later.low.min()) / depth
        rejected = pen > 0 and float(later.close.iloc[-1]) > hi and (later.low <= hi).any()
    else:
        if (later.close > hi).any(): return "broken"
        pen = (later.high.max() - lo) / depth
        rejected = pen > 0 and float(later.close.iloc[-1]) < lo and (later.high >= lo).any()
    if pen <= 0: return "untouched"
    if pen >= 1: return "fully mitigated"
    if rejected: return "rejected"
    return "75% mitigated" if pen >= 0.75 else "50% mitigated" if pen >= 0.5 else "25% mitigated"


def _breaker_state(after: pd.DataFrame, lo: float, hi: float, side: Side) -> str:
    """created → retested (price returned to the block) → rejected (closed away) / mitigated / invalidated (closed through)."""
    if after.empty: return "created"
    if side == Side.SHORT:      # broken bullish OB now acts as resistance
        if (after.close > hi).any(): return "invalidated"
        touched = (after.high >= lo).any()
        if not touched: return "created"
        return "rejected" if float(after.close.iloc[-1]) < lo else "mitigated"
    if (after.close < lo).any(): return "invalidated"
    touched = (after.low <= hi).any()
    if not touched: return "created"
    return "rejected" if float(after.close.iloc[-1]) > hi else "mitigated"


def weighted_sr_zones(structures: dict[str, StructureResult], atr: float) -> list[Zone]:
    weights = {"M5": 1.0, "M15": 2.0, "H1": 3.0, "H4": 4.0}
    raw: list[tuple[float, float, object, str]] = []
    for timeframe, result in structures.items():
        for pivot in result.pivots[-20:]:
            raw.append((pivot.price, weights.get(timeframe, 1.0), pivot.timestamp, pivot.kind))
    raw.sort(key=lambda value: value[0])
    tolerance = max(atr * 0.15, 1e-6)
    clusters: list[list[tuple[float, float, object, str]]] = []
    for item in raw:
        if not clusters or abs(item[0] - sum(v[0] for v in clusters[-1]) / len(clusters[-1])) > tolerance:
            clusters.append([item])
        else:
            clusters[-1].append(item)
    zones: list[Zone] = []
    for cluster in clusters:
        weight = sum(item[1] for item in cluster)
        price = sum(item[0] * item[1] for item in cluster) / weight
        kinds = [item[3] for item in cluster]
        side = Side.SHORT if kinds.count("HIGH") >= kinds.count("LOW") else Side.LONG
        created = max(item[2] for item in cluster)
        zones.append(Zone(price - tolerance / 2, price + tolerance / 2, "RESISTANCE" if side == Side.SHORT else "SUPPORT", side, created, created, "MULTI", weight, "active"))
    return zones

