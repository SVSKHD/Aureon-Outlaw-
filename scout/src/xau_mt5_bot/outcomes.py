"""Historical setup-outcome intelligence (v3.0.0 §8, §9, §10).

* `SetupTracker` registers every confirmed PA setup (traded or not) with the features that were known at that moment,
  follows it on closed M1 bars until +target, invalidation or the tracking limit, then classifies the path and stores
  the resolved record. Open setups are persisted so a restart continues them.
* `classify_path()` is a deterministic structure-based label (not P/L based).
* `reliability()` aggregates ONLY records whose decision timestamp is before the query timestamp (no look-ahead) with a
  fallback hierarchy and Wilson intervals.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd

OUTCOME_COLUMNS = [
    "setup_id", "account", "symbol", "config_fingerprint", "timestamp", "trading_date", "session", "direction",
    "pattern_names", "pattern_family", "trigger_source", "trigger_bar_time", "entry_price", "invalidation_price",
    "d1_structure", "h4_structure", "h1_structure", "m15_structure", "m5_structure", "alignment", "treatment",
    "liquidity_context", "sweep_context", "zone_type", "confluence", "market_speed", "atr", "atr_bucket",
    "scout_leader", "scout_strength", "scout_verdict", "spread", "session_minutes_remaining", "remaining_bucket",
    "mfe", "mae", "reached_plus_3", "reached_plus_5", "reached_plus_10", "time_to_plus_3", "time_to_plus_5", "time_to_plus_10",
    "invalidation_hit", "fakeout_classification", "continuation_classification", "final_trade_result", "resolved", "resolved_at",
    "scout_agreed_with_outcome", "payload_json",
]
LEVELS = (3.0, 5.0, 10.0)
LABELS = ("CONTINUATION", "REVERSAL", "FAILED_BREAKOUT", "SWEEP_RECLAIM", "STOP_HUNT_THEN_CONTINUATION", "RANGE_REVERSION", "INCONCLUSIVE")
FALLBACK = (
    ("EXACT", ("session", "direction", "pattern_family", "alignment", "market_speed", "sweep_context", "atr_bucket", "remaining_bucket")),
    ("SAME_PATTERN_SESSION_DIRECTION", ("session", "direction", "pattern_family")),
    ("SAME_PATTERN_SESSION", ("session", "pattern_family")),
    ("SAME_SESSION_REGIME", ("session", "market_speed")),
    ("BROAD_SESSION_HISTORY", ("session",)),
)


def ensure_schema(database: sqlite3.Connection) -> None:
    database.execute("""
        CREATE TABLE IF NOT EXISTS setup_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_id TEXT NOT NULL, account TEXT NOT NULL DEFAULT 'unknown', symbol TEXT NOT NULL DEFAULT 'unknown',
            config_fingerprint TEXT, timestamp TEXT NOT NULL, trading_date TEXT, session TEXT, direction TEXT,
            pattern_names TEXT, pattern_family TEXT, trigger_source TEXT, trigger_bar_time TEXT, entry_price REAL, invalidation_price REAL,
            d1_structure TEXT, h4_structure TEXT, h1_structure TEXT, m15_structure TEXT, m5_structure TEXT, alignment TEXT, treatment TEXT,
            liquidity_context TEXT, sweep_context TEXT, zone_type TEXT, confluence INTEGER, market_speed TEXT, atr REAL, atr_bucket TEXT,
            scout_leader TEXT, scout_strength INTEGER, scout_verdict TEXT, spread REAL, session_minutes_remaining REAL, remaining_bucket TEXT,
            mfe REAL, mae REAL, reached_plus_3 INTEGER, reached_plus_5 INTEGER, reached_plus_10 INTEGER,
            time_to_plus_3 REAL, time_to_plus_5 REAL, time_to_plus_10 REAL,
            invalidation_hit INTEGER, fakeout_classification TEXT, continuation_classification TEXT, final_trade_result TEXT,
            resolved INTEGER DEFAULT 0, resolved_at TEXT, scout_agreed_with_outcome INTEGER, payload_json TEXT,
            UNIQUE(account, symbol, config_fingerprint, setup_id)
        )""")
    database.execute("CREATE INDEX IF NOT EXISTS ix_setup_outcomes_scope ON setup_outcomes(account, symbol, config_fingerprint, timestamp)")


# ----------------------------------------------------------------------------------------------------------- features
def pattern_family(names: list[str]) -> str:
    text = " ".join(names).lower()
    if "sweep" in text or "reclaim" in text: return "SWEEP"
    if "failed" in text: return "FAILED_BREAK"
    if "choch" in text: return "CHOCH"
    if "bos" in text: return "BOS"
    if "engulf" in text: return "ENGULFING"
    if "pin" in text or "hammer" in text or "star" in text: return "REJECTION"
    if "inside" in text or "compression" in text or "squeeze" in text: return "COMPRESSION"
    if "double" in text or "head" in text or "triangle" in text or "flag" in text or "wedge" in text: return "CHART"
    return "OTHER"


def atr_bucket(atr: float) -> str:
    return "LOW" if atr < 1.5 else "MID" if atr < 3.5 else "HIGH"


def remaining_bucket(minutes: float) -> str:
    return "EARLY" if minutes >= 240 else "MID" if minutes >= 90 else "LATE"


# ----------------------------------------------------------------------------------------------------- classification
def classify_path(direction: str, sweep_context: str, trigger_source: str, mfe: float, mae: float,
                  reached: dict[float, bool], invalidation_hit: bool, reclaimed_through_level: bool,
                  favourable_threshold: float, adverse_threshold: float, treatment: str = "") -> tuple[str, str]:
    """Deterministic (fakeout_label, continuation_label) from the price path and the structural context.

    continuation_label: CONTINUATION when the favourable threshold was reached before invalidation and the move was not
    reclaimed through the level; REVERSAL when invalidated without reaching it; INCONCLUSIVE otherwise.
    fakeout_label follows §9 with the extra structural cases."""
    hit_fav = mfe >= favourable_threshold
    if invalidation_hit and not hit_fav:
        if sweep_context not in ("", "NONE"):
            fake = "SWEEP_RECLAIM" if reclaimed_through_level else "REVERSAL"
        elif trigger_source == "M5" or treatment in ("BREAKOUT", "FAILED_BREAKOUT"):
            fake = "FAILED_BREAKOUT"
        elif treatment == "RANGE_REVERSION":
            fake = "RANGE_REVERSION"
        else:
            fake = "REVERSAL"
        return fake, "REVERSAL"
    if hit_fav:
        if mae >= adverse_threshold and not invalidation_hit:
            return "STOP_HUNT_THEN_CONTINUATION", "CONTINUATION"
        if reclaimed_through_level:
            return "SWEEP_RECLAIM" if sweep_context not in ("", "NONE") else "REVERSAL", "INCONCLUSIVE"
        return "CONTINUATION", "CONTINUATION"
    if invalidation_hit:
        return "REVERSAL", "REVERSAL"
    return "INCONCLUSIVE", "INCONCLUSIVE"


# ----------------------------------------------------------------------------------------------------------- tracker
class SetupTracker:
    """Follows registered setups on closed M1 bars. State lives in `meta['open_setups']` (positions state file)."""

    def __init__(self, store: "OutcomeStore", meta: dict[str, Any], save, cfg: Any) -> None:
        self.store = store; self.meta = meta; self.save = save; self.cfg = cfg
        self.meta.setdefault("open_setups", {})

    def register(self, features: dict[str, Any]) -> bool:
        key = features["setup_id"]
        if key in self.meta["open_setups"] or self.store.exists(features["account"], features["symbol"], features["config_fingerprint"], key):
            return False
        self.store.insert(features)
        self.meta["open_setups"][key] = {"features": features, "mfe": 0.0, "mae": 0.0, "hits": {}, "last_bar": None,
                                         "reclaimed": False, "opened": features["timestamp"]}
        self.save()
        return True

    def update(self, m1_closed: pd.DataFrame, now: datetime, session_ended: bool = False, trade_results: dict[str, str] | None = None) -> list[dict[str, Any]]:
        resolved = []
        if m1_closed is None or len(m1_closed) == 0: return resolved
        times = pd.to_datetime(m1_closed["time"], utc=True)
        touched = False
        for key, state in list(self.meta["open_setups"].items()):
            f = state["features"]; long = f["direction"] == "LONG"
            entry = float(f["entry_price"]); inval = f.get("invalidation_price")
            since = pd.Timestamp(state["last_bar"]) if state["last_bar"] else pd.Timestamp(f["timestamp"])
            bars = m1_closed[times > since]
            if len(bars): touched = True
            for _, bar in bars.iterrows():
                fav = (float(bar.high) - entry) if long else (entry - float(bar.low))
                adv = (entry - float(bar.low)) if long else (float(bar.high) - entry)
                state["mfe"] = max(state["mfe"], round(fav, 2)); state["mae"] = max(state["mae"], round(adv, 2))
                minutes = (pd.Timestamp(bar.time) - pd.Timestamp(f["timestamp"])).total_seconds() / 60.0 + 1.0
                for level in LEVELS:
                    if str(level) not in state["hits"] and fav >= level: state["hits"][str(level)] = round(minutes, 1)
                if inval is not None:
                    crossed = (float(bar.low) <= float(inval)) if long else (float(bar.high) >= float(inval))
                    closed_through = (float(bar.close) < float(inval)) if long else (float(bar.close) > float(inval))
                    if closed_through: state["reclaimed"] = True
                    if crossed: state["invalidated"] = True; state["invalidated_at"] = pd.Timestamp(bar.time).isoformat()
                state["last_bar"] = pd.Timestamp(bar.time).isoformat()
                if state.get("invalidated") or str(LEVELS[-1]) in state["hits"]: break
            age = (now - datetime.fromisoformat(f["timestamp"])).total_seconds() / 60.0
            done = state.get("invalidated") or str(LEVELS[-1]) in state["hits"] or session_ended or age >= float(self.cfg.max_tracking_minutes)
            if done:
                resolved.append(self._resolve(key, state, now, (trade_results or {}).get(key)))
        if resolved or touched: self.save()                                              # progress persists across restarts
        return resolved

    def _resolve(self, key: str, state: dict[str, Any], now: datetime, trade_result: str | None) -> dict[str, Any]:
        f = state["features"]; hits = state["hits"]
        reached = {level: str(level) in hits for level in LEVELS}
        fake, cont = classify_path(f["direction"], f.get("sweep_context", "NONE"), f.get("trigger_source", "NONE"), state["mfe"], state["mae"],
                                   reached, bool(state.get("invalidated")), bool(state.get("reclaimed")),
                                   float(self.cfg.favourable_threshold), float(self.cfg.adverse_threshold), f.get("treatment", ""))
        leader = f.get("scout_leader", "NONE"); direction = f["direction"]
        agreed = None
        if leader in ("BUY", "SELL") and cont in ("CONTINUATION", "REVERSAL"):
            leader_dir = "LONG" if leader == "BUY" else "SHORT"
            agreed = (leader_dir == direction) if cont == "CONTINUATION" else (leader_dir != direction)
        outcome = {"mfe": state["mfe"], "mae": state["mae"],
                   "reached_plus_3": int(reached[3.0]), "reached_plus_5": int(reached[5.0]), "reached_plus_10": int(reached[10.0]),
                   "time_to_plus_3": hits.get("3.0"), "time_to_plus_5": hits.get("5.0"), "time_to_plus_10": hits.get("10.0"),
                   "invalidation_hit": int(bool(state.get("invalidated"))), "fakeout_classification": fake, "continuation_classification": cont,
                   "final_trade_result": trade_result or f.get("final_trade_result") or "NOT_TRADED", "resolved": 1, "resolved_at": now.isoformat(),
                   "scout_agreed_with_outcome": None if agreed is None else int(agreed)}
        self.store.resolve(f["account"], f["symbol"], f["config_fingerprint"], key, outcome)
        self.meta["open_setups"].pop(key, None)
        return {"setup_id": key, **outcome}

    def note_trade_result(self, setup_id: str, result: str) -> None:
        state = self.meta["open_setups"].get(setup_id)
        if state: state["features"]["final_trade_result"] = result
        else: self.store.set_trade_result(setup_id, result)


# ------------------------------------------------------------------------------------------------------------- store
class OutcomeStore:
    def __init__(self, connect) -> None:
        self._connect = connect
        with self._connect() as db: ensure_schema(db)

    def exists(self, account: str, symbol: str, fp: str, setup_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM setup_outcomes WHERE account=? AND symbol=? AND config_fingerprint=? AND setup_id=?",
                              (account, symbol, fp, setup_id)).fetchone() is not None

    def insert(self, features: dict[str, Any]) -> None:
        cols = [c for c in OUTCOME_COLUMNS if c != "payload_json"]
        values = [features.get(c) for c in cols]
        values = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in values]
        with self._connect() as db:
            db.execute(f"INSERT OR IGNORE INTO setup_outcomes({','.join(cols)},payload_json) VALUES({','.join('?' * len(cols))},?)",
                       (*values, json.dumps(features, default=str)))

    def resolve(self, account: str, symbol: str, fp: str, setup_id: str, outcome: dict[str, Any]) -> None:
        sets = ", ".join(f"{k}=?" for k in outcome)
        with self._connect() as db:
            db.execute(f"UPDATE setup_outcomes SET {sets} WHERE account=? AND symbol=? AND config_fingerprint=? AND setup_id=?",
                       (*outcome.values(), account, symbol, fp, setup_id))

    def set_trade_result(self, setup_id: str, result: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE setup_outcomes SET final_trade_result=? WHERE setup_id=?", (result, setup_id))

    def resolved_before(self, account: str, symbol: str, fp: str | None, before: datetime, limit: int = 2000) -> list[dict[str, Any]]:
        """Only records whose decision timestamp is strictly before `before` and that are resolved (no look-ahead)."""
        sql = "SELECT * FROM setup_outcomes WHERE account=? AND symbol=? AND resolved=1 AND timestamp<?"
        args: list[Any] = [account, symbol, before.isoformat()]
        if fp is not None: sql += " AND config_fingerprint=?"; args.append(fp)
        sql += " ORDER BY timestamp DESC LIMIT ?"; args.append(limit)
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(sql, args)]

    def window_summary(self, account: str, symbol: str, fp: str | None, start: datetime, end: datetime, session: str | None = None) -> dict[str, Any]:
        """Aggregate resolved setups decided within [start, end) — for session and weekly reports (§13)."""
        sql = "SELECT * FROM setup_outcomes WHERE account=? AND symbol=? AND timestamp>=? AND timestamp<?"
        args: list[Any] = [account, symbol, start.isoformat(), end.isoformat()]
        if fp is not None: sql += " AND config_fingerprint=?"; args.append(fp)
        if session is not None: sql += " AND session=?"; args.append(session)
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(sql, args)]
        resolved = [r for r in rows if r.get("resolved")]
        n = len(resolved)
        def rate(field): return round(sum(1 for r in resolved if r.get(field)) / n, 3) if n else None
        labels: dict[str, int] = {}
        for r in resolved: labels[r.get("fakeout_classification") or "INCONCLUSIVE"] = labels.get(r.get("fakeout_classification") or "INCONCLUSIVE", 0) + 1
        agree = [r.get("scout_agreed_with_outcome") for r in resolved if r.get("scout_agreed_with_outcome") is not None]
        traded = [r for r in rows if (r.get("final_trade_result") or "NOT_TRADED") != "NOT_TRADED"]
        by_session: dict[str, dict[str, Any]] = {}
        for r in resolved:
            b = by_session.setdefault(r.get("session") or "UNKNOWN", {"samples": 0, "plus_10": 0, "fakeouts": 0})
            b["samples"] += 1; b["plus_10"] += int(bool(r.get("reached_plus_10")))
            b["fakeouts"] += int((r.get("fakeout_classification") or "") in ("REVERSAL", "FAILED_BREAKOUT", "SWEEP_RECLAIM", "RANGE_REVERSION"))
        for b in by_session.values():
            b["plus_10_rate"] = round(b["plus_10"] / b["samples"], 3) if b["samples"] else None
            b["fakeout_rate"] = round(b["fakeouts"] / b["samples"], 3) if b["samples"] else None
        return {"setups_detected": len(rows), "setups_resolved": n, "setups_traded": len(traded), "pending_resolution": len(rows) - n,
                "plus_3_rate": rate("reached_plus_3"), "plus_5_rate": rate("reached_plus_5"), "plus_10_rate": rate("reached_plus_10"),
                "fakeout_rate": round(sum(v for k, v in labels.items() if k in ("REVERSAL", "FAILED_BREAKOUT", "SWEEP_RECLAIM", "RANGE_REVERSION")) / n, 3) if n else None,
                "classifications": labels, "median_mfe": _median([r.get("mfe") for r in resolved]), "median_mae": _median([r.get("mae") for r in resolved]),
                "scout_agreement_rate": round(sum(1 for a in agree if a) / len(agree), 3) if agree else None,
                "reliability_by_session": by_session, "note": "Observed outcomes of detected setups; historical, not instructions."}

    def counts(self, account: str, symbol: str, fp: str) -> dict[str, int]:
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM setup_outcomes WHERE account=? AND symbol=? AND config_fingerprint=?", (account, symbol, fp)).fetchone()[0]
            resolved = db.execute("SELECT COUNT(*) FROM setup_outcomes WHERE account=? AND symbol=? AND config_fingerprint=? AND resolved=1", (account, symbol, fp)).fetchone()[0]
        return {"registered": int(total), "resolved": int(resolved)}


# --------------------------------------------------------------------------------------------------------- reliability
def wilson(successes: int, n: int, z: float = 1.96) -> list[float] | None:
    if n <= 0: return None
    p = successes / n; denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def _median(values: list[float]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals: return None
    mid = len(vals) // 2
    return round(vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2, 2)


def reliability(records: list[dict[str, Any]], current: dict[str, Any], minimum_samples: int, min_level_samples: int = 5) -> dict[str, Any]:
    """Fallback hierarchy; uses the first level with at least `min_level_samples` (EXACT preferred), reports the level used."""
    chosen_level, subset = "NONE", []
    for level, dims in FALLBACK:
        subset = [r for r in records if all(str(r.get(d)) == str(current.get(d)) for d in dims)]
        if len(subset) >= min_level_samples:
            chosen_level = level; break
    if chosen_level == "NONE":
        subset = records; chosen_level = "BROAD_SESSION_HISTORY" if records else "NONE"
    n = len(subset)
    def rate(field: str) -> float | None:
        return round(sum(1 for r in subset if r.get(field)) / n, 3) if n else None
    fake_n = sum(1 for r in subset if r.get("fakeout_classification") in ("REVERSAL", "FAILED_BREAKOUT", "SWEEP_RECLAIM", "RANGE_REVERSION"))
    agree = [r.get("scout_agreed_with_outcome") for r in subset if r.get("scout_agreed_with_outcome") is not None]
    return {
        "status": "SUFFICIENT_SAMPLE" if n >= minimum_samples else "INSUFFICIENT_SAMPLE",
        "comparison_level": chosen_level, "samples": n,
        "plus_3_hit_rate": rate("reached_plus_3"), "plus_5_hit_rate": rate("reached_plus_5"), "plus_10_hit_rate": rate("reached_plus_10"),
        "fakeout_rate": round(fake_n / n, 3) if n else None,
        "median_mfe": _median([r.get("mfe") for r in subset]), "median_mae": _median([r.get("mae") for r in subset]),
        "median_time_to_plus_5_minutes": _median([r.get("time_to_plus_5") for r in subset if r.get("time_to_plus_5") is not None]),
        "scout_agreement_rate": round(sum(1 for a in agree if a) / len(agree), 3) if agree else None,
        "confidence_interval_plus_10": wilson(sum(1 for r in subset if r.get("reached_plus_10")), n),
        "confidence_interval_fakeout": wilson(fake_n, n),
        "note": "Historical frequencies of comparable setups; they do not guarantee the current outcome.",
    }
