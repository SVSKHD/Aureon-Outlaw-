from __future__ import annotations

from typing import Any

import pandas as pd

from .features import with_candle_features


def analyze_volatility(frame: pd.DataFrame, atr_period: int = 14) -> dict[str, Any]:
    data = with_candle_features(frame, atr_period)
    if len(data) < max(20, atr_period + 2):
        return {"regime": "UNKNOWN", "patterns": []}
    last = data.iloc[-1]
    recent = data.iloc[-20:]
    median_atr = float(recent.atr.median())
    regime = "NORMAL"
    if float(last.atr) < 0.75 * median_atr:
        regime = "CONTRACTION"
    elif float(last.atr) > 1.35 * median_atr:
        regime = "EXPANSION"
    patterns: list[str] = []
    if float(last.range) == float(data.iloc[-4:].range.min()):
        patterns.append("NR4")
    if float(last.range) == float(data.iloc[-7:].range.min()):
        patterns.append("NR7")
    if len(data) >= 2 and last.high < data.iloc[-2].high and last.low > data.iloc[-2].low:
        patterns.append("Inside-bar compression")
    if float(last.range_atr) >= 1.5 and float(last.body_ratio) >= 0.65:
        patterns.append("Strong displacement")
    return {"regime": regime, "patterns": patterns, "atr": float(last.atr)}

