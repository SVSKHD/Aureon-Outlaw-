from __future__ import annotations

import math
import time
from typing import Any

from .config import BotConfig
from .models import Action, Decision, TradePlan
from .mt5_client import OrderResult, TradingClient

RETRYABLE = {10004, 10006, 10007, 10021, 10024, 10027, 10031}   # requote, reject, cancelled, prices changed, too many, autotrade off, connection


def normalize_volume(value: float, info: Any) -> float:
    minimum = float(info.volume_min); maximum = float(info.volume_max); step = float(info.volume_step)
    clamped = min(max(value, minimum), maximum)
    steps = math.floor((clamped - minimum) / step + 1e-9)
    return round(minimum + steps * step, 8)


def normalize_price(value: float, info: Any) -> float:
    """Round to the symbol's tick size / digits (item 12)."""
    tick = float(getattr(info, "trade_tick_size", 0) or 0) or float(getattr(info, "point", 0.01) or 0.01)
    digits = int(getattr(info, "digits", 2) or 2)
    return round(round(value / tick) * tick, digits)


def risk_per_lot(client: TradingClient, config: BotConfig, side: str, entry: float, stop: float) -> float:
    """Broker-accurate loss for 1 lot from entry to SL via order_calc_profit; tick-value fallback."""
    calc = getattr(client, "calc_profit", None)
    if calc is not None:
        try:
            value = calc(config.symbol, side, 1.0, entry, stop)
            if value is not None and value < 0:
                return -float(value)
        except Exception:
            pass
    info = client.symbol_info(config.symbol)
    contract = float(getattr(info, "trade_contract_size", 100.0))
    tick_size = float(getattr(info, "trade_tick_size", getattr(info, "point", 0.01)) or 0.01)
    tick_value = float(getattr(info, "trade_tick_value", contract * tick_size) or contract * tick_size)
    return abs(entry - stop) / tick_size * tick_value


def risk_based_volume(client: TradingClient, config: BotConfig, risk_price: float, side: str = "LONG",
                      entry: float | None = None, stop: float | None = None) -> tuple[float, str]:
    """Lot from pa_risk_percent of balance/equity over the broker-calculated loss to SL (item 11).
    Returns volume 0 when even the minimum broker volume would exceed the permitted risk."""
    info = client.symbol_info(config.symbol)
    if config.risk.pa_risk_percent <= 0 or risk_price <= 0:
        return normalize_volume(config.risk.fixed_pa_lot, info), "fixed lot"
    account = client.account_state()
    basis = account.equity if config.risk.size_basis == "equity" else account.balance
    e = entry if entry is not None else 0.0; s = stop if stop is not None else e - risk_price
    per_lot = risk_per_lot(client, config, side, e, s) if entry is not None else risk_price / 0.01 * float(getattr(info, "trade_tick_value", 1.0) or 1.0)
    if per_lot <= 0:
        return normalize_volume(config.risk.fixed_pa_lot, info), "fixed lot (bad risk calc)"
    money = basis * config.risk.pa_risk_percent / 100
    raw = money / per_lot
    volume = normalize_volume(raw, info)
    if float(info.volume_min) * per_lot > money * 1.05:
        return 0.0, f"minimum volume {info.volume_min} risks {float(info.volume_min) * per_lot:.2f} > permitted {money:.2f}"
    return volume, f"{config.risk.pa_risk_percent}% of {config.risk.size_basis} {basis:.0f} / {per_lot:.2f} per lot = {raw:.3f} → {volume}"


def account_is_safe(client: TradingClient, config: BotConfig, for_scouts: bool = False, proposed_volume: float = 0.0) -> tuple[bool, str]:
    try:
        account = client.account_state(); info = client.symbol_info(config.symbol)
    except Exception as exc:
        return False, str(exc)
    if not account.trade_allowed: return False, "Account trading is not allowed"
    if not account.is_demo: return False, "Live account is not authorized; this bot is demo-only"
    if for_scouts and config.safety.require_hedging_for_scouts and not account.is_hedging: return False, "SCOUTS DISABLED — ACCOUNT IS NETTING MODE"
    if float(info.volume_min) <= 0 or float(info.volume_step) <= 0: return False, "Invalid symbol volume constraints"
    total = sum(float(position.volume) for position in client.positions(config.symbol))
    if total + proposed_volume > config.risk.max_total_volume + 1e-9:
        return False, f"Total volume {total:.2f} + {proposed_volume:.2f} exceeds limit {config.risk.max_total_volume}"
    if account.margin_free <= 0: return False, "No free margin"
    return True, "Account safety checks passed"


def send_with_retry(client: TradingClient, config: BotConfig, symbol: str, side: str, volume: float, magic: int, comment: str,
                    sl: float = 0.0, tp: float = 0.0, audit=None) -> OrderResult:
    """Validate, send, and accept only the volume confirmed in MT5 positions.

    A partial fill is deliberately not topped up: on hedging accounts that could
    create another ticket and accidentally exceed the requested trade group.
    """
    info = client.symbol_info(symbol)
    volume = normalize_volume(volume, info); sl = normalize_price(sl, info) if sl else 0.0; tp = normalize_price(tp, info) if tp else 0.0
    check = getattr(client, "order_check", None)
    if check is not None:
        ok, why = check(symbol, side, volume, sl, tp)
        if not ok:
            return OrderResult(False, -20, None, None, f"order_check failed: {why}")
    result = OrderResult(False, -1, None, None, "not sent")
    for attempt in range(config.safety.retry_count + 1):
        result = client.send_market(symbol, side, volume, magic, comment, sl, tp)
        if audit: audit("order_attempt", {"comment": comment, "side": side, "volume": volume, "attempt": attempt + 1, "retcode": result.retcode, "success": result.success, "message": result.message})
        if result.success or result.retcode not in RETRYABLE:
            break
        time.sleep(0.5 * (attempt + 1))
    if not result.success:
        return result
    filled = 0.0; mine = []
    for _ in range(5):                                      # verify the position is really there
        mine = [p for p in client.positions(symbol, magic) if str(getattr(p, "comment", ""))[:31] == comment[:31] or (result.ticket and int(p.ticket) == result.ticket)]
        if mine:
            filled = min(volume, sum(float(p.volume) for p in mine)); break
        time.sleep(0.3)
    if filled <= 0:
        return OrderResult(False, -21, result.ticket, result.price, "Order reported done but no position found")
    if filled + 1e-9 < volume:
        result.message = f"PARTIAL FILL ACCEPTED {filled:.2f}/{volume:.2f}; no top-up sent"
    result.volume_filled = filled
    result.position_tickets = tuple(int(p.ticket) for p in mine)
    if mine:
        result.price = sum(float(p.price_open) * float(p.volume) for p in mine) / max(sum(float(p.volume) for p in mine), 1e-9)
    return result


def execute_pa_trade(client: TradingClient, config: BotConfig, decision: Decision, plan: TradePlan, audit=None) -> OrderResult:
    if decision.action not in {Action.LONG, Action.SHORT}:
        return OrderResult(False, -10, None, None, "Final router did not authorize a PA trade")
    if not config.safety.allow_pa_orders:
        return OrderResult(False, -11, None, None, "PA orders disabled by configuration")
    if config.safety.one_pa_position_per_symbol and client.positions(config.symbol, config.magic.pa):
        return OrderResult(False, -13, None, None, "Existing PA position prevents duplicate")
    info = client.symbol_info(config.symbol)
    plan.entry = normalize_price(plan.entry, info); plan.requested_entry = plan.entry
    plan.stop_loss = normalize_price(plan.stop_loss, info); plan.initial_stop_loss = plan.stop_loss
    plan.take_profits = [normalize_price(t, info) for t in plan.take_profits]
    risk_price = abs(plan.entry - plan.stop_loss)
    if risk_price > config.risk.max_sl_distance_price:
        return OrderResult(False, -15, None, None, f"SL distance ${risk_price:.2f} exceeds max {config.risk.max_sl_distance_price}")
    volume, sizing = risk_based_volume(client, config, risk_price, decision.action.value, plan.entry, plan.stop_loss)
    if volume <= 0:
        return OrderResult(False, -16, None, None, f"Sizing rejected: {sizing}")
    volume = normalize_volume(min(volume, config.risk.max_total_volume), info)
    safe, reason = account_is_safe(client, config, proposed_volume=volume)
    if not safe:
        return OrderResult(False, -12, None, None, reason)
    plan.volume = volume
    partial_enabled = config.management.partial_tp1_percent > 0 or config.management.partial_tp2_percent > 0
    broker_tp = 0.0 if partial_enabled else (plan.take_profits[0] if plan.take_profits else 0.0)
    result = send_with_retry(client, config, config.symbol, decision.action.value, volume, config.magic.pa,
                             f"PA_{decision.action.value}", plan.stop_loss, broker_tp, audit)
    if result.success and getattr(result, "volume_filled", volume) != volume:
        plan.volume = float(result.volume_filled)      # real position size after a partial fill
    if result.success and result.price is not None:
        plan.actual_entry = normalize_price(float(result.price), info)
        plan.entry = plan.actual_entry
        risk = abs(plan.actual_entry - plan.stop_loss)
        plan.actual_rr = [abs(tp - plan.actual_entry) / risk for tp in plan.take_profits] if risk > 0 else []
    result.message = f"{result.message} | sizing: {sizing}"
    return result
