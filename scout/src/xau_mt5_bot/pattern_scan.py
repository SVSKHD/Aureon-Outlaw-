"""Long-horizon (30-day) pattern scan, isolated so it can run in a worker process (v1.9.0).

Runs outside the trading interpreter so the per-cycle trigger path never competes with it for the GIL.
Everything here must be picklable: plain DataFrames in, lists of dicts / a dict out.
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd

from .candles import detect_candlestick_patterns
from .charts import detect_chart_patterns
from .structure import analyze_structure
from .volatility import analyze_volatility


def lower_priority() -> None:
    """Worker initializer: the scan yields CPU to the trading process (POSIX nice / Windows BELOW_NORMAL)."""
    try:
        import os
        if hasattr(os, "nice"):
            os.nice(10)
        else:
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)
    except Exception:
        pass


def session_range_stats(long_m5: pd.DataFrame, windows: list[tuple[str, str, str]]) -> dict[str, dict[str, float]]:
    """Per-session range percentiles (p50/p75/p90) over the long window. `windows` = [(start_iso, end_iso, session_name)]."""
    if long_m5 is None or len(long_m5) == 0 or not windows: return {}
    times = pd.to_datetime(long_m5["time"], utc=True)
    ranges: dict[str, list[float]] = {}
    for start, end, name in windows:
        mask = (times >= pd.Timestamp(start)) & (times < pd.Timestamp(end))
        if mask.sum() < 6: continue
        chunk = long_m5[mask]
        ranges.setdefault(name, []).append(float(chunk.high.max() - chunk.low.min()))
    result = {}
    for name, values in ranges.items():
        series = pd.Series(values)
        result[name] = {"samples": int(len(values)), "p50": round(float(series.quantile(0.5)), 2),
                        "p75": round(float(series.quantile(0.75)), 2), "p90": round(float(series.quantile(0.9)), 2)}
    return result


def compute_patterns(long_m5: pd.DataFrame, atr: float, atr_period: int, swing_left: int, swing_right: int,
                     session_windows: list[tuple[str, str, str]] | None = None) -> dict[str, Any]:
    t0 = time.perf_counter()
    long_structure = analyze_structure(long_m5, "M5", swing_left, swing_right)
    candle_patterns = detect_candlestick_patterns(long_m5, atr_period)
    chart_patterns = detect_chart_patterns(long_structure.pivots, atr * 0.20)
    volatility = analyze_volatility(long_m5, atr_period)
    return {"candles": candle_patterns, "charts": chart_patterns, "volatility": volatility,
            "session_ranges": session_range_stats(long_m5, session_windows or []),
            "bars": int(len(long_m5)), "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "bar_time": pd.Timestamp(long_m5.iloc[-1].time).isoformat() if len(long_m5) else None}
