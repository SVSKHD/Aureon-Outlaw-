"""Session VWAP with bands/states, premium/discount from a confirmed dealing range, trendlines with quality rules."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from .models import LiquidityLevel, Pivot, Side, StructureResult


def session_vwap(m1: pd.DataFrame, session_start: datetime | None, now: datetime, price: float,
                 closed_m5: pd.DataFrame | None = None) -> tuple[LiquidityLevel | None, dict[str, Any]]:
    """VWAP + 1σ bands; state = above/below/at plus reclaim / rejection / cross from the last closed M5 bars (item 33)."""
    if m1.empty or session_start is None:
        return None, {"vwap": None, "relation": "n/a", "state": "n/a"}
    d = m1[pd.to_datetime(m1.time, utc=True) >= pd.Timestamp(session_start)]
    if d.empty:
        return None, {"vwap": None, "relation": "n/a", "state": "n/a"}
    vol = d["tick_volume"].astype(float).clip(lower=1) if "tick_volume" in d else pd.Series(1.0, index=d.index)
    typical = (d.high + d.low + d.close) / 3
    vwap = float((typical * vol).sum() / vol.sum())
    dev = float(np.sqrt(((typical - vwap) ** 2 * vol).sum() / vol.sum()))
    relation = "above" if price > vwap + 0.25 * dev else "below" if price < vwap - 0.25 * dev else "at"
    state = relation
    if closed_m5 is not None and len(closed_m5) >= 3:
        c = closed_m5.tail(3)
        prev, last = c.iloc[-2], c.iloc[-1]
        if prev.close < vwap <= last.close: state = "reclaim"
        elif prev.close > vwap >= last.close: state = "cross_below"
        elif last.high >= vwap and last.close < vwap and prev.close < vwap: state = "rejection_from_below"
        elif last.low <= vwap and last.close > vwap and prev.close > vwap: state = "rejection_from_above"
    level = LiquidityLevel(round(vwap, 2), "VWAP", "SESSION", pd.Timestamp(d.iloc[0].time).to_pydatetime(), 2.0)
    return level, {"vwap": round(vwap, 2), "dev": round(dev, 2), "upper_band": round(vwap + dev, 2), "lower_band": round(vwap - dev, 2),
                   "relation": relation, "state": state}


def premium_discount(structure: StructureResult, price: float) -> dict[str, Any]:
    """Dealing range = the impulse leg of the last confirmed BOS/CHoCH: the swing that started the move and the extreme it made (item 32)."""
    pivots, events = structure.pivots, structure.events
    if not events or not pivots:
        return {"zone": "n/a", "pct": None}
    usable_events = [event for event in events if event.get("status") != "failed"]
    if not usable_events:
        return {"zone": "n/a", "pct": None}
    last = usable_events[-1]; bullish = str(last["event"]).startswith("Bullish"); t = pd.Timestamp(last["timestamp"])
    origin_kind = "LOW" if bullish else "HIGH"
    before = [p for p in pivots if p.kind == origin_kind and pd.Timestamp(p.timestamp) <= t]
    after = [p for p in pivots if p.kind == ("HIGH" if bullish else "LOW") and pd.Timestamp(p.timestamp) >= t]
    if not before:
        return {"zone": "n/a", "pct": None}
    origin = before[-1].price
    extreme = (max(p.price for p in after) if after else None)
    if extreme is None:
        extreme = max(price, float(last["level"])) if bullish else min(price, float(last["level"]))
    hi, lo = (extreme, origin) if bullish else (origin, extreme)
    if hi <= lo:
        return {"zone": "n/a", "pct": None}
    pct = (price - lo) / (hi - lo)
    zone = "DEEP_DISCOUNT" if pct < 0.25 else "DISCOUNT" if pct < 0.5 else "PREMIUM" if pct <= 0.75 else "DEEP_PREMIUM"
    return {"zone": zone, "pct": round(float(pct), 3), "range_high": round(hi, 2), "range_low": round(lo, 2),
            "equilibrium": round((hi + lo) / 2, 2), "impulse": last["event"], "confirmed_range": bool(after)}


def trendlines(pivots: list[Pivot], frame: pd.DataFrame, atr: float, min_touches: int = 3, max_slope_atr: float = 0.15,
               max_age_bars: int = 400) -> list[dict[str, Any]]:
    """Line through two anchor pivots, validated by ≥ min_touches pivots within 0.3 ATR, slope limit, age limit;
    state = testing / respected / broken (close through) / broken+retested (item 31)."""
    out: list[dict[str, Any]] = []
    if frame.empty or len(pivots) < 2:
        return out
    last_i = len(frame) - 1; last = frame.iloc[-1]
    for kind, base in (("HIGH", "RESISTANCE"), ("LOW", "SUPPORT")):
        pts = [p for p in pivots if p.kind == kind and last_i - p.index <= max_age_bars][-30:]
        if len(pts) < 2:
            continue
        best = None
        for a in range(len(pts) - 1):
            for b in range(a + 1, len(pts)):
                pa, pb = pts[a], pts[b]
                if pb.index == pa.index: continue
                slope = (pb.price - pa.price) / (pb.index - pa.index)
                if abs(slope) > max_slope_atr * atr: continue
                touches = sum(1 for p in pts if abs(p.price - (pa.price + slope * (p.index - pa.index))) <= 0.3 * atr)
                if touches >= min_touches and (best is None or touches > best[2] or (touches == best[2] and pb.index > best[1].index)):
                    best = (pa, pb, touches, slope)
        if best is None:
            continue
        pa, pb, touches, slope = best
        value = pa.price + slope * (last_i - pa.index)
        closes = frame.close.values; idx = np.arange(len(frame)); line = pa.price + slope * (idx - pa.index)
        after = idx > pb.index
        if kind == "HIGH":
            broke = np.where(after & (closes > line + 0.1 * atr))[0]
            state = "broken" if len(broke) else ("testing" if abs(last.close - value) <= 0.3 * atr else "respected")
            if len(broke):
                bi = int(broke[0]); later = np.arange(bi + 1, len(frame))
                retested = later[(frame.low.values[later] <= line[later] + 0.1 * atr) & (closes[later] > line[later])]
                if len(retested): state = "broken+retested"
        else:
            broke = np.where(after & (closes < line - 0.1 * atr))[0]
            state = "broken" if len(broke) else ("testing" if abs(last.close - value) <= 0.3 * atr else "respected")
            if len(broke):
                bi = int(broke[0]); later = np.arange(bi + 1, len(frame))
                retested = later[(frame.high.values[later] >= line[later] - 0.1 * atr) & (closes[later] < line[later])]
                if len(retested): state = "broken+retested"
        name = f"{'ASCENDING' if slope > 0 else 'DESCENDING'}_{base}_LINE"
        out.append({"name": name, "value": round(float(value), 2), "slope_per_bar": round(float(slope), 4), "touches": int(touches),
                    "age_bars": int(last_i - pa.index), "state": state, "timestamp": pb.confirmation_timestamp})
    return out
