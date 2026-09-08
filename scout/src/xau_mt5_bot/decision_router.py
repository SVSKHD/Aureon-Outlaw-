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


# ---- v3.4.0: DecisionExplanation ------------------------------------------------------------------------------
# The router above is still the only authorisation point. Everything below only EXPLAINS what it just did.
# The router stops at its first failing check, and so does this: exactly one gate is marked FAIL (the one that
# actually blocked the decision), everything before it PASSED and everything after it was never evaluated.

GATE_ORDER = (
    "data fresh", "clock + account", "spread", "confluence", "side gap", "inside zone", "trigger",
    "pace", "scouts", "RR", "target realism", "session feasibility", "risk locks", "session time",
)

PASS, FAIL, SKIP = "pass", "fail", "skip"
ICONS = {PASS: "✅", FAIL: "❌", SKIP: "—"}

FAMILY_LABELS = {"htf": "higher-timeframe trend", "structure": "structure events", "liquidity": "sweeps",
                 "location": "zone location", "momentum": "M5 momentum", "candle": "candles",
                 "intermarket": "silver (XAG)"}


@dataclass(slots=True)
class Gate:
    """One router check, as it was actually evaluated."""
    name: str
    status: str
    value: Any = None
    threshold: Any = None
    note: str = ""
    flip: str = ""

    @property
    def passed(self) -> bool:                      # kept for readers of the older trace shape
        return self.status != FAIL

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "icon": ICONS[self.status], "passed": self.passed,
                "value": self.value, "threshold": self.threshold, "note": self.note, "flip": self.flip}

    def row(self) -> str:
        threshold = f" vs {self.threshold}" if self.threshold not in (None, "") else ""
        return f"{ICONS[self.status]} {self.name} · {self.value}{threshold}"


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _clip(sentence: str, words: int = 30) -> str:
    parts = sentence.split()
    return sentence if len(parts) <= words else " ".join(parts[:words]).rstrip(",;·") + "…"


def evidence_lines(direction_detail: dict[str, Any] | None, intermarket: dict[str, Any] | None,
                   side: str | None = None) -> dict[str, Any]:
    """What argues FOR the bias (families, location extras, scouts) and what argues AGAINST it
    (penalties the score already paid, plus the opposing side's own evidence)."""
    detail = direction_detail or {}
    bias = (side or "").upper()
    mine, theirs = ("long", "short") if bias == "LONG" else ("short", "long") if bias == "SHORT" else ("long", "short")

    def families(key: str) -> list[dict[str, Any]]:
        values = detail.get(f"{key}_families") or {}
        ranked = sorted(((k, int(v)) for k, v in values.items() if int(v) > 0), key=lambda kv: -kv[1])
        return [{"family": k, "label": FAMILY_LABELS.get(k, k), "points": v} for k, v in ranked]

    def extras(key: str) -> list[dict[str, Any]]:
        return [{"family": item.get("name"), "label": item.get("name"), "points": int(item.get("points", 0))}
                for item in (detail.get(f"{key}_extras") or [])]

    for_side = families(mine) + extras(mine)
    against = [{"family": item.get("name"), "label": item.get("name"), "points": int(item.get("points", 0))}
               for item in ((detail.get("penalties") or {}).get(mine) or [])]
    against += [{**item, "label": f"opposing {item['label']}"} for item in families(theirs)[:3]]
    scout = detail.get("scout_adjustment") or {}
    if scout.get("points"):
        entry = {"family": "scouts", "label": f"scouts {scout.get('verdict', '')}".strip(), "points": int(scout["points"])}
        (for_side if int(scout["points"]) > 0 else against).append(entry)

    im = intermarket or {}
    if im.get("status") == "OK":
        silver = (f"XAG {im.get('regime', 'n/a')} r={im.get('correlation')} · SMT {im.get('smt', 'NONE')} · "
                  f"+{im.get('long_points', 0)}L/+{im.get('short_points', 0)}S")
    else:
        silver = f"silver evidence unavailable ({im.get('reason', 'disabled')})"
    return {"for": for_side[:6], "against": against[:6], "long": families("long")[:3], "short": families("short")[:3],
            "for_score": detail.get(f"{mine}_score"), "against_score": detail.get(f"{theirs}_score"),
            "long_score": detail.get("long_score"), "short_score": detail.get("short_score"), "silver": silver}


def _gate_rows(value: DecisionInput, ctx: dict[str, Any]) -> list[Gate]:
    """Every router check with its measured value — evaluated independently, ordered as the router runs them."""
    side = value.pa_side.value if value.pa_side else None
    scout = value.scout
    zone_text = str(ctx.get("zone_text") or "no zone")
    distance = ctx.get("zone_distance_atr")
    inside = value.entry_state in {EntryState.INSIDE, EntryState.CONFIRMED}
    gap, min_gap = ctx.get("side_gap"), ctx.get("min_side_gap", 8)
    remaining = float(ctx.get("remaining_minutes") or 0.0)
    rr_text = "n/a" if value.rr is None else _num(value.rr)
    locks = [text for text in (ctx.get("day_lock_reason") if ctx.get("day_locked") else None,
                               ctx.get("cooldown_reason") if ctx.get("setup_cooldown") else None,
                               "PA position already open" if ctx.get("position_open") else None) if text]
    account_bits = [f"skew {_num(ctx.get('residual_skew_seconds'), 0)}s"]
    if ctx.get("is_demo") is not None:
        account_bits.append("demo" if ctx.get("is_demo") else "LIVE")
    if ctx.get("is_hedging") is not None:
        account_bits.append("hedging" if ctx.get("is_hedging") else "netting")
    return [
        Gate("data fresh", PASS if value.freshness != Freshness.STALE else FAIL,
             f"M1 {_num(ctx.get('m1_age_seconds'), 0)}s ({value.freshness.value})", f"< {ctx.get('max_m1_age_seconds', 300)}s",
             flip="a fresh M1 bar from the terminal"),
        Gate("clock + account", PASS if (ctx.get("clock_ok", True) and value.account_safe) else FAIL,
             " · ".join(account_bits), f"skew ≤ {ctx.get('max_clock_skew_seconds', 600)}s, demo",
             note=str(ctx.get("account_reason") or ""),
             flip=str(ctx.get("account_reason") or "the account/clock gate to clear")),
        Gate("spread", PASS if value.spread_state != SpreadState.ABNORMAL else FAIL,
             _num(ctx.get("spread")), f"≤ {_num(ctx.get('max_spread_price'))}",
             note=value.spread_state.value, flip=f"spread at or below {_num(ctx.get('max_spread_price'))}"),
        Gate("confluence", PASS if (side and value.confluence >= value.min_confluence) else FAIL,
             f"{value.confluence}/100 {side or 'no side'}", f"≥ {value.min_confluence}",
             flip=f"{max(0, value.min_confluence - value.confluence)} more confluence points"),
        Gate("side gap", PASS if (gap is None or gap >= min_gap) and side else FAIL,
             f"{gap if gap is not None else 'n/a'} ({ctx.get('long_score', '?')}L vs {ctx.get('short_score', '?')}S)",
             f"≥ {min_gap}", flip=f"{max(0, min_gap - (gap or 0))} more points of LONG/SHORT separation"),
        Gate("inside zone", PASS if inside else FAIL,
             f"{value.entry_state.value}" + (f" · {_num(distance, 1)} ATR away" if distance is not None and not inside else ""),
             f"inside {zone_text}" if zone_text != "no zone" else "a zone on the bias side",
             flip=f"price back inside {zone_text}" if zone_text != "no zone" else "a zone to form on the bias side"),
        Gate("trigger", PASS if (value.trigger.confirmed and value.trigger.fresh) else FAIL,
             f"{value.trigger.source} {ctx.get('trigger_time', '') or 'waiting'}".strip(), "fresh M1/M5 confirmation",
             note=str(value.trigger.reason or ""), flip="an M1 sweep+BOS or an M5 close confirmation on this zone visit"),
        Gate("pace", PASS if not (value.hold_when_slow and scout.market_speed == "SLOW") else FAIL,
             f"{scout.market_speed} {_num(scout.velocity, 3)}/min", f"≥ {ctx.get('slow_velocity', 0.03)}/min",
             flip=f"pace above {ctx.get('slow_velocity', 0.03)} price/min"),
        Gate("scouts", PASS if not (scout.verdict == ScoutVerdict.CONTRADICTS
                                    and scout.strength >= value.scout_contradiction_threshold) else FAIL,
             f"{scout.leader or 'NONE'} {scout.verdict.value} {scout.strength}/10",
             f"< {value.scout_contradiction_threshold} when contradicting",
             flip="scout strength to fall or the leader to flip"),
        Gate("RR", PASS if (value.rr is not None and value.rr >= value.min_rr) else FAIL,
             rr_text, f"≥ {_num(value.min_rr)}", flip="a nearer TP1 or a tighter structural SL"),
        Gate("target realism", PASS if value.target_realism not in {None, TargetRealism.UNLIKELY} else FAIL,
             str(value.target_realism.value) if value.target_realism else "n/a", "not UNLIKELY",
             flip="a closer structural target or more daily range left"),
        Gate("session feasibility", FAIL if ctx.get("session_target_verdict") == "UNLIKELY" else PASS,
             str(ctx.get("session_target_verdict") or "n/a"), "not UNLIKELY",
             note=f"${_num(ctx.get('session_target_move'), 0)} move", flip="more session time, faster pace or a smaller required move"),
        Gate("risk locks", FAIL if locks else PASS, ", ".join(locks) or "clear", "no lock",
             flip="the lock/cooldown to clear"),
        Gate("session time", FAIL if remaining <= 0 else PASS, f"{remaining:.0f} min left",
             "> 0 min", note=str(ctx.get("session") or ""), flip="the next session to open"),
    ]


def explain_decision(value: DecisionInput, decision: Decision, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """The whole decision on one screen: headline, verdict sentence, gate checklist, what flips it,
    evidence for and against, the levels involved, and what GO means today."""
    ctx = context or {}
    side = value.pa_side.value if value.pa_side else None
    action = decision.action.value
    rows = _gate_rows(value, ctx)

    failures = [gate for gate in rows if gate.status == FAIL]
    first_blocking = failures[0] if failures else None
    if first_blocking is not None:                       # the router stopped here; nothing after it was evaluated
        for gate in rows[rows.index(first_blocking) + 1:]:
            gate.status = SKIP
            gate.value = "not reached"
    remaining_vetoes = [gate.name for gate in failures[1:]]

    placed = ctx.get("order") or {}
    session = str(ctx.get("session") or "")
    clock = str(ctx.get("time_text") or "")
    bias = f"{side} bias {value.confluence}/100" if side else f"no directional bias ({value.confluence}/100 best side)"
    if placed.get("ticket"):
        state, emoji = "ORDER_PLACED", "🟢"
        headline = f"{emoji} {action} PLACED · #{placed.get('ticket')} · {placed.get('volume')} lot"
    elif session in {"", "CLOSED"} or (ctx.get("remaining_minutes") is not None and float(ctx["remaining_minutes"]) <= 0):
        state, emoji = "CLOSED", "⚪"
        headline = f"{emoji} CLOSED · no session · {bias}"
    elif action == "WAIT":
        state, emoji = "WAIT", "🟠"
        blockers = ", ".join(gate.name for gate in failures[:2]) or "final checks"
        headline = f"{emoji} WAIT · {bias} · {blockers}"
    elif action in {"LONG", "SHORT"}:
        state, emoji = "READY", "🟢"
        headline = f"{emoji} {action} · {bias} · every gate passed"
    else:
        state, emoji = "NO_TRADE", "🔴"
        headline = f"{emoji} NO TRADE · {bias}"
    if session:
        headline += f" · {session}{' ' + clock if clock else ''}"

    zone_text = str(ctx.get("zone_text") or "no zone")
    distance = ctx.get("zone_distance_atr")
    where = ("no zone selected yet" if zone_text == "no zone"
             else f"price inside {zone_text}" if value.entry_state in {EntryState.INSIDE, EntryState.CONFIRMED}
             else f"price {_num(distance, 1)} ATR from {zone_text}" if distance is not None
             else f"price {value.entry_state.value.lower()} of {zone_text}")
    if placed.get("ticket"):
        verdict = (f"{action} placed at {_num(placed.get('entry'))}, SL {_num(placed.get('stop_loss'))} "
                   f"({_num(placed.get('risk_price'))} risk), first target {_num((placed.get('take_profits') or [None])[0])}.")
    elif first_blocking is None:
        verdict = f"{action} authorised: {bias}, {where}, RR {_num(value.rr)} — all {len(rows)} gates passed."
    else:
        second = f" then {failures[1].name} ({failures[1].value})" if len(failures) > 1 else ""
        verdict = (f"{bias}: blocked by {first_blocking.name} ({first_blocking.value} vs "
                   f"{first_blocking.threshold}){second}; {where}.")

    flips: list[str] = []
    for gate in failures[:3]:
        flips.append(f"{len(flips) + 1}. {gate.name}: {gate.flip}" if gate.flip else f"{len(flips) + 1}. {gate.name}")
    if not flips and not placed.get("ticket"):
        flips = ["1. nothing — the router authorised this setup"]

    plan = ctx.get("plan") or {}
    levels = {"price": ctx.get("price"), "zone": zone_text, "zone_kind": ctx.get("zone_kind"),
              "stop_loss": plan.get("stop_loss"), "take_profit_1": (plan.get("take_profits") or [None])[0],
              "rr_1": (plan.get("actual_rr") or [None])[0], "atr": ctx.get("atr"),
              "distance_atr": distance, "spread": ctx.get("spread")}
    go_meaning = ("GO means this demo cycle passed every router gate — it counts towards the session GO tally "
                  "and is a signal, never a standing instruction to place an order.")
    return {
        "headline": headline, "state": state, "emoji": emoji, "action": action, "reason": decision.reason,
        "verdict": _clip(verdict), "bias": bias, "side": side, "confluence": value.confluence,
        "session": session, "time_text": clock,
        "gates": [gate.as_dict() for gate in rows],
        "checklist": [gate.row() for gate in rows],
        "first_blocking": first_blocking.name if first_blocking else None,
        "blocked_by": [gate.as_dict() for gate in failures],
        "remaining_vetoes": remaining_vetoes,
        "passed_count": sum(1 for gate in rows if gate.status == PASS), "gate_count": len(rows),
        "flips": flips, "next": [item.split(". ", 1)[-1] for item in flips],
        "evidence": evidence_lines(ctx.get("direction_detail"), ctx.get("intermarket"), side),
        "levels": levels, "order": placed,
        "go_meaning": go_meaning,
        "footer": {"go_meaning": go_meaning,
                   "remaining_vetoes": remaining_vetoes,
                   "hint": "!why for the full gate table · !detected full for everything the engine sees"},
    }
