from __future__ import annotations

from typing import Any

from .models import Pivot


def detect_chart_patterns(pivots: list[Pivot], tolerance: float) -> list[dict[str, Any]]:
    """Conservative candidate/confirmed detection; complex geometry is left extensible."""
    highs = [pivot for pivot in pivots if pivot.kind == "HIGH"]
    lows = [pivot for pivot in pivots if pivot.kind == "LOW"]
    patterns: list[dict[str, Any]] = []
    for values, candidate, confirmed in (
        (highs, "Double Top Candidate", "Double Top"),
        (lows, "Double Bottom Candidate", "Double Bottom"),
    ):
        if len(values) < 2 or abs(values[-1].price - values[-2].price) > tolerance:
            continue
        between = [p for p in pivots if values[-2].timestamp < p.timestamp < values[-1].timestamp]
        neckline = min((p.price for p in between if p.kind == "LOW"), default=None) if values is highs else max((p.price for p in between if p.kind == "HIGH"), default=None)
        patterns.append(
            {
                "name": candidate if neckline is None else confirmed + " Candidate",
                "timestamp": values[-1].confirmation_timestamp,
                "neckline": neckline,
                "status": "CANDIDATE",
            }
        )
    if len(highs) >= 3 and abs(highs[-1].price - highs[-3].price) <= tolerance and highs[-2].price > highs[-1].price + tolerance:
        patterns.append({"name": "Head and Shoulders Candidate", "timestamp": highs[-1].confirmation_timestamp, "status": "CANDIDATE"})
    if len(lows) >= 3 and abs(lows[-1].price - lows[-3].price) <= tolerance and lows[-2].price < lows[-1].price - tolerance:
        patterns.append({"name": "Inverse Head and Shoulders Candidate", "timestamp": lows[-1].confirmation_timestamp, "status": "CANDIDATE"})
    return patterns
