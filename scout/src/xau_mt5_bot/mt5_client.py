from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import pandas as pd

try:
    import MetaTrader5 as mt5  # type: ignore
except ImportError:  # pragma: no cover - expected on non-Windows test systems
    mt5 = None


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



OFFSET_QUANTUM_MINUTES = 30
OFFSET_CONFIDENCE_SECONDS = 120
"""A broker's server clock is NTP-synced, so the raw delta should land within a couple of minutes of its
timezone. A larger leftover means the quantum picked may have absorbed genuine drift (a PC 25 minutes slow on a
UTC+3 broker looks exactly like a correct clock on a UTC+3:30 broker), so the reading is flagged unconfident."""
"""Broker server clocks sit on whole- or half-hour UTC offsets, so the raw tick-vs-system delta is
rounded to the nearest 30 minutes. Whatever is left over is genuine skew, not a timezone."""


def quantize_offset(delta: timedelta, quantum_minutes: int = OFFSET_QUANTUM_MINUTES) -> timedelta:
    """Round a raw (broker wall clock − system UTC) delta to the nearest broker timezone offset."""
    step = quantum_minutes * 60
    return timedelta(seconds=round(delta.total_seconds() / step) * step)


class BrokerClock:
    """The single conversion point between broker server time and true UTC (v3.3.0).

    MetaTrader5 reports `symbol_info_tick().time`, `copy_rates_*()['time']`, deal times and position
    times as epoch seconds of the BROKER SERVER's wall clock, not of UTC. A broker on UTC+3 therefore
    looks like a 10 800 s clock skew to anything that compares those values with `datetime.now(UTC)`.

    `broker_utc_offset` is that timezone offset; `residual_seconds` is what is left after removing it,
    which is the only number the order-safety clock guard should ever look at.
    """

    def __init__(self, manual_offset_hours: float | None = None, remeasure_seconds: float = 3600.0) -> None:
        self.manual_offset_hours = manual_offset_hours
        self.remeasure_seconds = remeasure_seconds
        self.broker_utc_offset: timedelta = (
            timedelta(hours=float(manual_offset_hours)) if manual_offset_hours is not None else timedelta(0)
        )
        self.offset_source: str = "manual" if manual_offset_hours is not None else "unmeasured"
        self.residual_seconds: float = 0.0
        self.raw_delta_seconds: float = 0.0
        self.measured_at: datetime | None = None
        self.offset_confident: bool = True
        self.server: str = ""

    # -- conversion ----------------------------------------------------------------------------
    def to_utc(self, moment: datetime) -> datetime:
        """Broker-server timestamp (read as if it were UTC) → true UTC."""
        return moment - self.broker_utc_offset

    def to_broker(self, moment: datetime) -> datetime:
        """True UTC → the broker-server timestamp MT5 expects in history queries."""
        return moment + self.broker_utc_offset

    def epoch_to_utc(self, epoch: float) -> datetime:
        return self.to_utc(datetime.fromtimestamp(float(epoch), tz=UTC))

    def frame_to_utc(self, series: "pd.Series") -> "pd.Series":
        return series - pd.Timedelta(self.broker_utc_offset)

    # -- measurement ---------------------------------------------------------------------------
    def due(self, now: datetime) -> bool:
        if self.manual_offset_hours is not None:
            return self.measured_at is None                      # measured once, only to report the residual
        return self.measured_at is None or (now - self.measured_at).total_seconds() >= self.remeasure_seconds

    def measure(self, broker_epoch: float, now: datetime, server: str = "") -> dict[str, Any]:
        """Detect the offset from one broker timestamp. Manual override wins; the residual is always recomputed."""
        raw = datetime.fromtimestamp(float(broker_epoch), tz=UTC)
        delta = raw - now.astimezone(UTC)
        self.raw_delta_seconds = delta.total_seconds()
        if self.manual_offset_hours is not None:
            self.broker_utc_offset = timedelta(hours=float(self.manual_offset_hours))
            self.offset_source = "manual"
        else:
            candidate = quantize_offset(delta)
            # Every MT5 server timezone lives on the half-hour grid. A difference that does NOT sit on that grid
            # is a broken clock, not a timezone: refuse it rather than invent an offset from it (master PR #3).
            on_grid = abs((delta - candidate).total_seconds()) <= OFFSET_CONFIDENCE_SECONDS
            # And a broker timezone changes by whole hours (DST). A sub-hour "change" against an offset we
            # already hold is drift in one of the two clocks, so keep the established offset and let it show
            # as skew — otherwise a slow PC would be absorbed and every timestamp would shift with it.
            sub_hour_drift = (self.measured_at is not None
                              and 0 < abs((candidate - self.broker_utc_offset).total_seconds()) < 3600)
            if not on_grid or sub_hour_drift:
                candidate = self.broker_utc_offset
            self.broker_utc_offset = candidate
            self.offset_source = "auto"
        self.residual_seconds = (delta - self.broker_utc_offset).total_seconds()
        self.offset_confident = abs(self.residual_seconds) <= OFFSET_CONFIDENCE_SECONDS
        self.measured_at = now.astimezone(UTC)
        if server:
            self.server = server
        return self.info()

    def invalidate(self) -> None:
        """Forget the measurement so the next cycle re-detects (used on reconnect)."""
        self.measured_at = None

    def info(self) -> dict[str, Any]:
        return {
            "offset_hours": round(self.broker_utc_offset.total_seconds() / 3600.0, 2),
            "offset_seconds": round(self.broker_utc_offset.total_seconds(), 1),
            "residual_seconds": round(self.residual_seconds, 1),
            "raw_delta_seconds": round(self.raw_delta_seconds, 1),
            "source": self.offset_source,
            "confident": self.offset_confident,
            "confidence_limit_seconds": OFFSET_CONFIDENCE_SECONDS,
            "broker_time_utc": (datetime.now(UTC) + self.broker_utc_offset + timedelta(seconds=self.residual_seconds)).isoformat(),
            "server": self.server,
            "measured_at": self.measured_at.isoformat() if self.measured_at else None,
        }


def position_open_time(client: Any, position: Any, default: datetime) -> datetime:
    """MT5 `position.time` is broker-server epoch seconds. Convert it through the client's BrokerClock so
    adopted/tracked positions carry true UTC open times (v3.3.0). Clients without a clock fall back to raw UTC."""
    raw = getattr(position, "time", None)
    if not isinstance(raw, (int, float)):
        return default
    clock = getattr(client, "clock", None)
    if isinstance(clock, BrokerClock):
        return clock.epoch_to_utc(raw)
    return datetime.fromtimestamp(float(raw), tz=UTC)


class TradingClient(Protocol):
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
    def broker_clock(self) -> dict[str, Any]: ...
    def refresh_broker_offset(self, now: datetime | None = None, force: bool = False) -> dict[str, Any]: ...


class MT5Client:
    TIMEFRAMES: dict[str, int] = {}

    def __init__(self, terminal_path: str | None = None, deal_history_max_days: int = 3650, symbol: str = "XAUUSD",
                 tick_max_age_seconds: int = 120, broker_utc_offset_hours: float | None = None,
                 offset_remeasure_seconds: float = 3600.0,
                 broker_timestamp_offset_seconds: float | None = None) -> None:
        # `broker_timestamp_offset_seconds` is the legacy seconds form of the same pin (master PR #2/#3 API).
        if broker_utc_offset_hours is None and broker_timestamp_offset_seconds is not None:
            broker_utc_offset_hours = float(broker_timestamp_offset_seconds) / 3600.0
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package is unavailable; run this bot on Windows with MT5")
        self.terminal_path = terminal_path or None
        self.deal_history_max_days = deal_history_max_days
        self.symbol = symbol
        self.tick_max_age_seconds = tick_max_age_seconds
        self.validation: dict[str, Any] = {}
        self.clock = BrokerClock(broker_utc_offset_hours, offset_remeasure_seconds)
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
        # v3.3.0: MT5 timestamps are broker-server wall clock. Detect the timezone offset FIRST, then judge
        # staleness on what is left over — otherwise a UTC+3 broker looks 3 h stale at every startup.
        clock = self.clock.measure(tick.time, datetime.now(UTC), server=str(getattr(account, "server", "")))
        age = (datetime.now(UTC) - self.clock.epoch_to_utc(tick.time)).total_seconds()
        if age < -OFFSET_CONFIDENCE_SECONDS:
            raise RuntimeError(f"{self.symbol} tick is {-age:.0f}s in the future after the {clock['offset_hours']:+g} h broker "
                               f"offset — that difference is not a timezone; check the PC clock")
        if age > self.tick_max_age_seconds: raise RuntimeError(f"{self.symbol} tick is stale ({age:.0f}s old after removing the {clock['offset_hours']:+g} h broker offset)")
        if float(info.volume_min) <= 0 or float(info.volume_step) <= 0 or float(info.volume_max) < float(info.volume_min):
            raise RuntimeError(f"Invalid volume constraints for {self.symbol}")
        self.validation = {
            "login": int(account.login), "server": str(getattr(account, "server", "")), "is_demo": is_demo,
            "is_hedging": int(getattr(account, "margin_mode", -1)) == hedge_mode, "trade_allowed": bool(account.trade_allowed),
            "algo_trading": bool(terminal.trade_allowed), "terminal_connected": bool(terminal.connected),
            "symbol": self.symbol, "symbol_visible": bool(info.visible), "tick_age_seconds": round(age, 1),
            "volume_min": float(info.volume_min), "volume_step": float(info.volume_step), "volume_max": float(info.volume_max),
            "terminal_path": self.terminal_path, "validated_at": datetime.now(UTC).isoformat(),
            "broker_utc_offset_hours": clock["offset_hours"], "broker_clock_residual_seconds": clock["residual_seconds"],
            "broker_clock_source": clock["source"],
        }
        return self.validation

    def reconnect(self, attempts: int = 5, base_backoff_seconds: float = 2.0, sleep=None) -> dict[str, Any]:
        """shutdown → bounded back-off → initialize (with full validation). No credentials are ever used."""
        import time as _time
        sleep = sleep or _time.sleep
        self.clock.invalidate()                       # v3.3.0: a new server/session may sit on a different offset
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

    def get_tick(self, symbol: str) -> Tick:
        self.ensure_symbol(symbol)
        value = mt5.symbol_info_tick(symbol)
        if value is None:
            raise RuntimeError(f"No live tick for {symbol}: {mt5.last_error()}")
        return Tick(self.clock.epoch_to_utc(value.time), float(value.bid), float(value.ask))

    def get_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        self.ensure_symbol(symbol)
        if timeframe not in self.TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        rates = mt5.copy_rates_from_pos(symbol, self.TIMEFRAMES[timeframe], 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"No {timeframe} data for {symbol}: {mt5.last_error()}")
        frame = pd.DataFrame(rates)
        # v3.3.0: MT5 bar stamps are broker-server wall clock read as epoch seconds. Subtract the detected
        # broker offset here so every downstream consumer (sessions, freshness, sweeps, cards) sees true UTC.
        frame["time"] = self.clock.frame_to_utc(pd.to_datetime(frame["time"], unit="s", utc=True))
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
        # MT5 interprets history_deals_get() bounds in broker-server time (v3.3.0).
        deals = mt5.history_deals_get(self.clock.to_broker(start), self.clock.to_broker(datetime.now(UTC) + timedelta(days=1)), position=position_ticket)
        if not deals: return None
        out = [d for d in deals if int(d.entry) == getattr(mt5, "DEAL_ENTRY_OUT", 1)]
        if not out: return None
        d = out[-1]
        reasons = {getattr(mt5, "DEAL_REASON_SL", 4): "SL", getattr(mt5, "DEAL_REASON_TP", 5): "TP", getattr(mt5, "DEAL_REASON_SO", 6): "STOP_OUT",
                   getattr(mt5, "DEAL_REASON_EXPERT", 3): "BOT", getattr(mt5, "DEAL_REASON_CLIENT", 0): "MANUAL"}
        return {"price": float(d.price), "profit": float(d.profit) + float(getattr(d, "commission", 0)) + float(getattr(d, "swap", 0)),
                "time": self.clock.epoch_to_utc(int(d.time)), "reason": reasons.get(int(d.reason), str(d.reason)), "volume": float(d.volume)}

    def closed_deals(self, position_ticket: int, opened_at: datetime | None = None) -> list[dict]:
        """All exit deals for a position, including partial exits and costs."""
        from datetime import timedelta
        start = (opened_at.astimezone(UTC) - timedelta(days=1)) if opened_at else datetime.now(UTC) - timedelta(days=self.deal_history_max_days)
        # MT5 interprets history_deals_get() bounds in broker-server time (v3.3.0).
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
                        "time": self.clock.epoch_to_utc(int(d.time)), "reason": reasons.get(int(d.reason), str(d.reason)),
                        "volume": float(d.volume)})
        return out

    def calc_profit(self, symbol: str, side: str, volume: float, price_open: float, price_close: float) -> float | None:
        is_buy = side.upper() in {"BUY", "LONG"}
        value = mt5.order_calc_profit(mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL, symbol, float(volume), float(price_open), float(price_close))
        return float(value) if value is not None else None

    def broker_clock(self) -> dict[str, Any]:
        return self.clock.info()

    def refresh_broker_offset(self, now: datetime | None = None, force: bool = False) -> dict[str, Any]:
        """Re-detect the broker timezone offset at most once an hour (and on reconnect / when forced).
        Always recomputes the residual skew from the live tick, which is what the order clock guard reads."""
        now = (now or datetime.now(UTC)).astimezone(UTC)
        if not (force or self.clock.due(now)):
            value = mt5.symbol_info_tick(self.symbol)
            if value is not None:
                raw = datetime.fromtimestamp(value.time, tz=UTC)
                self.clock.raw_delta_seconds = (raw - now).total_seconds()
                self.clock.residual_seconds = (raw - self.clock.broker_utc_offset - now).total_seconds()
            return self.clock.info()
        value = mt5.symbol_info_tick(self.symbol)
        if value is None:
            raise RuntimeError(f"No live tick for {self.symbol}: {mt5.last_error()}")
        info = mt5.account_info()
        return self.clock.measure(value.time, now, server=str(getattr(info, "server", "")) if info else "")

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
