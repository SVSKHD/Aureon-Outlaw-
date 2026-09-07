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


# --- v3.3.0: the same veto ladder, enumerated so a NO-GO explains itself -------------------------------------------
# Every NO_TRADE/WAIT branch of final_decision_router(), in the order it evaluates them, followed by the
# engine-level overrides that run after the router and can also turn a GO into a NO-GO.
VETO_ORDER = ("trigger_consumed", "trigger_ownership", "data_stale", "account_safety", "spread", "setup",
              "confluence", "zone", "trigger", "slow", "scouts", "rr", "target", "higher_tf_conflict",
              "session_target", "session_feasibility", "cold_start", "send_time", "clock", "day_lock")


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
        ("trigger_consumed", bool(value.trigger.consumed),
         "the current trigger was already consumed by an order",
         "a new zone visit produces a fresh trigger"),
        ("trigger_ownership",
         bool(value.setup_id is not None and (value.trigger.setup_id != value.setup_id or value.trigger.direction != value.pa_side)),
         f"trigger belongs to setup {value.trigger.setup_id} / {getattr(value.trigger.direction, 'value', value.trigger.direction)}, "
         f"not {value.setup_id} / {getattr(value.pa_side, 'value', value.pa_side)}",
         "a trigger prints for the selected setup in the selected direction"),
        ("data_stale", value.freshness == Freshness.STALE,
         f"M1 data is {value.freshness.value}",
         "a fresh M1 bar arrives (check the feed and the broker clock)"),
        ("account_safety", not value.account_safe,
         str(gate.get("account_reason", "account or execution safety check failed")),
         "the account safety gate passes: margin, total volume, trade permission, demo mode"),
        ("spread", value.spread_state == SpreadState.ABNORMAL,
         f"spread {gate.get('spread', 'n/a')} vs limit {gate.get('max_spread', 'n/a')}",
         "spread falls back to or below the configured maximum"),
        ("setup", not (value.setup_valid and value.pa_side is not None),
         f"setup_valid={value.setup_valid} pa_side={getattr(value.pa_side, 'value', value.pa_side)}",
         "structure, zones and patterns agree on a directional setup"),
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
        ("higher_tf_conflict", bool(gate.get("higher_tf_conflict")),
         str(gate.get("higher_tf_conflict_detail", "higher-timeframe conflict with a STRETCHED session target")),
         "the higher timeframes align, or the session target stops being STRETCHED"),
        ("session_target", bool(gate.get("session_target_unlikely")),
         str(gate.get("session_target_detail", "the $ session move is UNLIKELY")),
         "the remaining session range makes the configured move reachable again"),
        ("session_feasibility",
         bool(minimum_remaining is not None and remaining is not None and remaining < minimum_remaining),
         f"{remaining if remaining is None else round(float(remaining))} min left vs {minimum_remaining} min minimum",
         "the next session opens (this one no longer has enough time)"),
        ("cold_start", bool(gate.get("cold_start")),
         "cold start: inputs captured before the synchronous history/pattern load",
         "the next cycle re-evaluates on warm, cached history"),
        ("send_time", bool(gate.get("send_time_withheld")),
         str(gate.get("send_time_detail", "order withheld at send time")),
         "the send-time re-check of the order gate and the daily risk limits passes"),
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


# --- v3.4.0: the decision trace — one structure that answers "go or not" in a glance -----------------------------
GATE_ORDER = ("data", "clock_account", "spread", "confluence", "side_gap", "zone", "trigger", "pace",
              "scouts", "rr", "target", "session_target", "risk_locks", "session_time")

GATE_LABELS = {
    "data": "Data fresh", "clock_account": "Clock + account", "spread": "Spread", "confluence": "Confluence",
    "side_gap": "Side gap", "zone": "Price inside zone", "trigger": "Trigger", "pace": "Pace",
    "scouts": "Scouts", "rr": "Risk-reward", "target": "Target realism", "session_target": "$ session target",
    "risk_locks": "Risk locks", "session_time": "Session time",
}

FAMILY_LABELS = {
    "htf": "higher timeframes", "structure": "structure", "liquidity": "liquidity", "location": "location",
    "momentum": "momentum", "candle": "candles", "intermarket": "silver",
}

# The checklist row carries the full measurement; the verdict sentence needs the short version.
GATE_SHORT = {
    "data": "M1 data is stale", "clock_account": "clock or account gate", "spread": "spread too wide",
    "confluence": "confluence short of the threshold", "side_gap": "neither side leads clearly",
    "zone": "price is not in the zone", "trigger": "no fresh trigger", "pace": "session pace is SLOW",
    "scouts": "scouts contradict", "rr": "risk-reward too low", "target": "target unrealistic",
    "session_target": "session move unlikely", "risk_locks": "a risk lock is active",
    "session_time": "not enough session left",
}

# What the title says the setup is doing while it waits.
GATE_PENDING = {
    "zone": "price outside zone", "trigger": "trigger pending", "pace": "pace SLOW",
    "scouts": "scouts contradict", "rr": "RR short", "target": "target unrealistic",
    "session_target": "session move unlikely", "confluence": "confluence short",
    "side_gap": "no clear side", "spread": "spread wide", "data": "data stale",
    "clock_account": "clock/account", "risk_locks": "risk lock", "session_time": "session ending",
}

MAX_VERDICT_WORDS = 30


@dataclass(slots=True)
class Gate:
    """One row of the checklist: did this gate pass, on what measured number, against what threshold."""

    key: str
    label: str
    state: str                     # "pass" | "fail" | "skipped"
    value: str                     # measured value vs threshold, already formatted
    blocking: bool = False         # the FIRST failing gate — the one actually stopping the trade

    @property
    def mark(self) -> str:
        return {"pass": "✅", "fail": "❌", "skipped": "—"}[self.state]

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.key, "label": self.label, "state": self.state, "value": self.value,
                "blocking": self.blocking, "mark": self.mark}


@dataclass(slots=True)
class DecisionExplanation:
    """Everything the status card renders, decided once and stored on the snapshot (v3.4.0)."""

    verdict: str                   # PLACED | WAIT | NO_TRADE | CLOSED
    colour: str                    # green | amber | red | grey
    emoji: str
    title: str
    sentence: str
    gates: list[Gate]
    flips: list[str]
    evidence_for: list[str]
    evidence_against: list[str]
    levels: dict[str, Any]
    footer: dict[str, Any]

    @property
    def blocking_gate(self) -> Gate | None:
        return next((g for g in self.gates if g.blocking), None)

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "colour": self.colour, "emoji": self.emoji, "title": self.title,
                "sentence": self.sentence, "gates": [g.to_dict() for g in self.gates], "flips": list(self.flips),
                "evidence_for": list(self.evidence_for), "evidence_against": list(self.evidence_against),
                "levels": dict(self.levels), "footer": dict(self.footer),
                "blocking_gate": self.blocking_gate.key if self.blocking_gate else None}


def _n(value: Any, digits: int = 2, default: str = "n/a") -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return default


def _clip_words(text: str, limit: int = MAX_VERDICT_WORDS) -> str:
    """Keep the verdict sentence readable at a glance: drop whole trailing sentences, never mid-number."""
    parts = [p.strip().rstrip(".") for p in text.split(". ") if p.strip().rstrip(".")]
    while len(". ".join(parts).split()) > limit and len(parts) > 1:
        parts.pop()
    joined = ". ".join(parts)
    words = joined.split()
    if len(words) > limit:
        joined = " ".join(words[:limit])
    joined = joined.rstrip(" .,")
    return joined + "." if joined else joined


def _gate_rows(value: DecisionInput, ctx: dict[str, Any]) -> list[Gate]:
    """Every gate in checklist order with its measured number. The first failure is the blocking one;
    everything after it is marked skipped, because the router never got that far."""
    zone = ctx.get("zone") or {}
    scout = value.scout
    inside = value.entry_state in {EntryState.INSIDE, EntryState.CONFIRMED}
    zone_value = f"{value.entry_state.value}"
    if not inside and ctx.get("zone_distance_atr") is not None:
        zone_value += f" · {_n(ctx['zone_distance_atr'])} ATR from {_n(zone.get('low'))}–{_n(zone.get('high'))}"
    elif inside and zone:
        zone_value += f" · {_n(zone.get('low'))}–{_n(zone.get('high'))} {zone.get('kind', '')}".rstrip()

    checks: list[tuple[str, bool, str]] = [
        ("data", value.freshness != Freshness.STALE,
         f"M1 age {_n(ctx.get('m1_age_seconds'), 0)}s / {ctx.get('max_m1_age_seconds', 'n/a')}s · {value.freshness.value}"),
        ("clock_account", value.account_safe and bool(ctx.get("clock_ok", True)),
         f"skew {_n(ctx.get('clock_residual_seconds'), 0)}s / {ctx.get('max_clock_skew_seconds', 'n/a')}s · "
         f"demo={ctx.get('is_demo', True)} hedging={ctx.get('is_hedging', True)}"
         + ("" if value.account_safe else f" · {ctx.get('account_reason', 'account unsafe')}")),
        ("spread", value.spread_state != SpreadState.ABNORMAL,
         f"{_n(ctx.get('spread'))} / {_n(ctx.get('max_spread'))} · {value.spread_state.value}"),
        ("confluence", value.confluence >= value.min_confluence,
         f"{value.confluence} / {value.min_confluence}"),
        ("side_gap", value.setup_valid and value.pa_side is not None,
         f"{ctx.get('side_gap', 'n/a')} / {ctx.get('min_side_gap', 8)} · "
         f"LONG {ctx.get('long_score', 'n/a')} vs SHORT {ctx.get('short_score', 'n/a')}"),
        ("zone", inside, zone_value),
        ("trigger", bool(value.trigger.confirmed and value.trigger.fresh and not value.trigger.consumed),
         f"{value.trigger.source or 'none'} · confirmed={value.trigger.confirmed} fresh={value.trigger.fresh}"
         + (f" · {ctx.get('trigger_time')}" if ctx.get("trigger_time") else "")),
        ("pace", not (value.hold_when_slow and scout.market_speed == "SLOW"),
         f"{scout.market_speed} · {_n(ctx.get('pace_price_per_min'), 3)} price/min"),
        ("scouts", not (scout.verdict == ScoutVerdict.CONTRADICTS and scout.strength >= value.scout_contradiction_threshold),
         f"leader {scout.leader or 'none'} · {scout.verdict.value} {scout.strength}/10 "
         f"(veto at {value.scout_contradiction_threshold})"),
        ("rr", not (value.rr is None or value.rr < value.min_rr),
         f"{_n(value.rr)} / {_n(value.min_rr)}"),
        ("target", value.target_realism not in {None, TargetRealism.UNLIKELY},
         f"{getattr(value.target_realism, 'value', 'n/a')}"),
        ("session_target", not ctx.get("session_target_unlikely"),
         f"{ctx.get('session_target_verdict', 'n/a')} · ${_n(ctx.get('session_target_move'), 0)} move"),
        ("risk_locks", not (ctx.get("day_lock") or ctx.get("cooldown") or ctx.get("one_position_block")),
         ctx.get("risk_locks_detail") or "day lock clear · no cooldown · no open PA position"),
        ("session_time", not ctx.get("session_time_short"),
         f"{_n(ctx.get('remaining_minutes'), 0)} min left / {ctx.get('minimum_remaining_minutes', 'n/a')} min minimum"),
    ]

    rows: list[Gate] = []
    blocked = False
    for key, ok, detail in checks:
        if blocked:
            rows.append(Gate(key, GATE_LABELS[key], "skipped", detail))
            continue
        if ok:
            rows.append(Gate(key, GATE_LABELS[key], "pass", detail))
        else:
            rows.append(Gate(key, GATE_LABELS[key], "fail", detail, blocking=True))
            blocked = True
    return rows


def _flips_for(gate: Gate | None, value: DecisionInput, ctx: dict[str, Any]) -> list[str]:
    """Up to three concrete, numbered conditions — each with the number that has to change."""
    if gate is None:
        return []
    zone = ctx.get("zone") or {}
    side = getattr(value.pa_side, "value", None)
    gap = ctx.get("confluence_gap")
    best_family = ctx.get("best_missing_family")
    flips: dict[str, list[str]] = {
        "data": [f"A fresh M1 bar lands (age back under {ctx.get('max_m1_age_seconds', 'n/a')}s)",
                 "Market Watch shows XAUUSD ticking again"],
        "clock_account": [f"Residual skew falls under {ctx.get('max_clock_skew_seconds', 'n/a')}s — run `w32tm /resync` on the PC",
                          f"Free margin covers the trade ({_n(ctx.get('margin_free'))} available)"],
        "spread": [f"Spread comes back to {_n(ctx.get('max_spread'))} or tighter (now {_n(ctx.get('spread'))})"],
        "confluence": ([f"Confluence gains {gap} more point{'s' if gap != 1 else ''} to reach {value.min_confluence}"] if gap else [])
                      + ([f"{FAMILY_LABELS.get(best_family, best_family)} can still supply up to "
                          f"{ctx.get('best_missing_headroom')} of them"] if best_family else []),
        "side_gap": [f"One side leads the other by {ctx.get('min_side_gap', 8)} "
                     f"(now LONG {ctx.get('long_score', 'n/a')} vs SHORT {ctx.get('short_score', 'n/a')})"],
        "zone": ([f"Price returns to {_n(zone.get('low'))}–{_n(zone.get('high'))} ({zone.get('kind', 'zone')})"] if zone else [])
                + ([f"That is {_n(ctx.get('zone_distance_atr'))} ATR away from {_n(ctx.get('price'))}"]
                   if ctx.get("zone_distance_atr") is not None else []),
        "trigger": [f"An M1 or M5 confirmation prints for this zone visit"
                    + (f" in the {side} direction" if side else "")],
        "pace": [f"Session pace rises out of SLOW (now {_n(ctx.get('pace_price_per_min'), 3)} price/min, "
                 f"needs {_n(ctx.get('slow_threshold'), 3)})"],
        "scouts": [f"Scout contradiction drops below {value.scout_contradiction_threshold} "
                   f"(now {value.scout.strength}) or the leader flips off {value.scout.leader}"],
        "rr": [f"Actual RR reaches {_n(value.min_rr)} — a tighter stop or a further structural TP (now {_n(value.rr)})"],
        "target": ["The nearest structural target comes inside the remaining daily range"],
        "session_target": [f"The remaining session range makes the ${_n(ctx.get('session_target_move'), 0)} move reachable"],
        "risk_locks": ["The trading date rolls over at 17:00 New York, clearing the day lock",
                       "The open PA position closes, or its setup cooldown expires"],
        "session_time": [f"The next session opens — this one has {_n(ctx.get('remaining_minutes'), 0)} min left"],
    }
    return [f for f in flips.get(gate.key, []) if f][:3]


def _evidence(ctx: dict[str, Any], side: str | None) -> tuple[list[str], list[str]]:
    """FOR = the families backing the chosen side, biggest first. AGAINST = penalties and the opposing families."""
    families = ctx.get("families") or {}
    ours = families.get("long" if side == "LONG" else "short") or {}
    theirs = families.get("short" if side == "LONG" else "long") or {}
    if side is None:                                   # no side chosen: show whichever is ahead as FOR
        ours, theirs = families.get("long") or {}, families.get("short") or {}
    for_lines = [f"{FAMILY_LABELS.get(k, k)} +{v}" for k, v in
                 sorted(ours.items(), key=lambda kv: -kv[1]) if v > 0]
    for bonus in (ctx.get("bonuses") or []):
        for_lines.append(str(bonus))
    against_lines = [str(p) for p in (ctx.get("penalties") or [])]
    against_lines += [f"opposing {FAMILY_LABELS.get(k, k)} +{v}" for k, v in
                      sorted(theirs.items(), key=lambda kv: -kv[1]) if v > 0]
    return for_lines[:8], against_lines[:8]


def explain_decision(value: DecisionInput, decision: Decision, ctx: dict[str, Any] | None = None) -> DecisionExplanation:
    """Build the one structure the status card renders (v3.4.0).

    `ctx` carries everything the router itself does not see — measured spreads and ages, the zone and its
    distance, the confluence family breakdown, the placed ticket, the session tally. Nothing here re-decides
    anything: `decision` has already been made by `final_decision_router` plus the engine's later overrides."""
    ctx = dict(ctx or {})
    side = getattr(value.pa_side, "value", None)
    action = decision.action.value
    placed = bool(ctx.get("order_ticket"))
    session = str(ctx.get("session", ""))
    closed = session in {"CLOSED", ""} or bool(ctx.get("market_closed"))

    gates = _gate_rows(value, ctx)
    blocking = next((g for g in gates if g.blocking), None)

    if placed:
        verdict, colour, emoji = "PLACED", "green", "🟢"
    elif closed:
        verdict, colour, emoji = "CLOSED", "grey", "⚪"
    elif action in {"LONG", "SHORT"}:
        # The router authorised but nothing was sent — PA orders off, or the send failed. Still a GO, not a refusal.
        verdict, colour, emoji = "GO", "green", "🟢"
    elif action == "WAIT":
        verdict, colour, emoji = "WAIT", "amber", "🟠"
    else:
        verdict, colour, emoji = "NO_TRADE", "red", "🔴"

    when = str(ctx.get("time_label") or "")
    tail = " · ".join(x for x in (session or None, when or None) if x)
    bias = f"{side} bias {value.confluence}/100" if side else f"no side {value.confluence}/100"
    if placed:
        title = f"{emoji} {side or action} PLACED · #{ctx.get('order_ticket')} · {ctx.get('order_volume', '')} lot"
    elif verdict == "CLOSED":
        title = f"{emoji} CLOSED · no session"
    elif verdict == "GO":
        title = f"{emoji} {side} GO · {bias} · every gate clear, no order sent"
    elif verdict == "WAIT":
        reached = [g for g in gates if g.state == "pass"]
        got_zone = any(g.key == "zone" for g in reached)
        pending = GATE_PENDING.get(blocking.key, blocking.label.lower()) if blocking else "all gates clear"
        state = f"inside zone, {pending}" if got_zone and blocking and blocking.key != "zone" else pending
        title = f"{emoji} WAIT · {bias} · {state}"
    else:
        title = f"{emoji} NO TRADE · {bias}"
    if tail:
        title = f"{title} · {tail}"

    zone = ctx.get("zone") or {}
    if zone and ctx.get("price") is not None:
        inside = value.entry_state in {EntryState.INSIDE, EntryState.CONFIRMED}
        where = (f"Price {_n(ctx.get('price'))} sits inside the {_n(zone.get('low'))}–{_n(zone.get('high'))} "
                 f"{zone.get('kind', 'zone')}." if inside else
                 f"Price {_n(ctx.get('price'))} is {_n(ctx.get('zone_distance_atr'))} ATR from the "
                 f"{_n(zone.get('low'))}–{_n(zone.get('high'))} {zone.get('kind', 'zone')}.")
    else:
        where = "No zone selected yet."
    if placed:
        head = (f"{side} filled at {_n(ctx.get('order_entry'))} risking {_n(ctx.get('risk_currency'))} "
                f"with stop {_n(ctx.get('stop_loss'))}.")
    elif verdict == "CLOSED":
        head = "No session is running, so the router is not evaluating a trade."
    elif verdict == "GO":
        head = f"{bias}. Every gate is clear; no order was sent ({ctx.get('no_order_reason', 'PA orders disabled')})."
    elif blocking is not None:
        head = f"{bias}, blocked by {GATE_SHORT.get(blocking.key, blocking.label.lower())} ({blocking.value})."
    else:
        head = f"{bias}. Every gate is clear."
    sentence = _clip_words(f"{head} {where}")

    for_lines, against_lines = _evidence(ctx, side)
    plan = ctx.get("plan") or {}
    tps = plan.get("take_profits") or []
    rrs = plan.get("actual_rr") or []
    def _r(v: Any, d: int = 2) -> Any:
        try:
            return round(float(v), d)
        except (TypeError, ValueError):
            return None
    levels = {
        "price": _r(ctx.get("price")), "bid": _r(ctx.get("bid")), "ask": _r(ctx.get("ask")),
        "zone_low": _r(zone.get("low")), "zone_high": _r(zone.get("high")), "zone_kind": zone.get("kind"),
        "stop_loss": _r(plan.get("stop_loss")), "take_profit_1": _r(tps[0]) if tps else None,
        "take_profit_1_rr": _r(rrs[0]) if rrs else None, "atr": _r(ctx.get("atr"), 3),
    }
    after_zone = GATE_ORDER[GATE_ORDER.index("zone") + 1:]
    remaining = [g.label for g in gates if g.key in after_zone and g.state != "pass"]
    footer = {
        "session": session, "go_today": ctx.get("go_today"), "go_cycles": ctx.get("go_cycles"),
        "cycles_observed": ctx.get("cycles_observed"),
        "remaining_vetoes": remaining,
        "hint": "`!why` for what would flip each gate · `!clock` for the broker offset",
        "calibration": ctx.get("calibration_status"),
    }
    return DecisionExplanation(verdict, colour, emoji, title, sentence, gates,
                               _flips_for(blocking, value, ctx), for_lines, against_lines, levels, footer)
