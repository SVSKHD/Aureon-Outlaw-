"""Session target feasibility (v3.0.0 §7): can a $`target_price_move` XAUUSD move still happen this session?

Pure function of the current snapshot inputs plus historical aggregates that were computed only from observations
before the decision timestamp (the caller guarantees that). The verdict is an estimate, never a guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Side


@dataclass(slots=True)
class TargetInputs:
    side: Side | None
    reference_entry: float | None
    bid: float
    ask: float
    spread: float
    atr: float
    remaining_minutes: float
    session_minutes_total: float
    session_high: float | None
    session_low: float | None
    pace_range_per_min: float
    velocity_direction: str
    market_speed: str
    liquidity: list[Any] = field(default_factory=list)         # LiquidityLevel-like: price, kind, strength
    alignment_label: str = "RANGE_CONDITION"
    alignment_score: int = 0
    scout_leader: str = "NONE"
    scout_strength: int = 0
    scout_verdict: str = "NEUTRAL"
    historical_samples: int = 0
    historical_plus_target_rate: float | None = None
    historical_fakeout_rate: float | None = None
    session_range_p50: float | None = None
    session_range_p75: float | None = None
    session_range_p90: float | None = None


def assess_session_target(inputs: TargetInputs, cfg: Any) -> dict[str, Any]:
    move = float(cfg.target_price_move)
    reasons: list[str] = []
    if inputs.side is None or inputs.reference_entry is None:
        return _result(inputs, move, None, None, "NONE", "UNLIKELY", "No PA direction / entry to measure a target from.",
                       inputs.remaining_minutes, None, None, None, None, None, None, None, None, 0.0, "NONE")
    long = inputs.side == Side.LONG
    entry = float(inputs.reference_entry)
    target = round(entry + move, 2) if long else round(entry - move, 2)
    current = inputs.bid if long else inputs.ask                                                   # exit side price
    required = round((target - current) if long else (current - target), 2)
    direction = "UP" if long else "DOWN"
    remaining = max(0.0, float(inputs.remaining_minutes))
    required_velocity = round(required / remaining, 4) if remaining > 0 else None
    current_velocity = round(float(inputs.pace_range_per_min), 4)
    session_range = round((inputs.session_high - inputs.session_low), 2) if inputs.session_high is not None and inputs.session_low is not None else None
    reference_range = inputs.session_range_p75 or (inputs.atr * 6.0)                             # fallback when no history: ~6 ATR(M5)
    consumed = round(session_range / reference_range, 2) if session_range is not None and reference_range > 0 else None
    est_remaining = round(max(0.0, (inputs.session_range_p90 or reference_range * 1.25) - (session_range or 0.0)), 2)
    pace_remaining = round(current_velocity * remaining * 0.6, 2)                                 # pace-based estimate (60% of rolling pace sustained)
    est_remaining = round(max(est_remaining, pace_remaining) if consumed is not None and consumed < 1.0 else est_remaining, 2)
    blocker, blocker_dist = _nearest_blocker(inputs.liquidity, current, target, long)

    # ---- hard blockers -------------------------------------------------------------------------------------
    if required <= 0:
        verdict = "ACHIEVABLE"; reasons.append("Target already reached from the current price.")
        return _result(inputs, move, target, required, direction, verdict, " ".join(reasons), remaining, required_velocity, current_velocity,
                       session_range, consumed, est_remaining, blocker, blocker_dist, 1.0, 1.0, "HARD")
    if remaining < float(cfg.minimum_remaining_minutes):
        return _result(inputs, move, target, required, direction, "UNLIKELY", f"Only {remaining:.0f} min left in session (< {cfg.minimum_remaining_minutes}).",
                       remaining, required_velocity, current_velocity, session_range, consumed, est_remaining, blocker, blocker_dist, 0.05, 0.05, "HARD")
    if est_remaining < required * 0.5:
        return _result(inputs, move, target, required, direction, "UNLIKELY",
                       f"Estimated remaining session range {est_remaining:.2f} is under half the required {required:.2f}.",
                       remaining, required_velocity, current_velocity, session_range, consumed, est_remaining, blocker, blocker_dist, 0.10, 0.10, "HARD")

    # ---- structural probability (0..1) -----------------------------------------------------------------------
    score = 0.0
    range_ratio = est_remaining / required if required > 0 else 9.0
    score += 0.30 if range_ratio >= 1.5 else 0.20 if range_ratio >= 1.0 else 0.08
    reasons.append(f"remaining range {est_remaining:.2f} vs required {required:.2f}")
    if required_velocity is not None and current_velocity > 0:
        pace_ratio = current_velocity / required_velocity
        score += 0.25 if pace_ratio >= 1.5 else 0.15 if pace_ratio >= 1.0 else 0.05
        reasons.append(f"pace {current_velocity:.3f}/min vs required {required_velocity:.3f}/min")
    else:
        reasons.append("pace unknown")
    if inputs.velocity_direction == direction: score += 0.05
    elif inputs.velocity_direction not in ("FLAT", ""): score -= 0.05
    score += {"FULL_ALIGNMENT": 0.15, "PARTIAL_ALIGNMENT": 0.10, "LOWER_TF_COUNTERTREND": 0.05,
              "HIGHER_TF_CONFLICT": -0.10, "RANGE_CONDITION": -0.05}.get(inputs.alignment_label, 0.0)
    reasons.append(inputs.alignment_label)
    if blocker is not None and blocker_dist is not None and blocker_dist < required:
        strength = float(getattr(blocker, "strength", 1.0))
        penalty = min(0.20, 0.05 * strength)
        score -= penalty; reasons.append(f"{getattr(blocker, 'kind', 'level')} at {getattr(blocker, 'price', 0):.2f} blocks {blocker_dist:.2f} into the move")
    if inputs.spread > 0 and inputs.spread / move > 0.05: score -= 0.05; reasons.append("wide spread")
    if inputs.scout_verdict == "CONFIRMS": score += 0.05
    elif inputs.scout_verdict == "CONTRADICTS": score -= 0.10; reasons.append("scouts contradict")
    if inputs.market_speed == "SLOW": score -= 0.05
    structural = max(0.0, min(1.0, round(score + 0.10, 3)))
    hist = inputs.historical_plus_target_rate
    enough_history = inputs.historical_samples >= int(cfg.minimum_history_samples) and hist is not None
    probability = round(0.5 * structural + 0.5 * hist, 3) if enough_history else structural
    if enough_history: reasons.append(f"history: {inputs.historical_samples} comparable, +{move:.0f} hit rate {hist:.2f}")
    if inputs.historical_fakeout_rate is not None and enough_history and inputs.historical_fakeout_rate > 0.5:
        probability = round(probability - 0.05, 3); reasons.append("high historical fakeout rate")
    structural_verdict = ("ACHIEVABLE" if structural >= float(cfg.achievable_probability_threshold)
                          else "STRETCHED" if structural >= float(cfg.stretched_probability_threshold) else "UNLIKELY")
    if not enough_history:
        verdict = "INSUFFICIENT_HISTORY"
        reasons.insert(0, f"{inputs.historical_samples}/{cfg.minimum_history_samples} comparable observations; structural read {structural_verdict}")
    else:
        verdict = ("ACHIEVABLE" if probability >= float(cfg.achievable_probability_threshold)
                   else "STRETCHED" if probability >= float(cfg.stretched_probability_threshold) else "UNLIKELY")
    return _result(inputs, move, target, required, direction, verdict, "; ".join(reasons), remaining, required_velocity, current_velocity,
                   session_range, consumed, est_remaining, blocker, blocker_dist, probability, structural, structural_verdict)


def _nearest_blocker(levels: list[Any], current: float, target: float, long: bool):
    lo, hi = (current, target) if long else (target, current)
    candidates = []
    for level in levels:
        price = float(getattr(level, "price", 0.0)); kind = str(getattr(level, "kind", ""))
        if not (lo < price < hi): continue
        opposing = (kind.endswith("H") or "HIGH" in kind.upper()) if long else (kind.endswith("L") or "LOW" in kind.upper())
        if opposing or kind in ("VWAP", "PDC"):
            candidates.append((abs(price - current), level))
    if not candidates: return None, None
    dist, level = min(candidates, key=lambda item: item[0])
    return level, round(dist, 2)


def _result(inputs, move, target, required, direction, verdict, reason, remaining, req_v, cur_v, srange, consumed, est_rem,
            blocker, blocker_dist, probability, structural, structural_verdict) -> dict[str, Any]:
    return {
        "session_target_price_move": move, "target_price": target, "required_price_move": required, "expected_direction": direction,
        "target_verdict": verdict, "remaining_session_minutes": round(remaining, 1), "required_velocity": req_v, "current_velocity": cur_v,
        "session_range": srange, "session_range_consumed": consumed, "estimated_remaining_range": est_rem,
        "nearest_blocking_liquidity": {"kind": getattr(blocker, "kind", None), "price": getattr(blocker, "price", None),
                                       "strength": getattr(blocker, "strength", None)} if blocker is not None else None,
        "distance_to_blocking_liquidity": blocker_dist,
        "historical_samples": inputs.historical_samples, "historical_target_hit_rate": inputs.historical_plus_target_rate,
        "historical_fakeout_rate": inputs.historical_fakeout_rate,
        "estimated_probability": probability, "structural_probability": structural, "structural_verdict": structural_verdict,
        "target_reason": reason, "note": "Estimate from current conditions and past comparable sessions; not a guarantee.",
    }
