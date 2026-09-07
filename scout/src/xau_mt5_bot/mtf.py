"""Multi-timeframe treatment (v3.0.0 §11).

Classifies how D1/H4/H1/M15/M5 structure lines up with the proposed PA direction and names the way the current
setup is being treated. Pure functions of already-computed structure states — no market access, no look-ahead.
"""
from __future__ import annotations

from typing import Any

from .models import Side, StructureState

TIMEFRAMES = ("D1", "H4", "H1", "M15", "M5")
ROLE = {"D1": "broad bias", "H4": "major structure", "H1": "directional context", "M15": "setup context", "M5": "execution structure"}


def _lean(state: StructureState | None) -> int:
    if state == StructureState.BULLISH: return 1
    if state == StructureState.BEARISH: return -1
    return 0


def alignment(structures: dict[str, Any], side: Side | None) -> dict[str, Any]:
    """Returns the per-timeframe view plus one label:
    FULL_ALIGNMENT, PARTIAL_ALIGNMENT, LOWER_TF_COUNTERTREND, HIGHER_TF_CONFLICT, RANGE_CONDITION."""
    view = {}
    for tf in TIMEFRAMES:
        result = structures.get(tf)
        state = getattr(result, "state", None)
        view[tf] = {"role": ROLE[tf], "state": state.value if state else "Unknown", "lean": _lean(state)}
    want = 1 if side == Side.LONG else -1 if side == Side.SHORT else 0
    leans = {tf: view[tf]["lean"] for tf in TIMEFRAMES}
    ranging = sum(1 for tf in TIMEFRAMES if structures.get(tf) is not None and getattr(structures[tf], "state", None) in (StructureState.RANGE, StructureState.NEUTRAL))
    if want == 0 or ranging >= 4:
        label = "RANGE_CONDITION"
    else:
        higher = [leans["D1"], leans["H4"], leans["H1"]]; lower = [leans["M15"], leans["M5"]]
        agree = sum(1 for l in leans.values() if l == want); against = sum(1 for l in leans.values() if l == -want)
        if agree == 5: label = "FULL_ALIGNMENT"
        elif against == 0: label = "PARTIAL_ALIGNMENT"
        elif all(l == want or l == 0 for l in higher) and any(l == -want for l in lower): label = "LOWER_TF_COUNTERTREND"
        elif any(l == -want for l in higher): label = "HIGHER_TF_CONFLICT"
        else: label = "PARTIAL_ALIGNMENT"
    score = sum(w * (1 if view[tf]["lean"] == want else -1 if view[tf]["lean"] == -want else 0)
                for tf, w in (("D1", 3), ("H4", 3), ("H1", 2), ("M15", 1), ("M5", 1))) if want else 0
    return {"label": label, "score": score, "max_score": 10, "timeframes": view,
            "explanation": _explain(label, view, side)}


def _explain(label: str, view: dict[str, Any], side: Side | None) -> str:
    if side is None: return "No PA direction; timeframes not compared."
    parts = [f"{tf} {view[tf]['state']}" for tf in TIMEFRAMES]
    return f"{label}: " + ", ".join(parts) + f" for {side.value}."


def treatment(side: Side | None, alignment_label: str, sweeps: list[Any], zones: list[Any], trigger: Any,
              structures: dict[str, Any], patterns: list[dict[str, Any]]) -> dict[str, Any]:
    """Names how the setup is being treated. Deterministic and explained.
    One of: TREND_CONTINUATION, PULLBACK_CONTINUATION, COUNTERTREND_REVERSAL, LIQUIDITY_SWEEP_REVERSAL, BREAKOUT,
    FAILED_BREAKOUT, RANGE_REVERSION, NO_VALID_STRUCTURE."""
    if side is None:
        return {"treatment": "NO_VALID_STRUCTURE", "reason": "Price action produced no direction."}
    active_sweep = next((s for s in sweeps if getattr(s, "active", False)), None)
    names = [str(p.get("name", "")) for p in patterns]
    failed_break = any(n.startswith("Failed") for n in names)
    m5 = structures.get("M5"); h1 = structures.get("H1")
    m5_lean = _lean(getattr(m5, "state", None)); h1_lean = _lean(getattr(h1, "state", None))
    want = 1 if side == Side.LONG else -1
    trigger_src = getattr(trigger, "source", "NONE"); confirmed = bool(getattr(trigger, "confirmed", False))
    zone_kind = getattr(zones[0], "kind", "") if zones else ""
    if alignment_label == "RANGE_CONDITION":
        return {"treatment": "RANGE_REVERSION", "reason": "Most timeframes are ranging; setup is a reversion from the range edge."}
    if failed_break and m5_lean == want:
        return {"treatment": "FAILED_BREAKOUT", "reason": "Recent failed structure break on the opposite side; trading the failure back into structure."}
    if active_sweep is not None and getattr(active_sweep, "direction", "") != ("LONG" if want == 1 else "SHORT"):
        return {"treatment": "LIQUIDITY_SWEEP_REVERSAL", "reason": f"Active {active_sweep.level_type} sweep reclaimed; trading the reclaim against the sweep direction."}
    if alignment_label == "HIGHER_TF_CONFLICT":
        return {"treatment": "COUNTERTREND_REVERSAL", "reason": "Higher timeframes lean against the setup; treated as a countertrend reversal with reduced feasibility."}
    if trigger_src == "M5" and confirmed and zone_kind == "" and m5_lean == want and h1_lean == want:
        return {"treatment": "BREAKOUT", "reason": "M5 break-and-retest with M5/H1 agreement."}
    if zones and m5_lean == want:
        return {"treatment": "PULLBACK_CONTINUATION", "reason": f"Entry at a {zone_kind or 'structure'} zone in the direction of the M5 trend."}
    if alignment_label in ("FULL_ALIGNMENT", "PARTIAL_ALIGNMENT"):
        return {"treatment": "TREND_CONTINUATION", "reason": "Structure agrees across timeframes; continuation of the prevailing trend."}
    return {"treatment": "COUNTERTREND_REVERSAL", "reason": "Lower timeframes lean against higher ones; reversal attempt."}
