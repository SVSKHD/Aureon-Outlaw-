from __future__ import annotations

import math
import pandas as pd

from .config import BotConfig
from .mt5_client import TradingClient


REQUIRED_COLUMNS = {"time", "open", "high", "low", "close"}


_CACHE: dict[tuple, pd.DataFrame] = {}
REFRESH_BARS = {"M1": 240, "M5": 120, "M15": 60, "H1": 30, "H4": 12, "D1": 5}
TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}


def reset_history_cache() -> None:
    _CACHE.clear()


def load_native_history(client: TradingClient, config: BotConfig, force_full: bool = False, account_key: str = "",
                        symbol: str | None = None, history_bars: dict[str, int] | None = None) -> dict[str, pd.DataFrame]:
    """Full fetch once per (symbol, account, timeframe); then only the tail is refetched and merged.
    A tail that does not overlap the cache (gap after reconnect / long pause) triggers a full refetch.
    v3.1.0: `symbol`/`history_bars` override the primary symbol so reference symbols (XAGUSD) share the same cache logic."""
    frames: dict[str, pd.DataFrame] = {}
    symbol = symbol or config.symbol
    for timeframe, count in (history_bars or config.history_bars).items():
        key = (symbol, account_key, timeframe)
        cached = None if force_full else _CACHE.get(key)
        frame = None
        if cached is not None:
            tail = client.get_bars(symbol, timeframe, REFRESH_BARS.get(timeframe, 60))
            tail["time"] = pd.to_datetime(tail.time, utc=True)
            gap = (pd.Timestamp(tail.time.iloc[0]) - pd.Timestamp(cached.time.iloc[-1])).total_seconds()
            if gap <= TF_SECONDS.get(timeframe, 60) * 2:
                frame = pd.concat([cached, tail]).drop_duplicates("time", keep="last").sort_values("time").tail(count)
        if frame is None:
            frame = client.get_bars(symbol, timeframe, count)
        received = len(frame)
        missing = REQUIRED_COLUMNS.difference(frame.columns)
        if missing:
            raise ValueError(f"{timeframe} history is missing columns: {sorted(missing)}")
        frame = frame.copy()
        frame["time"] = pd.to_datetime(frame.time, utc=True)
        if not frame.time.is_monotonic_increasing:
            frame = frame.sort_values("time")
        frames[timeframe] = frame.drop_duplicates("time", keep="last").reset_index(drop=True)
        frames[timeframe].attrs.update({"requested_bars": count, "received_bars": received,
                                        "history_complete": received >= count})
        _CACHE[key] = frames[timeframe]
    return frames


def analysis_window(frames: dict[str, pd.DataFrame], config: BotConfig) -> dict[str, pd.DataFrame]:
    """Fast per-cycle windows for structure/trigger/zones. The 30-day pattern scan uses pattern_window() (v1.8.0)."""
    windows = dict(config.analysis_window_bars)
    result = {}
    for tf, df in frames.items():
        frame = df.tail(windows.get(tf, len(df))).reset_index(drop=True)
        frame.attrs.update(df.attrs)
        result[tf] = frame
    return result


def pattern_window(full_m5: pd.DataFrame, config: BotConfig, now) -> pd.DataFrame:
    """Long-horizon M5 window for the pattern scan: last pattern_lookback_days of bars (v1.8.0, item 2)."""
    cutoff = pd.Timestamp(now) - pd.Timedelta(days=config.analysis.pattern_lookback_days)
    frame = full_m5[pd.to_datetime(full_m5.time, utc=True) > cutoff].reset_index(drop=True)
    frame.attrs.update(full_m5.attrs)
    return frame
