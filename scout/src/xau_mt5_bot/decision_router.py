from __future__ import annotations

from datetime import UTC, datetime

from .models import (
    Action,
    Decision,
    DecisionInput,
    EntryState,
    ScoutVerdict,
    SpreadState,
    Freshness,
    TargetRealism,
)


def final_decision_router(value: DecisionInput, now: datetime | None = None) -> Decision:
    """The one and only authorization point for a directional PA order."""
    timestamp = (now or datetime.now(UTC)).astimezone(UTC)
    if value.trigger.consumed:
        return Decision(Action.NO_TRADE, "Trigger was already consumed", timestamp)
    if value.setup_id is not None and (value.trigger.setup_id != value.setup_id or value.trigger.direction != value.pa_side):
        return Decision(Action.NO_TRADE, "Trigger does not belong to the selected setup", timestamp)
    if value.freshness == Freshness.STALE:
        return Decision(Action.NO_TRADE, "M1 data is stale", timestamp)
    if not value.account_safe:
        return Decision(Action.NO_TRADE, "Account or execution safety check failed", timestamp)
    if value.spread_state == SpreadState.ABNORMAL:
        return Decision(Action.NO_TRADE, "Spread exceeds configured maximum", timestamp)
    if not value.setup_valid or value.pa_side is None:
        return Decision(Action.NO_TRADE, "No valid price-action setup", timestamp)
    if value.confluence < value.min_confluence:
        return Decision(Action.WAIT, "Price-action confluence is below threshold", timestamp)
    if value.entry_state in {EntryState.NOT_IN_SETUP, EntryState.ABOVE, EntryState.BELOW, EntryState.APPROACHING}:
        return Decision(Action.WAIT, "Price has not produced a valid entry", timestamp)
    if value.entry_state == EntryState.MISSED:
        return Decision(Action.WAIT, "Entry was missed", timestamp)
    if not value.trigger.confirmed or not value.trigger.fresh:
        return Decision(Action.WAIT, "No fresh M1/M5 confirmation for the current zone visit", timestamp)
    if value.hold_when_slow and value.scout.market_speed == "SLOW":
        return Decision(Action.WAIT, "Market is SLOW — HOLD; wait for the next session unless velocity improves", timestamp)
    if value.scout.verdict == ScoutVerdict.CONTRADICTS and value.scout.strength >= value.scout_contradiction_threshold:
        return Decision(Action.WAIT, "Session scouts strongly contradict the PA direction", timestamp)
    if value.rr is None or value.rr < value.min_rr:
        return Decision(Action.WAIT, "Actual bid/ask risk-reward is below threshold", timestamp)
    if value.target_realism in {None, TargetRealism.UNLIKELY}:
        return Decision(Action.WAIT, "Nearest structural target is unrealistic for remaining daily range", timestamp)
    return Decision(Action(value.pa_side.value), "All deterministic setup and safety checks passed", timestamp)
