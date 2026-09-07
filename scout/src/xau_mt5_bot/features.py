from __future__ import annotations

import numpy as np
import pandas as pd


def with_candle_features(frame: pd.DataFrame, atr_period: int = 14) -> pd.DataFrame:
    data = frame.copy()
    data["range"] = (data["high"] - data["low"]).clip(lower=1e-12)
    data["body"] = (data["close"] - data["open"]).abs()
    data["upper_wick"] = data["high"] - data[["open", "close"]].max(axis=1)
    data["lower_wick"] = data[["open", "close"]].min(axis=1) - data["low"]
    data["body_ratio"] = data["body"] / data["range"]
    data["upper_wick_ratio"] = data["upper_wick"] / data["range"]
    data["lower_wick_ratio"] = data["lower_wick"] / data["range"]
    data["close_location"] = (data["close"] - data["low"]) / data["range"]
    data["open_location"] = (data["open"] - data["low"]) / data["range"]
    previous_close = data["close"].shift(1)
    true_range = pd.concat(
        [
            data["high"] - data["low"],
            (data["high"] - previous_close).abs(),
            (data["low"] - previous_close).abs(),
        ], axis=1,
    ).max(axis=1)
    data["atr"] = true_range.ewm(alpha=1 / atr_period, adjust=False, min_periods=atr_period).mean()
    data["range_atr"] = data["range"] / data["atr"].replace(0, np.nan)
    return data


def closed_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """MT5 position zero is forming, so the newest row is excluded from confirmations."""
    return frame.iloc[:-1].copy() if len(frame) > 1 else frame.iloc[0:0].copy()


def freshness_age_seconds(m1: pd.DataFrame, now: pd.Timestamp) -> float:
    if m1.empty:
        return float("inf")
    latest = pd.Timestamp(m1.iloc[-1]["time"])
    if latest.tzinfo is None:
        latest = latest.tz_localize("UTC")
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    return max(0.0, (now - latest).total_seconds())

