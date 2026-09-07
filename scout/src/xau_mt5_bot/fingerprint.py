"""Strategy fingerprint (v1.8.0): hash of every parameter that changes PA direction, entries, exits or sizing.
Confirmed-trade counts used for strategy readiness are scoped to the fingerprint, so a parameter change restarts the sample."""
from __future__ import annotations

import hashlib
import json

from .config import BotConfig


def strategy_fingerprint(config: BotConfig) -> str:
    material = {
        "symbol": config.symbol,
        "analysis": config.analysis.model_dump(),
        "management": config.management.model_dump(),
        "risk": config.risk.model_dump(),
        "scout_analysis": config.scout_analysis.model_dump(),
        "analysis_window_bars": dict(config.analysis_window_bars),
        "sessions": config.sessions.model_dump(),
        "safety": config.safety.model_dump(),
        "magic": config.magic.model_dump(),
        "session_target": config.session_target.model_dump(),                   # v3.0.0: feasibility gate changes GO/NO-GO
        "outcomes": {k: v for k, v in config.outcomes.model_dump().items() if k != "enabled"},
        "intermarket": config.intermarket.model_dump(),                          # v3.1.0: silver evidence changes confluence
    }
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(encoded.encode()).hexdigest()[:12]
