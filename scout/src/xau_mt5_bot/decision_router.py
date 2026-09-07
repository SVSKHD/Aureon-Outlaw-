from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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


# ---- v3.3.0: DecisionExplanation ------------------------------------------------------------------------------
# The router above is still the only authorisation point. Everything below only EXPLAINS what it just did:
# the same inputs, in the same order, rendered as a gate table that a human can read in one screen.

GATE_ORDER = (
    "data fresh", "account safe", "clock", "spread", "setup exists", "confluence", "PA side gap",
    "inside zone", "trigger fresh", "market pace", "scouts", "RR", "target realism",
    "$ session feasibility", "day lock", "cooldown", "one-position rule",
)

FAMILY_LABELS = {"htf": "higher-timeframe trend", "structure": "structure events", "liquidity": "sweeps",
                 "location": "zone location", "momentum": "M5 momentum", "candle": "candles",
                 "intermarket": "silver (XAG)"}


@dataclass(slots=True)
class Gate:
    name: str
    passed: bool
    value: Any = None
    threshold: Any = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": bool(self.passed), "value": self.value,
                "threshold": self.threshold, "note": self.note}


def _skipped(name: str, note: str) -> Gate:
    """A gate that never got a chance to run because an earlier one failed — reported, never counted as passed."""
    return Gate(name, True, "not reached", None, note)


def evidence_lines(direction_detail: dict[str, Any] | None, intermarket: dict[str, Any] | None) -> dict[str, Any]:
    """The three strongest confluence families per side, plus the one-line silver verdict."""
    detail = direction_detail or {}
    def top(key: str) -> list[dict[str, Any]]:
        families = detail.get(key) or {}
        ranked = sorted(((k, int(v)) for k, v in families.items() if int(v) > 0), key=lambda kv: -kv[1])[:3]
        return [{"family": k, "label": FAMILY_LABELS.get(k, k), "points": v} for k, v in ranked]
    im = intermarket or {}
    if im.get("status") == "OK":
        silver = (f"XAG {im.get('regime', 'n/a')} r={im.get('correlation')} · SMT {im.get('smt', 'NONE')} · "
                  f"+{im.get('long_points', 0)}L/+{im.get('short_points', 0)}S")
    else:
        silver = f"silver evidence unavailable ({im.get('reason', 'disabled')})"
    return {"long": top("long_families"), "short": top("short_families"),
            "long_score": detail.get("long_score"), "short_score": detail.get("short_score"), "silver": silver}


def explain_decision(value: DecisionInput, decision: Decision, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the decision trace: verdict sentence, ordered gate table, what would flip it, evidence.

    `context` carries what the router receives already folded into `account_safe` (clock, day lock,
    cooldown, one-position rule) plus display-only extras (zone bounds, trigger time, session target).
    """
    ctx = context or {}
    side = value.pa_side.value if value.pa_side else None
    gates: list[Gate] = []
    nxt: list[str] = []

    def add(name: str, passed: bool, val: Any = None, threshold: Any = None, note: str = "", flip: str = "") -> bool:
        gates.append(Gate(name, passed, val, threshold, note))
        if not passed and flip:
            nxt.append(flip)
        return passed

    add("data fresh", value.freshness != Freshness.STALE, str(value.freshness.value), "not STALE",
        "newest M1 bar age", "a fresh M1 bar (check the feed and the broker clock offset)")
    clock_ok = bool(ctx.get("clock_ok", True))
    account_reason = str(ctx.get("account_reason", "") or "")
    add("account safe", bool(value.account_safe), "safe" if value.account_safe else account_reason, "safe",
        "trading permitted, tick fresh, risk gates clear", "the account/risk gate that is blocking to clear")
    add("clock", clock_ok, f"{ctx.get('residual_skew_seconds', 0.0):.0f}s residual", f"≤ {ctx.get('max_clock_skew_seconds', 600)}s",
        f"broker offset UTC{float(ctx.get('broker_utc_offset_hours', 0.0)):+g}h is applied before this check",
        "a corrected PC clock — the broker timezone itself is already converted")
    add("spread", value.spread_state != SpreadState.ABNORMAL, ctx.get("spread"), ctx.get("max_spread_price"),
        str(value.spread_state.value), f"spread at or below {ctx.get('max_spread_price')}")
    setup_ok = add("setup exists", bool(value.setup_valid and value.pa_side is not None), side or "no side", "a zone + a side",
                   str(ctx.get("zone_text", "")), "a valid zone with a directional bias")
    add("confluence", value.confluence >= value.min_confluence, value.confluence, value.min_confluence,
        f"{value.confluence}/100", f"{max(0, value.min_confluence - value.confluence)} more confluence points")
    gap = ctx.get("side_gap"); min_gap = ctx.get("min_side_gap", 8)
    if gap is None:
        gates.append(_skipped("PA side gap", "no direction scores this cycle"))
    else:
        add("PA side gap", gap >= min_gap, gap, min_gap, "LONG vs SHORT score separation",
            f"{max(0, min_gap - gap)} more points of separation between the LONG and SHORT scores")
    if not setup_ok:
        for name, note in (("inside zone", "no zone selected"), ("trigger fresh", "no zone to trigger on")):
            gates.append(_skipped(name, note))
    else:
        add("inside zone", value.entry_state in {EntryState.INSIDE, EntryState.CONFIRMED}, str(value.entry_state.value),
            "INSIDE / CONFIRMED", str(ctx.get("zone_text", "")), f"price back inside {ctx.get('zone_text', 'the zone')}")
        add("trigger fresh", bool(value.trigger.confirmed and value.trigger.fresh),
            "confirmed" if value.trigger.confirmed else "waiting", "fresh confirmation",
            str(value.trigger.reason or ""), "an M1 sweep+BOS or an M5 close confirmation on this zone visit")
    add("market pace", not (value.hold_when_slow and value.scout.market_speed == "SLOW"), value.scout.market_speed,
        "not SLOW while hold_when_slow", str(value.scout.guidance or ""), "pace above the SLOW threshold")
    add("scouts", not (value.scout.verdict == ScoutVerdict.CONTRADICTS and value.scout.strength >= value.scout_contradiction_threshold),
        f"{value.scout.verdict.value} {value.scout.strength}/10", f"< {value.scout_contradiction_threshold} when contradicting",
        f"leader {value.scout.leader}", "scout strength to fall or the leader to flip")
    add("RR", value.rr is not None and value.rr >= value.min_rr,
        None if value.rr is None else round(float(value.rr), 2), value.min_rr, "actual bid/ask RR to TP1",
        "a nearer TP1 or a tighter structural SL")
    add("target realism", value.target_realism not in {None, TargetRealism.UNLIKELY},
        None if value.target_realism is None else str(value.target_realism.value), "not UNLIKELY",
        "nearest structural target vs remaining daily range", "a closer structural target or more daily range")
    feasibility = ctx.get("session_target_verdict")
    add("$ session feasibility", feasibility not in {"UNLIKELY"}, feasibility or "n/a", "not UNLIKELY",
        f"{ctx.get('remaining_minutes', 0):.0f} min left in {ctx.get('session', 'the session')}",
        "more session time, a faster pace, or a smaller required move")
    add("day lock", not ctx.get("day_locked"), ctx.get("day_lock_reason") or "none", "no lock",
        "account-wide demo lock", "the next trading date, or the lock condition clearing")
    add("cooldown", not ctx.get("setup_cooldown"), ctx.get("cooldown_reason") or "clear", "clear",
        "per-setup re-entry rules", "the setup cooldown to expire")
    add("one-position rule", not ctx.get("position_open"), "open" if ctx.get("position_open") else "none",
        "no open PA position", "one PA position per symbol", "the open PA position to close")

    failed = [g for g in gates if not g.passed]
    action = decision.action.value
    if action in {"LONG", "SHORT"}:
        parts = [f"{action} authorised", str(ctx.get("zone_text", "zone"))]
        if value.trigger.confirmed:
            parts.append(f"{value.trigger.source} trigger {ctx.get('trigger_time', '')}".strip())
        parts.append(f"confluence {value.confluence}/100")
        if value.rr is not None:
            parts.append(f"RR {float(value.rr):.2f}")
        verdict = parts[0] + " — " + ", ".join(parts[1:])
    else:
        bias = f"{side} bias {value.confluence}/100" if side else f"no directional bias ({value.confluence}/100 best side)"
        blockers = " and ".join(f"{g.name} ({g.value})" for g in failed[:2]) or decision.reason
        verdict = f"{action.replace('_', ' ')} — {bias}; blocked by {blockers}"
    return {
        "verdict": verdict,
        "action": action,
        "reason": decision.reason,
        "gates": [g.as_dict() for g in gates],
        "blocked_by": [g.as_dict() for g in failed],
        "passed_count": len(gates) - len(failed),
        "gate_count": len(gates),
        "next": nxt or (["nothing — the router authorised this setup"] if action in {"LONG", "SHORT"} else []),
        "evidence": evidence_lines(ctx.get("direction_detail"), ctx.get("intermarket")),
        "go_meaning": ("GO means this demo cycle's PA setup passed every router gate — it is a signal and a session GO tally, "
                       "not a standing instruction to place an order."),
    }
