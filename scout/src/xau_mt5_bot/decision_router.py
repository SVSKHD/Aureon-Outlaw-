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


# --- v3.3.0: the same veto ladder, enumerated so a NO-GO explains itself -------------------------------------------
VETO_ORDER = ("spread", "confluence", "zone", "trigger", "slow", "scouts", "rr", "target",
              "session_feasibility", "clock", "day_lock")


def router_vetoes(value: DecisionInput, gate: dict | None = None) -> list[dict]:
    """Every router veto in evaluation order, each with whether it is currently active, the numbers behind it
    and what would have to change to clear it. `gate` carries the engine-level order gate
    (`clock_ok`, `clock_detail`, `day_lock`, `day_lock_detail`, `remaining_minutes`, `minimum_remaining_minutes`)."""
    gate = gate or {}
    scout = value.scout
    rr_text = f"{value.rr:.2f}" if isinstance(value.rr, (int, float)) else "n/a"
    entry_blocked = value.entry_state in {EntryState.NOT_IN_SETUP, EntryState.ABOVE, EntryState.BELOW,
                                          EntryState.APPROACHING, EntryState.MISSED}
    remaining = gate.get("remaining_minutes")
    minimum_remaining = gate.get("minimum_remaining_minutes")
    checks = [
        ("spread", value.spread_state == SpreadState.ABNORMAL,
         f"spread {gate.get('spread', 'n/a')} vs limit {gate.get('max_spread', 'n/a')}",
         "spread falls back to or below the configured maximum"),
        ("confluence", value.confluence < value.min_confluence,
         f"confluence {value.confluence}/100 < {value.min_confluence}",
         f"confluence reaches {value.min_confluence} (more structure / sweep / zone agreement)"),
        ("zone", entry_blocked,
         f"entry state {value.entry_state.value}",
         "price trades back inside the selected zone and produces a valid entry"),
        ("trigger", not (value.trigger.confirmed and value.trigger.fresh),
         f"trigger confirmed={value.trigger.confirmed} fresh={value.trigger.fresh}",
         "a fresh M1/M5 confirmation prints for the current zone visit"),
        ("slow", bool(value.hold_when_slow and scout.market_speed == "SLOW"),
         f"pace {scout.market_speed}",
         "session pace rises out of SLOW (or the session turns over)"),
        ("scouts", value.scout.verdict == ScoutVerdict.CONTRADICTS and scout.strength >= value.scout_contradiction_threshold,
         f"scouts {scout.verdict.value} at strength {scout.strength}/10 (leader {scout.leader})",
         f"scout contradiction strength drops below {value.scout_contradiction_threshold}, or the leader flips"),
        ("rr", value.rr is None or value.rr < value.min_rr,
         f"actual RR {rr_text} < {value.min_rr}",
         f"a tighter stop or a further structural TP lifts actual RR to {value.min_rr}"),
        ("target", value.target_realism in {None, TargetRealism.UNLIKELY},
         f"target realism {getattr(value.target_realism, 'value', 'n/a')}",
         "the nearest structural target becomes reachable within the remaining session range"),
        ("session_feasibility",
         bool(minimum_remaining is not None and remaining is not None and remaining < minimum_remaining),
         f"{remaining if remaining is None else round(float(remaining))} min left vs {minimum_remaining} min minimum",
         "the next session opens (this one no longer has enough time)"),
        ("clock", not gate.get("clock_ok", True),
         str(gate.get("clock_detail", "broker clock skew")),
         "system UTC and the broker clock agree once the detected broker offset is removed"),
        ("day_lock", bool(gate.get("day_lock")),
         str(gate.get("day_lock_detail", "daily risk lock active")),
         "the next trading date rolls over (17:00 New York)"),
    ]
    return [{"veto": name, "active": bool(active), "detail": detail, "flips_when": flip}
            for name, active, detail, flip in checks]


def blocked_by(value: DecisionInput, gate: dict | None = None) -> list[dict]:
    """Only the vetoes currently standing in the way, in evaluation order."""
    return [item for item in router_vetoes(value, gate) if item["active"]]
