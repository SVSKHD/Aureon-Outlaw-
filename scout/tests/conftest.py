from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from xau_mt5_bot.config import load_config
from xau_mt5_bot.mt5_client import AccountState, BrokerClock, OrderResult, Tick


class FakeClient:
    """`self.tick` always carries the TRUE UTC instant. `broker_offset_hours` simulates a broker server on a
    non-UTC clock (a UTC+3 server reports its own wall clock as the MT5 epoch), and `clock_skew_seconds`
    simulates genuine skew on top of that. Everything the client hands out is converted back to UTC through
    `self.clock`, exactly as MT5Client does (v3.3.0)."""

    def __init__(self, hedging: bool = True, demo: bool = True, broker_offset_hours: float = 0.0,
                 manual_offset_hours: float | None = None) -> None:
        self.hedging = hedging
        self.demo = demo
        self._positions: list[SimpleNamespace] = []
        self.ticket = 100
        self.tick = Tick(datetime(2026, 9, 4, 12, tzinfo=UTC), 2500.00, 2500.20)
        self.closed: list[tuple[int, int]] = []
        self.deals: dict[int, list[dict]] = {}
        self.sent: list[dict] = []
        self.broker_offset_hours = broker_offset_hours
        self.clock_skew_seconds = 0.0
        self.server = "FakeBroker-Demo"
        self.clock = BrokerClock(manual_offset_hours)
        self.offset_measurements = 0
        self.frames: dict[str, pd.DataFrame] = {}

    # -- v3.3.0 broker clock -------------------------------------------------------------------
    def _broker_epoch(self) -> float:
        """What MT5 would report for the latest tick: the broker's wall clock, as epoch seconds."""
        return (self.tick.time + timedelta(hours=self.broker_offset_hours, seconds=self.clock_skew_seconds)).timestamp()

    def broker_clock(self) -> dict:
        return self.clock.info()

    def refresh_broker_offset(self, now: datetime | None = None, force: bool = False) -> dict:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        epoch = self._broker_epoch()
        if force or self.clock.due(now):
            self.offset_measurements += 1
            return self.clock.measure(epoch, now, server=self.server)
        raw = datetime.fromtimestamp(epoch, tz=UTC)
        self.clock.raw_delta_seconds = (raw - now).total_seconds()
        self.clock.residual_seconds = (raw - self.clock.broker_utc_offset - now).total_seconds()
        return self.clock.info()

    def account_state(self) -> AccountState:
        return AccountState(1, 50000, 50000, 45000, True, self.demo, self.hedging)

    def symbol_info(self, symbol: str):
        return SimpleNamespace(volume_min=0.01, volume_max=100.0, volume_step=0.01, point=0.01, digits=2,
                               trade_tick_size=0.01, trade_tick_value=1.0, trade_stops_level=0, trade_freeze_level=0)

    def get_tick(self, symbol: str) -> Tick:
        return Tick(self.clock.epoch_to_utc(self._broker_epoch()), self.tick.bid, self.tick.ask)

    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        frame = self.frames.get(timeframe)
        if frame is None:
            raise NotImplementedError
        raw = frame.copy()
        raw["time"] = pd.to_datetime(raw["time"], utc=True) + pd.Timedelta(hours=self.broker_offset_hours)
        raw["time"] = self.clock.frame_to_utc(raw["time"])           # single conversion point, as in MT5Client
        return raw.tail(count).reset_index(drop=True)

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
