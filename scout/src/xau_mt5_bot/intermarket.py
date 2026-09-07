"""XAU/XAG intermarket context (v3.1.0).

Silver is fetched from the same MT5 terminal and compared with gold on closed bars only:

- rolling Pearson correlation of M5 log returns (`correlation_bars`), labelled COUPLED / WEAK / DECOUPLED;
- relative strength: XAG return minus XAU return over `relative_strength_bars` M5 bars (silver leading up/down);
- SMT divergence on the last two confirmed pivots of the chosen timeframe: gold makes a higher high while silver makes a
  lower high = BEARISH SMT; gold makes a lower low while silver makes a higher low = BULLISH SMT.

Silver never authorises a trade; it can only add up to `max_weight` points to the confluence family `intermarket`
(and only while the pair is COUPLED), and it is reported as evidence. Missing or stale silver data degrades to
UNAVAILABLE and the engine continues unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .models import Side
from .structure import detect_pivots

UNAVAILABLE: dict[str, Any] = {
    "status": "UNAVAILABLE", "symbol": None, "correlation": None, "regime": "UNAVAILABLE", "smt": "NONE",
    "smt_timeframe": None, "smt_detail": None, "relative_strength": None, "silver_leading": "NONE",
    "long_points": 0, "short_points": 0, "reason": "no silver data",
}


def _log_returns(frame: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(frame["close"], errors="coerce").astype(float)
    return np.log(close).diff().dropna()


def _align(gold: pd.DataFrame, silver: pd.DataFrame) -> pd.DataFrame:
    g = gold[["time", "close"]].copy(); s = silver[["time", "close"]].copy()
    g["time"] = pd.to_datetime(g.time, utc=True); s["time"] = pd.to_datetime(s.time, utc=True)
    merged = g.merge(s, on="time", how="inner", suffixes=("_xau", "_xag")).sort_values("time").reset_index(drop=True)
    return merged


def rolling_correlation(gold_m5: pd.DataFrame, silver_m5: pd.DataFrame, bars: int) -> float | None:
    merged = _align(gold_m5, silver_m5)
    if len(merged) < max(bars + 1, 10):
        return None
    r_xau = np.log(merged.close_xau.astype(float)).diff().tail(bars)
    r_xag = np.log(merged.close_xag.astype(float)).diff().tail(bars)
    if r_xau.std() == 0 or r_xag.std() == 0:
        return None
    value = float(np.corrcoef(r_xau.to_numpy(), r_xag.to_numpy())[0, 1])
    return None if np.isnan(value) else round(value, 3)


def relative_strength(gold_m5: pd.DataFrame, silver_m5: pd.DataFrame, bars: int) -> float | None:
    merged = _align(gold_m5, silver_m5)
    if len(merged) < bars + 1:
        return None
    xau = float(merged.close_xau.iloc[-1] / merged.close_xau.iloc[-1 - bars] - 1.0)
    xag = float(merged.close_xag.iloc[-1] / merged.close_xag.iloc[-1 - bars] - 1.0)
    return round((xag - xau) * 100.0, 3)                                   # percentage points: + = silver stronger


def smt_divergence(gold: pd.DataFrame, silver: pd.DataFrame, left: int, right: int, max_age_bars: int) -> dict[str, Any]:
    """Compare the last two confirmed pivots of each metal. Pivots must be aligned in time within `pair_tolerance` bars."""
    gp = detect_pivots(gold, left, right); sp = detect_pivots(silver, left, right)
    result = {"smt": "NONE", "detail": None}
    if len(gold) == 0:
        return result
    last_time = pd.Timestamp(pd.to_datetime(gold.time.iloc[-1], utc=True))
    tf_seconds = _bar_seconds(gold)
    tolerance = timedelta(seconds=tf_seconds * 3)
    for kind, bear in (("HIGH", True), ("LOW", False)):
        g = [p for p in gp if p.kind == kind][-2:]; s = [p for p in sp if p.kind == kind]
        if len(g) < 2:
            continue
        if (last_time - pd.Timestamp(g[-1].timestamp)).total_seconds() > tf_seconds * max_age_bars:
            continue
        pair = []
        for gpiv in g:
            match = min((p for p in s if abs((pd.Timestamp(p.timestamp) - pd.Timestamp(gpiv.timestamp)).total_seconds()) <= tolerance.total_seconds()),
                        key=lambda p: abs((pd.Timestamp(p.timestamp) - pd.Timestamp(gpiv.timestamp)).total_seconds()), default=None)
            if match is None:
                break
            pair.append((gpiv, match))
        if len(pair) < 2:
            continue
        (g1, s1), (g2, s2) = pair
        if bear and g2.price > g1.price and s2.price < s1.price:
            return {"smt": "BEARISH", "detail": {"kind": "HIGH", "xau": [round(g1.price, 2), round(g2.price, 2)],
                                                 "xag": [round(s1.price, 3), round(s2.price, 3)], "timestamp": g2.timestamp.isoformat()}}
        if not bear and g2.price < g1.price and s2.price > s1.price:
            return {"smt": "BULLISH", "detail": {"kind": "LOW", "xau": [round(g1.price, 2), round(g2.price, 2)],
                                                 "xag": [round(s1.price, 3), round(s2.price, 3)], "timestamp": g2.timestamp.isoformat()}}
    return result


def _bar_seconds(frame: pd.DataFrame) -> int:
    if len(frame) < 2:
        return 300
    t = pd.to_datetime(frame.time, utc=True)
    return int(max(60, (t.iloc[-1] - t.iloc[-2]).total_seconds()))


def assess_intermarket(gold_closed: dict[str, pd.DataFrame], silver_closed: dict[str, pd.DataFrame] | None, cfg: Any,
                       now: datetime | None = None) -> dict[str, Any]:
    if not cfg.enabled or not silver_closed or "M5" not in silver_closed or silver_closed["M5"].empty:
        return dict(UNAVAILABLE)
    gold_m5, silver_m5 = gold_closed["M5"], silver_closed["M5"]
    if now is not None:
        age = (pd.Timestamp(now) - pd.Timestamp(pd.to_datetime(silver_m5.time.iloc[-1], utc=True))).total_seconds()
        if age > cfg.max_silver_age_seconds:
            out = dict(UNAVAILABLE); out.update({"symbol": cfg.symbol, "reason": f"silver M5 is {age:.0f}s old"}); return out
    corr = rolling_correlation(gold_m5, silver_m5, cfg.correlation_bars)
    rs = relative_strength(gold_m5, silver_m5, cfg.relative_strength_bars)
    tf = cfg.smt_timeframe
    smt = smt_divergence(gold_closed.get(tf, gold_m5), silver_closed.get(tf, silver_m5), cfg.swing_left, cfg.swing_right, cfg.smt_max_age_bars) \
        if tf in gold_closed and tf in silver_closed else {"smt": "NONE", "detail": None}
    if corr is None:
        regime = "INSUFFICIENT"
    elif corr >= cfg.coupled_correlation:
        regime = "COUPLED"
    elif corr >= cfg.weak_correlation:
        regime = "WEAK"
    else:
        regime = "DECOUPLED"
    leading = "NONE"
    if rs is not None and abs(rs) >= cfg.leading_threshold_pct:
        leading = "UP" if rs > 0 else "DOWN"
    long_points = short_points = 0
    if regime == "COUPLED":
        if smt["smt"] == "BULLISH": long_points += cfg.smt_weight
        elif smt["smt"] == "BEARISH": short_points += cfg.smt_weight
        if leading == "UP": long_points += cfg.leading_weight
        elif leading == "DOWN": short_points += cfg.leading_weight
    long_points, short_points = min(long_points, cfg.max_weight), min(short_points, cfg.max_weight)
    reason = f"corr {corr} {regime}; SMT {smt['smt']}; silver leading {leading} (rs {rs})" if corr is not None else "not enough overlapping bars"
    return {"status": "OK", "symbol": cfg.symbol, "correlation": corr, "regime": regime, "smt": smt["smt"], "smt_timeframe": tf,
            "smt_detail": smt["detail"], "relative_strength": rs, "silver_leading": leading,
            "long_points": long_points, "short_points": short_points, "reason": reason}


def intermarket_patterns(info: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    names = []
    if info.get("smt") in {"BULLISH", "BEARISH"}:
        names.append(f"{info['smt'].title()} SMT XAU/XAG")
    if info.get("regime") == "DECOUPLED":
        names.append("XAU/XAG decoupled")
    return [{"name": n, "timestamp": now, "intermarket": {k: info.get(k) for k in ("correlation", "regime", "relative_strength")}} for n in names]


def side_for(info: dict[str, Any]) -> Side | None:
    if info.get("long_points", 0) > info.get("short_points", 0): return Side.LONG
    if info.get("short_points", 0) > info.get("long_points", 0): return Side.SHORT
    return None
