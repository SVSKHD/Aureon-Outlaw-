from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .models import (
    EntryState,
    LiquidityLevel,
    Side,
    StructureResult,
    SweepEvent,
    TargetRealism,
    TradePlan,
    Zone,
)


def select_entry_zone(
    zones: list[Zone], side: Side, price: float, now: datetime, atr: float,
) -> Zone | None:
    candidates: list[Zone] = []
    for zone in zones:
        if zone.side != side or zone.status.lower() in {"fully mitigated", "broken", "invalidated", "failed"}:
            continue
        age = now.astimezone(UTC) - zone.valid_from.astimezone(UTC)
        if age > timedelta(days=5):
            continue
        distance = abs(zone.midpoint - price) / max(atr, 1e-9)
        freshness = max(0.0, 3.0 - age.total_seconds() / 86400)
        untouched = 2.0 if zone.status == "untouched" else 0.5
        zone.score = zone.score + freshness + untouched - min(distance, 10.0)
        candidates.append(zone)
    return max(candidates, key=lambda item: item.score, default=None)


def classify_entry(price: float, zone: Zone | None, atr: float, side: Side | None) -> EntryState:
    if zone is None or side is None:
        return EntryState.NOT_IN_SETUP
    if zone.low <= price <= zone.high:
        return EntryState.INSIDE
    proximity = max(atr * 0.20, (zone.high - zone.low) * 0.5)
    if abs(price - (zone.low if price < zone.low else zone.high)) <= proximity:
        return EntryState.APPROACHING
    if price > zone.high:
        return EntryState.ABOVE
    return EntryState.BELOW


def _latest_protective_extreme(
    side: Side, sweeps: list[SweepEvent], structure: StructureResult,
) -> tuple[float | None, str]:
    wanted = "BULLISH" if side == Side.LONG else "BEARISH"
    matching = [event for event in sweeps if event.direction == wanted and event.active]
    if matching:
        latest = max(matching, key=lambda event: event.reclaim_time)
        return latest.sweep_price, f"beyond latest {latest.level_type} sweep"
    wanted_pivot = "LOW" if side == Side.LONG else "HIGH"
    pivots = [pivot for pivot in structure.pivots if pivot.kind == wanted_pivot]
    if pivots:
        latest = max(pivots, key=lambda pivot: pivot.confirmation_timestamp)
        return latest.price, f"beyond latest confirmed M5 swing {wanted_pivot.lower()}"
    return None, "no structural extreme"


def build_trade_plan(
    side: Side,
    execution_price: float,
    zone: Zone,
    m5_structure: StructureResult,
    sweeps: list[SweepEvent],
    liquidity: list[LiquidityLevel],
    atr: float,
    daily_atr: float,
    today_range: float,
    volume: float,
    min_rr: float = 1.0,
) -> TradePlan | None:
    extreme, reason = _latest_protective_extreme(side, sweeps, m5_structure)
    buffer = max(atr * 0.10, 0.01)
    if extreme is None:
        extreme = zone.low if side == Side.LONG else zone.high
        reason = f"outside {zone.kind}"
    stop = extreme - buffer if side == Side.LONG else extreme + buffer
    risk = abs(execution_price - stop)
    if risk <= 0 or (side == Side.LONG and stop >= execution_price) or (side == Side.SHORT and stop <= execution_price):
        return None

    usable = [level for level in liquidity if level.kind != "ROUND_1" and not level.kind.startswith("TODAY_")]
    min_gap = max(min_rr, 0.8) * risk                        # TP1 must satisfy the router's minimum RR by construction
    if side == Side.LONG:
        target_values = sorted({round(level.price, 2) for level in usable if level.price - execution_price >= min_gap})
    else:
        target_values = sorted({round(level.price, 2) for level in usable if execution_price - level.price >= min_gap}, reverse=True)
    # Only structural targets offering positive reward are considered; never fabricate TP slots.
    take_profits = target_values[:3]
    if not take_profits:
        return None
    rrs = [abs(target - execution_price) / risk for target in take_profits]
    remaining_ratio = max(0.0, daily_atr - today_range) / max(daily_atr, 1e-9)
    first_distance = abs(take_profits[0] - execution_price)
    remaining_move = max(0.0, daily_atr - today_range)
    if first_distance <= remaining_move or remaining_ratio >= 0.50:
        realism = TargetRealism.REALISTIC
    elif first_distance <= remaining_move * 1.5:
        realism = TargetRealism.AGGRESSIVE
    else:
        realism = TargetRealism.UNLIKELY
    invalidation = (
        f"M5 close below {zone.low:.2f}" if side == Side.LONG else f"M5 close above {zone.high:.2f}"
    )
    return TradePlan(side, execution_price, stop, take_profits, invalidation, reason, rrs, realism, volume)
