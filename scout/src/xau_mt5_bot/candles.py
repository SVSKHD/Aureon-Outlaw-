from __future__ import annotations

from typing import Any

import pandas as pd

from .features import with_candle_features


def detect_candlestick_patterns(frame: pd.DataFrame, atr_period: int = 14) -> list[dict[str, Any]]:
    """Detect objective candle shapes; callers must add location/structure context."""
    data = with_candle_features(frame, atr_period)
    found: list[dict[str, Any]] = []
    for i, row in data.iterrows():
        names: list[str] = []
        bullish = row.close > row.open
        bearish = row.close < row.open
        if row.body_ratio <= 0.10:
            names.append("Doji")
            if row.upper_wick_ratio >= 0.40 and row.lower_wick_ratio >= 0.40:
                names.append("Long-Legged Doji")
            if row.lower_wick_ratio >= 0.60 and row.upper_wick_ratio <= 0.10:
                names.append("Dragonfly Doji")
            if row.upper_wick_ratio >= 0.60 and row.lower_wick_ratio <= 0.10:
                names.append("Gravestone Doji")
        if row.body_ratio <= 0.35 and row.lower_wick >= 2 * max(row.body, 1e-9) and row.upper_wick_ratio < 0.20:
            names.extend(["Hammer", "Bullish Pin Bar"])
        if row.body_ratio <= 0.35 and row.upper_wick >= 2 * max(row.body, 1e-9) and row.lower_wick_ratio < 0.20:
            names.extend(["Shooting Star", "Bearish Pin Bar"])
        if row.body_ratio >= 0.90:
            names.append("Marubozu")
        elif row.body_ratio <= 0.30 and row.upper_wick_ratio >= 0.20 and row.lower_wick_ratio >= 0.20:
            names.append("Spinning Top" if row.range_atr < 1.5 else "High-Wave Candle")
        if i > 0:
            previous = data.iloc[i - 1]
            prev_bull = previous.close > previous.open
            prev_bear = previous.close < previous.open
            if bullish and prev_bear and row.open <= previous.close and row.close >= previous.open:
                names.append("Bullish Engulfing")
            if bearish and prev_bull and row.open >= previous.close and row.close <= previous.open:
                names.append("Bearish Engulfing")
            inside_body = max(row.open, row.close) <= max(previous.open, previous.close) and min(row.open, row.close) >= min(previous.open, previous.close)
            if inside_body and bullish and prev_bear:
                names.append("Bullish Harami")
            if inside_body and bearish and prev_bull:
                names.append("Bearish Harami")
            if inside_body and row.body_ratio <= 0.10:
                names.append("Harami Cross")
            if row.high < previous.high and row.low > previous.low:
                names.append("Inside Bar")
            if row.high > previous.high and row.low < previous.low:
                names.extend(["Outside Bar", "Bullish Outside Bar" if bullish else "Bearish Outside Bar"])
            tolerance = max(float(row.atr) * 0.05 if pd.notna(row.atr) else 0, 1e-9)
            if abs(row.high - previous.high) <= tolerance:
                names.append("Tweezer Top")
            if abs(row.low - previous.low) <= tolerance:
                names.append("Tweezer Bottom")
        if i >= 2:
            a, b = data.iloc[i - 2], data.iloc[i - 1]
            if a.close < a.open and b.body_ratio <= 0.30 and bullish and row.close > (a.open + a.close) / 2:
                names.append("Morning Star")
                if b.body_ratio <= 0.10:
                    names.append("Morning Doji Star")
            if a.close > a.open and b.body_ratio <= 0.30 and bearish and row.close < (a.open + a.close) / 2:
                names.append("Evening Star")
                if b.body_ratio <= 0.10:
                    names.append("Evening Doji Star")
            trio = data.iloc[i - 2 : i + 1]
            if all(trio.close > trio.open) and all(trio.body_ratio >= 0.5):
                names.append("Three White Soldiers")
            if all(trio.close < trio.open) and all(trio.body_ratio >= 0.5):
                names.append("Three Black Crows")
        for name in dict.fromkeys(names):
            found.append({"name": name, "timestamp": row.time, "index": int(i)})
    return found

