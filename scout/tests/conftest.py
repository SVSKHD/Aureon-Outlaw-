from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from xau_mt5_bot.config import load_config
from xau_mt5_bot.mt5_client import AccountState, BrokerClock, OrderResult, Tick


class FakeClient:
    """In-memory MT5 stand-in.

    `self.tick` is the TRUE-UTC state of the fake market. `broker_offset_hours` simulates a
    server that stamps its ticks and bars in its own timezone (a UTC+3 broker → 3.0), and
    `extra_skew_seconds` simulates a genuinely wrong clock on top of that offset. Like the real
    client, everything handed out of `get_tick`/`get_bars` has already been converted to UTC,
    so a correctly detected offset is invisible downstream and only the residual skew remains.
    """

    def __init__(self, hedging: bool = True, demo: bool = True, broker_offset_hours: float = 0.0,
                 extra_skew_seconds: float = 0.0, manual_offset_hours: float | None = None) -> None:
        self.hedging = hedging
        self.demo = demo
        self._positions: list[SimpleNamespace] = []
        self.ticket = 100
        self.tick = Tick(datetime(2026, 9, 4, 12, tzinfo=UTC), 2500.00, 2500.20)
        self.closed: list[tuple[int, int]] = []
        self.deals: dict[int, list[dict]] = {}
        self.sent: list[dict] = []
        self.broker_offset_hours = float(broker_offset_hours)
        self.extra_skew_seconds = float(extra_skew_seconds)
        self.clock = BrokerClock.from_hours(manual_offset_hours)
        self.server = "FakeBroker-Demo"
        self.bar_frames: dict[str, pd.DataFrame] = {}      # true-UTC bars the fake market has produced
        self.bar_calls: list[tuple[str, str, int]] = []

    # ---- broker clock simulation -------------------------------------------------------------
    def system_utc_now(self) -> datetime:
        """The PC clock as the fake bot sees it (the fake market's true UTC unless a test moves it)."""
        return getattr(self, "now", None) or self.tick.time

    def _broker_shift(self) -> timedelta:
        return timedelta(hours=self.broker_offset_hours, seconds=self.extra_skew_seconds)

    def raw_tick_epoch(self) -> float:
        """What MetaTrader5 would return from symbol_info_tick().time — broker-server seconds."""
        return (self.tick.time + self._broker_shift()).timestamp()

    def raw_bars(self, timeframe: str) -> pd.DataFrame:
        """What copy_rates_from_pos() would return — bar stamps in broker-server time."""
        frame = self.bar_frames[timeframe].copy()
        frame["time"] = pd.to_datetime(frame["time"], utc=True) + timedelta(hours=self.broker_offset_hours)
        return frame

    def measure_broker_clock(self) -> BrokerClock:
        return self.clock.measure(self.raw_tick_epoch(), self.system_utc_now(), self.server)

    def refresh_broker_clock(self) -> BrokerClock:
        now = self.system_utc_now()
        return self.measure_broker_clock() if self.clock.needs_measurement(now) else self.clock

    def account_state(self) -> AccountState:
        return AccountState(1, 50000, 50000, 45000, True, self.demo, self.hedging)

    def symbol_info(self, symbol: str):
        return SimpleNamespace(volume_min=0.01, volume_max=100.0, volume_step=0.01, point=0.01, digits=2,
                               trade_tick_size=0.01, trade_tick_value=1.0, trade_stops_level=0, trade_freeze_level=0)

    def get_tick(self, symbol: str) -> Tick:
        if not self.broker_offset_hours and not self.extra_skew_seconds and not self.clock.manual:
            return self.tick
        self.refresh_broker_clock()
        return Tick(self.clock.to_utc(self.raw_tick_epoch()), self.tick.bid, self.tick.ask)

    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self.bar_calls.append((symbol, timeframe, count))
        if timeframe not in self.bar_frames:
            raise NotImplementedError
        self.refresh_broker_clock()
        frame = self.raw_bars(timeframe)
        frame["time"] = pd.to_datetime(frame["time"], utc=True) - self.clock.offset      # the single conversion point
        return frame.sort_values("time").tail(count).reset_index(drop=True)

    def positions(self, symbol: str | None = None, magic: int | None = None):
        values = [p for p in self._positions if symbol is None or p.symbol == symbol]
        if magic is not None:
            values = [p for p in values if p.magic == magic]
        return list(values)

    def send_market(self, symbol, side, volume, magic, comment, sl=0.0, tp=0.0):
        self.sent.append({"symbol": symbol, "side": side, "volume": volume, "magic": magic, "comment": comment, "sl": sl, "tp": tp})
        self.ticket += 1
        is_buy = side in {"BUY", "LONG"}
        position = SimpleNamespace(
            ticket=self.ticket, symbol=symbol, magic=magic, volume=volume,
            type=0 if is_buy else 1, price_open=self.tick.ask if is_buy else self.tick.bid,
            profit=0.0, comment=comment, sl=sl, tp=tp,
        )
        self._positions.append(position)
        return OrderResult(True, 10009, self.ticket, position.price_open, "done")

    def order_check(self, *args, **kwargs): return True, "ok"
    def calc_profit(self, symbol, side, volume, price_open, price_close):
        return (price_close - price_open) * (1 if side in {"BUY", "LONG"} else -1) * volume * 100
    def modify_sltp(self, ticket, sl, tp, expected_magic):
        p = next((p for p in self._positions if p.ticket == ticket and p.magic == expected_magic), None)
        if not p: return OrderResult(False, -2, ticket, None, "mismatch")
        p.sl, p.tp = sl, tp
        return OrderResult(True, 10009, ticket, None, "modified")
    def close_partial(self, ticket, volume, expected_magic, comment="PA_PARTIAL"):
        p = next((p for p in self._positions if p.ticket == ticket and p.magic == expected_magic), None)
        if not p or volume > p.volume: return OrderResult(False, -2, ticket, None, "mismatch")
        price = self.tick.bid if p.type == 0 else self.tick.ask
        p.volume = round(p.volume - volume, 8)
        self.deals.setdefault(ticket, []).append({"deal_ticket": len(self.deals.get(ticket, [])) + 1, "price": price,
            "profit": self.calc_profit(p.symbol, "LONG" if p.type == 0 else "SHORT", volume, p.price_open, price),
            "commission": -0.1, "swap": 0.0, "net": self.calc_profit(p.symbol, "LONG" if p.type == 0 else "SHORT", volume, p.price_open, price) - 0.1,
            "time": self.tick.time, "reason": "BOT", "volume": volume})
        if p.volume <= 1e-9: self._positions.remove(p)
        return OrderResult(True, 10009, ticket, price, "partial")

    def closed_deals(self, ticket): return list(self.deals.get(ticket, []))

    def close_position(self, ticket: int, expected_magic: int):
        match = next((p for p in self._positions if p.ticket == ticket), None)
        if match is None or match.magic != expected_magic:
            return OrderResult(False, -2, ticket, None, "mismatch")
        self._positions.remove(match)
        volume = float(match.volume); price = self.tick.bid if match.type == 0 else self.tick.ask
        self.deals.setdefault(ticket, []).append({"deal_ticket": len(self.deals.get(ticket, [])) + 1, "price": price,
            "profit": self.calc_profit(match.symbol, "LONG" if match.type == 0 else "SHORT", volume, match.price_open, price),
            "commission": -0.1, "swap": 0.0, "net": self.calc_profit(match.symbol, "LONG" if match.type == 0 else "SHORT", volume, match.price_open, price) - 0.1,
            "time": self.tick.time, "reason": "BOT", "volume": volume})
        self.closed.append((ticket, expected_magic))
        return OrderResult(True, 10009, ticket, self.tick.bid, "closed")


@pytest.fixture
def config():
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.yaml")
    cfg.safety.allow_scout_orders = True
    return cfg


def bars(start: str, values: list[tuple[float, float, float, float]], freq: str = "1min") -> pd.DataFrame:
    times = pd.date_range(start, periods=len(values), freq=freq, tz="UTC")
    return pd.DataFrame(
        [
            {"time": time, "open": o, "high": h, "low": l, "close": c, "tick_volume": 1, "spread": 10}
            for time, (o, h, l, c) in zip(times, values)
        ]
    )
