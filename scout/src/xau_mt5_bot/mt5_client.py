from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import pandas as pd

try:
    import MetaTrader5 as mt5  # type: ignore
except ImportError:  # pragma: no cover - expected on non-Windows test systems
    mt5 = None


OFFSET_QUANTUM_SECONDS = 1800          # broker server timezones are whole or half hours
REMEASURE_SECONDS = 3600               # v3.3.0: re-detect the offset at most once per hour


@dataclass
class BrokerClock:
    """Single conversion point between BROKER SERVER time and true UTC (v3.3.0).

    `symbol_info_tick().time` and the `time` column of `copy_rates_*` are epoch seconds
    expressed in the BROKER SERVER timezone, not UTC. Reading them with
    `datetime.fromtimestamp(t, tz=UTC)` therefore yields the broker's wall clock merely
    *labelled* UTC. `offset` is that label error (broker wall clock − true UTC); every
    timestamp the client hands out has it subtracted, so the rest of the bot only ever
    sees true UTC. What is left after the subtraction is `residual_seconds` — a genuine
    clock problem, and the only thing that may block orders.
    """

    offset: timedelta = timedelta(0)
    manual: bool = False
    measured_at: datetime | None = None
    residual_seconds: float = 0.0
    server: str = ""
    measurements: int = 0

    @property
    def offset_hours(self) -> float:
        return round(self.offset.total_seconds() / 3600.0, 2)

    @property
    def source(self) -> str:
        return "manual" if self.manual else ("auto" if self.measured_at is not None else "assumed_utc")

    @staticmethod
    def from_hours(hours: float | None) -> "BrokerClock":
        """Manual override (`safety.broker_utc_offset_hours`); None = auto-detect."""
        if hours is None:
            return BrokerClock()
        return BrokerClock(offset=timedelta(hours=float(hours)), manual=True)

    def broker_wall_clock(self, epoch: float) -> datetime:
        """The raw MT5 epoch read as-is: the broker's wall clock, labelled UTC."""
        return datetime.fromtimestamp(float(epoch), tz=UTC)

    def to_utc(self, epoch: float) -> datetime:
        """Raw MT5 epoch seconds → true UTC."""
        return self.broker_wall_clock(epoch) - self.offset

    def convert(self, moment: datetime) -> datetime:
        """A datetime already read as broker-wall-clock-labelled-UTC → true UTC."""
        return moment.astimezone(UTC) - self.offset

    def to_broker(self, moment: datetime) -> datetime:
        """True UTC → the broker-labelled instant MT5 expects in history queries."""
        return moment.astimezone(UTC) + self.offset

    def needs_measurement(self, now: datetime) -> bool:
        if self.manual and self.measured_at is not None:
            return False                                    # manual offset never moves; residual is refreshed on validate
        return self.measured_at is None or (now - self.measured_at).total_seconds() >= REMEASURE_SECONDS

    def measure(self, epoch: float, now: datetime, server: str = "") -> "BrokerClock":
        """Detect (or, when manual, only verify) the offset from one tick.

        The detected offset is the broker-minus-system difference rounded to the nearest
        30 minutes; whatever is left over is the residual skew.
        """
        delta = (self.broker_wall_clock(epoch) - now.astimezone(UTC)).total_seconds()
        if not self.manual:
            self.offset = timedelta(seconds=round(delta / OFFSET_QUANTUM_SECONDS) * OFFSET_QUANTUM_SECONDS)
        self.residual_seconds = round(delta - self.offset.total_seconds(), 3)
        self.measured_at = now.astimezone(UTC)
        self.measurements += 1
        if server:
            self.server = server
        return self

    def payload(self) -> dict[str, Any]:
        return {"broker_utc_offset_hours": self.offset_hours, "broker_clock_residual_seconds": self.residual_seconds,
                "broker_clock_source": self.source, "broker_server": self.server,
                "broker_clock_measured_at": self.measured_at.isoformat() if self.measured_at else None}


def broker_epoch_to_utc(client: Any, epoch: float) -> datetime:
    """Convert a raw MT5 epoch through the client's broker clock wherever one is available.

    Modules that read epoch fields straight off an MT5 structure (position.time, deal.time)
    call this instead of `datetime.fromtimestamp(t, tz=UTC)` so there is exactly one
    conversion rule in the process.
    """
    clock = getattr(client, "clock", None)
    if isinstance(clock, BrokerClock):
        return clock.to_utc(epoch)
    return datetime.fromtimestamp(float(epoch), tz=UTC)


@dataclass(slots=True)
class Tick:
    time: datetime
    bid: float
    ask: float

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(slots=True)
class AccountState:
    login: int
    balance: float
    equity: float
    margin_free: float
    trade_allowed: bool
    is_demo: bool
    is_hedging: bool


@dataclass(slots=True)
class OrderResult:
    success: bool
    retcode: int
    ticket: int | None
    price: float | None
    message: str
    volume_filled: float = 0.0
    position_tickets: tuple[int, ...] = ()


class TradingClient(Protocol):
    clock: BrokerClock
    def get_tick(self, symbol: str) -> Tick: ...
    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame: ...
    def account_state(self) -> AccountState: ...
    def symbol_info(self, symbol: str) -> Any: ...
    def positions(self, symbol: str | None = None, magic: int | None = None) -> list[Any]: ...
    def send_market(
        self, symbol: str, side: str, volume: float, magic: int, comment: str,
        sl: float = 0.0, tp: float = 0.0,
    ) -> OrderResult: ...
    def close_position(self, ticket: int, expected_magic: int) -> OrderResult: ...
    def order_check(self, symbol: str, side: str, volume: float, sl: float = 0.0, tp: float = 0.0) -> tuple[bool, str]: ...
    def closed_deals(self, position_ticket: int, opened_at: datetime | None = None) -> list[dict]: ...
    def calc_profit(self, symbol: str, side: str, volume: float, price_open: float, price_close: float) -> float | None: ...
    def modify_sltp(self, ticket: int, sl: float, tp: float, expected_magic: int) -> OrderResult: ...
    def close_partial(self, ticket: int, volume: float, expected_magic: int, comment: str = "PA_PARTIAL") -> OrderResult: ...
    def account_id(self) -> str: ...


class MT5Client:
    TIMEFRAMES: dict[str, int] = {}

    def __init__(self, terminal_path: str | None = None, deal_history_max_days: int = 3650, symbol: str = "XAUUSD",
                 tick_max_age_seconds: int = 120, broker_utc_offset_hours: float | None = None) -> None:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package is unavailable; run this bot on Windows with MT5")
        self.terminal_path = terminal_path or None
        self.deal_history_max_days = deal_history_max_days
        self.symbol = symbol
        self.tick_max_age_seconds = tick_max_age_seconds
        self.clock = BrokerClock.from_hours(broker_utc_offset_hours)     # v3.3.0: broker-server time → true UTC
        self.validation: dict[str, Any] = {}
        self.TIMEFRAMES = {
            "M1": mt5.TIMEFRAME_M1,
            "M5": mt5.TIMEFRAME_M5,
            "M15": mt5.TIMEFRAME_M15,
            "H1": mt5.TIMEFRAME_H1,
            "H4": mt5.TIMEFRAME_H4,
            "D1": mt5.TIMEFRAME_D1,
        }

    def initialize(self) -> dict[str, Any]:
        """Attach to the already-running, already-logged-in MT5 terminal (v3.0.0).
        Never performs an MT5 log-in; never takes credentials. Raises with mt5.last_error() on failure."""
        ok = mt5.initialize(path=self.terminal_path) if self.terminal_path else mt5.initialize()
        if not ok:
            raise RuntimeError(f"MT5 initialization failed: {mt5.last_error()}")
        try:
            return self.validate_terminal()
        except Exception:
            try: mt5.shutdown()
            except Exception: pass
            raise

    def validate_terminal(self) -> dict[str, Any]:
        """Terminal → account (demo) → trading permitted → algo trading → hedging (informational) → symbol → fresh tick → volumes."""
        terminal = mt5.terminal_info(); account = mt5.account_info()
        if terminal is None: raise RuntimeError(f"terminal_info() unavailable: {mt5.last_error()}")
        if account is None: raise RuntimeError(f"account_info() unavailable: {mt5.last_error()}")
        if not getattr(terminal, "connected", False): raise RuntimeError("MT5 terminal is not connected to its broker")
        demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0); hedge_mode = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        is_demo = int(account.trade_mode) == demo_mode
        if not is_demo: raise RuntimeError(f"Connected account {account.login} is not a DEMO account; refusing to run")
        if not getattr(account, "trade_allowed", False): raise RuntimeError("Trading is not permitted on the connected account")
        if not getattr(terminal, "trade_allowed", False): raise RuntimeError("Algo Trading is disabled in the terminal (enable the AutoTrading button)")
        self.ensure_symbol(self.symbol)
        info = mt5.symbol_info(self.symbol)
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None: raise RuntimeError(f"No tick for {self.symbol}: {mt5.last_error()}")
        # v3.3.0: detect the broker timezone FIRST — otherwise a UTC+3 server looks like a 3-hour-stale tick.
        self.clock.measure(tick.time, datetime.now(UTC), str(getattr(account, "server", "")))
        age = (datetime.now(UTC) - self.clock.to_utc(tick.time)).total_seconds()
        if age > self.tick_max_age_seconds: raise RuntimeError(f"{self.symbol} tick is stale ({age:.0f}s old)")
        if float(info.volume_min) <= 0 or float(info.volume_step) <= 0 or float(info.volume_max) < float(info.volume_min):
            raise RuntimeError(f"Invalid volume constraints for {self.symbol}")
        self.validation = {
            "login": int(account.login), "server": str(getattr(account, "server", "")), "is_demo": is_demo,
            "is_hedging": int(getattr(account, "margin_mode", -1)) == hedge_mode, "trade_allowed": bool(account.trade_allowed),
            "algo_trading": bool(terminal.trade_allowed), "terminal_connected": bool(terminal.connected),
            "symbol": self.symbol, "symbol_visible": bool(info.visible), "tick_age_seconds": round(age, 1),
            "volume_min": float(info.volume_min), "volume_step": float(info.volume_step), "volume_max": float(info.volume_max),
            "terminal_path": self.terminal_path, "validated_at": datetime.now(UTC).isoformat(),
            **self.clock.payload(),
        }
        return self.validation

    def reconnect(self, attempts: int = 5, base_backoff_seconds: float = 2.0, sleep=None) -> dict[str, Any]:
        """shutdown → bounded back-off → initialize (with full validation). No credentials are ever used."""
        import time as _time
        sleep = sleep or _time.sleep
        last: Exception | None = None
        for attempt in range(attempts):
            try: mt5.shutdown()
            except Exception: pass
            sleep(min(60.0, base_backoff_seconds * (2 ** attempt)))
            try:
                return self.initialize()
            except Exception as exc:
                last = exc
        raise RuntimeError(f"MT5 reconnect failed after {attempts} attempts: {last}")

    def shutdown(self) -> None:
        mt5.shutdown()

    def ensure_symbol(self, symbol: str) -> None:
        info = mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"Symbol not found: {symbol}")
        if not info.visible and not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"Unable to select symbol: {symbol}")

    def symbol_info(self, symbol: str) -> Any:
        self.ensure_symbol(symbol)
        return mt5.symbol_info(symbol)

    def refresh_broker_clock(self, epoch: float | None = None, now: datetime | None = None) -> BrokerClock:
        """Re-measure the offset at most once per hour (and on every reconnect, through validate_terminal)."""
        now = now or datetime.now(UTC)
        if not self.clock.needs_measurement(now):
            return self.clock
        if epoch is None:
            value = mt5.symbol_info_tick(self.symbol)
            if value is None:
                return self.clock
            epoch = value.time
        return self.clock.measure(epoch, now)

    def get_tick(self, symbol: str) -> Tick:
        self.ensure_symbol(symbol)
        value = mt5.symbol_info_tick(symbol)
        if value is None:
            raise RuntimeError(f"No live tick for {symbol}: {mt5.last_error()}")
        self.refresh_broker_clock(value.time)
        return Tick(self.clock.to_utc(value.time), float(value.bid), float(value.ask))

    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self.ensure_symbol(symbol)
        if timeframe not in self.TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        rates = mt5.copy_rates_from_pos(symbol, self.TIMEFRAMES[timeframe], 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No {timeframe} data for {symbol}: {mt5.last_error()}")
        frame = pd.DataFrame(rates)
        # v3.3.0: bar stamps are broker-server epochs — subtract the detected offset so the
        # rest of the bot (sessions, freshness, sweep ages, cards) works in true UTC.
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True) - self.clock.offset
        return frame.sort_values("time").reset_index(drop=True)

    def account_state(self) -> AccountState:
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"Unable to read account: {mt5.last_error()}")
        demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)
        hedge_mode = getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2)
        return AccountState(
            login=int(info.login),
            balance=float(info.balance),
            equity=float(info.equity),
            margin_free=float(info.margin_free),
            trade_allowed=bool(info.trade_allowed),
            is_demo=int(info.trade_mode) == demo_mode,
            is_hedging=int(info.margin_mode) == hedge_mode,
        )

    def positions(self, symbol: str | None = None, magic: int | None = None) -> list[Any]:
        values = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        items = list(values or [])
        if magic is not None:
            items = [position for position in items if int(position.magic) == magic]
        return items

    def send_market(
        self, symbol: str, side: str, volume: float, magic: int, comment: str,
        sl: float = 0.0, tp: float = 0.0,
    ) -> OrderResult:
        tick = self.get_tick(symbol)
        is_buy = side.upper() in {"BUY", "LONG"}
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "price": price,
            "sl": float(sl),
            "tp": float(tp),
            "deviation": 20,
            "magic": int(magic),
            "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(symbol),
        }
        result = mt5.order_send(request)
        return self._order_result(result)

    def close_position(self, ticket: int, expected_magic: int) -> OrderResult:
        matches = mt5.positions_get(ticket=ticket)
        if not matches:
            return OrderResult(False, -1, ticket, None, "Position does not exist")
        position = matches[0]
        if int(position.magic) != int(expected_magic):
            return OrderResult(False, -2, ticket, None, "Magic-number mismatch; close refused")
        tick = self.get_tick(position.symbol)
        closing_buy = int(position.type) == mt5.POSITION_TYPE_SELL
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(position.ticket),
            "symbol": position.symbol,
            "volume": float(position.volume),
            "type": mt5.ORDER_TYPE_BUY if closing_buy else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if closing_buy else tick.bid,
            "deviation": 20,
            "magic": int(expected_magic),
            "comment": "SCOUT_SESSION_CLOSE",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(position.symbol),
        }
        return self._order_result(mt5.order_send(request))

    def order_check(self, symbol: str, side: str, volume: float, sl: float = 0.0, tp: float = 0.0) -> tuple[bool, str]:
        """Broker-side pre-validation: margin, volume, stops distance, trade mode."""
        info = self.symbol_info(symbol)
        if int(getattr(info, "trade_mode", 4)) == getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", 0):
            return False, "Symbol trading disabled"
        tick = self.get_tick(symbol)
        is_buy = side.upper() in {"BUY", "LONG"}
        price = tick.ask if is_buy else tick.bid
        point = float(getattr(info, "point", 0.01))
        stops = float(getattr(info, "trade_stops_level", 0)) * point; freeze = float(getattr(info, "trade_freeze_level", 0)) * point
        if sl and abs(price - sl) < max(stops, freeze): return False, f"SL inside stops/freeze level ({max(stops, freeze):.2f})"
        if tp and abs(tp - price) < max(stops, freeze): return False, f"TP inside stops/freeze level ({max(stops, freeze):.2f})"
        if sl and ((is_buy and sl >= price) or (not is_buy and sl <= price)): return False, "SL on the wrong side of price"
        if tp and ((is_buy and tp <= price) or (not is_buy and tp >= price)): return False, "TP on the wrong side of price"
        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
                   "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL, "price": price, "sl": float(sl), "tp": float(tp),
                   "deviation": 20, "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling_mode(symbol)}
        result = mt5.order_check(request)
        if result is None: return False, f"order_check returned None: {mt5.last_error()}"
        if int(result.retcode) != 0: return False, f"order_check {result.retcode}: {getattr(result, 'comment', '')}"
        if float(getattr(result, "margin_free", 1)) < 0: return False, "Insufficient free margin after trade"
        return True, "ok"

    def closed_deal(self, position_ticket: int, opened_at: datetime | None = None) -> dict | None:
        """Exit deal of a closed position (for P/L, exit price, exit reason)."""
        from datetime import timedelta
        start = (opened_at.astimezone(UTC) - timedelta(days=1)) if opened_at else datetime.now(UTC) - timedelta(days=self.deal_history_max_days)
        deals = mt5.history_deals_get(self.clock.to_broker(start), self.clock.to_broker(datetime.now(UTC) + timedelta(days=1)), position=position_ticket)
        if not deals: return None
        out = [d for d in deals if int(d.entry) == getattr(mt5, "DEAL_ENTRY_OUT", 1)]
        if not out: return None
        d = out[-1]
        reasons = {getattr(mt5, "DEAL_REASON_SL", 4): "SL", getattr(mt5, "DEAL_REASON_TP", 5): "TP", getattr(mt5, "DEAL_REASON_SO", 6): "STOP_OUT",
                   getattr(mt5, "DEAL_REASON_EXPERT", 3): "BOT", getattr(mt5, "DEAL_REASON_CLIENT", 0): "MANUAL"}
        return {"price": float(d.price), "profit": float(d.profit) + float(getattr(d, "commission", 0)) + float(getattr(d, "swap", 0)),
                "time": self.clock.to_utc(int(d.time)), "reason": reasons.get(int(d.reason), str(d.reason)), "volume": float(d.volume)}

    def closed_deals(self, position_ticket: int, opened_at: datetime | None = None) -> list[dict]:
        """All exit deals for a position, including partial exits and costs."""
        from datetime import timedelta
        start = (opened_at.astimezone(UTC) - timedelta(days=1)) if opened_at else datetime.now(UTC) - timedelta(days=self.deal_history_max_days)
        deals = mt5.history_deals_get(self.clock.to_broker(start), self.clock.to_broker(datetime.now(UTC) + timedelta(days=1)), position=position_ticket)
        if not deals:
            return []
        reasons = {getattr(mt5, "DEAL_REASON_SL", 4): "SL", getattr(mt5, "DEAL_REASON_TP", 5): "TP",
                   getattr(mt5, "DEAL_REASON_SO", 6): "STOP_OUT", getattr(mt5, "DEAL_REASON_EXPERT", 3): "BOT",
                   getattr(mt5, "DEAL_REASON_CLIENT", 0): "MANUAL"}
        out = []
        for d in deals:
            if int(d.entry) != getattr(mt5, "DEAL_ENTRY_OUT", 1):
                continue
            out.append({"deal_ticket": int(d.ticket), "price": float(d.price), "profit": float(d.profit),
                        "commission": float(getattr(d, "commission", 0)), "swap": float(getattr(d, "swap", 0)),
                        "net": float(d.profit) + float(getattr(d, "commission", 0)) + float(getattr(d, "swap", 0)),
                        "time": self.clock.to_utc(int(d.time)), "reason": reasons.get(int(d.reason), str(d.reason)),
                        "volume": float(d.volume)})
        return out

    def calc_profit(self, symbol: str, side: str, volume: float, price_open: float, price_close: float) -> float | None:
        is_buy = side.upper() in {"BUY", "LONG"}
        value = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL, symbol, float(volume), float(price_open), float(price_close))
        return float(value) if value is not None else None

    def account_id(self) -> str:
        info = mt5.account_info()
        return f"{info.login}@{info.server}" if info else "unknown"

    def modify_sltp(self, ticket: int, sl: float, tp: float, expected_magic: int) -> OrderResult:
        matches = mt5.positions_get(ticket=ticket)
        if not matches: return OrderResult(False, -1, ticket, None, "Position does not exist")
        position = matches[0]
        if int(position.magic) != int(expected_magic): return OrderResult(False, -2, ticket, None, "Magic-number mismatch; modify refused")
        request = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": position.symbol, "sl": float(sl), "tp": float(tp), "magic": int(expected_magic)}
        return self._order_result(mt5.order_send(request))

    def close_partial(self, ticket: int, volume: float, expected_magic: int, comment: str = "PA_PARTIAL") -> OrderResult:
        matches = mt5.positions_get(ticket=ticket)
        if not matches: return OrderResult(False, -1, ticket, None, "Position does not exist")
        position = matches[0]
        if int(position.magic) != int(expected_magic): return OrderResult(False, -2, ticket, None, "Magic-number mismatch; close refused")
        tick = self.get_tick(position.symbol)
        closing_buy = int(position.type) == mt5.POSITION_TYPE_SELL
        request = {"action": mt5.TRADE_ACTION_DEAL, "position": int(ticket), "symbol": position.symbol, "volume": float(min(volume, position.volume)),
                   "type": mt5.ORDER_TYPE_BUY if closing_buy else mt5.ORDER_TYPE_SELL, "price": tick.ask if closing_buy else tick.bid,
                   "deviation": 20, "magic": int(expected_magic), "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling_mode(position.symbol)}
        return self._order_result(mt5.order_send(request))

    def _filling_mode(self, symbol: str) -> int:
        """Map SYMBOL_FILLING_* capability flags (bit 1 = FOK, bit 2 = IOC) to the ORDER_FILLING_* enum
        (FOK=0, IOC=1, RETURN=2). These are different numbering schemes — v2.0.0 fix. Prefer IOC, then FOK;
        a symbol that advertises neither only accepts RETURN (exchange-execution symbols)."""
        info = mt5.symbol_info(symbol)
        flags = int(getattr(info, "filling_mode", 0) or 0)
        fok_flag = int(getattr(mt5, "SYMBOL_FILLING_FOK", 1)); ioc_flag = int(getattr(mt5, "SYMBOL_FILLING_IOC", 2))
        if flags & ioc_flag:
            return mt5.ORDER_FILLING_IOC
        if flags & fok_flag:
            return mt5.ORDER_FILLING_FOK
        return mt5.ORDER_FILLING_RETURN

    @staticmethod
    def _order_result(result: Any) -> OrderResult:
        if result is None:
            return OrderResult(False, -1, None, None, f"order_send returned None: {mt5.last_error()}")
        good = {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL}
        ticket = int(result.order or result.deal) if (result.order or result.deal) else None
        return OrderResult(
            int(result.retcode) in good,
            int(result.retcode),
            ticket,
            float(result.price) if result.price else None,
            str(getattr(result, "comment", "")),
        )
