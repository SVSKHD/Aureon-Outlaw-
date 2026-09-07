"""Firestore writes for the Vue app. Bot writes, Vue subscribes with onSnapshot. Fails soft."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from .logger import json_default
from .models import AnalysisSnapshot


class FirestoreSink:
    SCHEMA_VERSION = "3.3.0"

    def __init__(self, key_path: str, push_seconds: int = 300, series_seconds: int = 30,
                 series_max_points: int = 240) -> None:
        self.db = None
        self.push_seconds, self.series_seconds = push_seconds, series_seconds
        self.series_max_points = series_max_points
        self.last_error: str | None = None
        self._last_push = self._last_series = 0.0
        self._series: dict[str, dict[str, list]] = {}
        path = os.environ.get("FIREBASE_KEY_PATH", key_path)
        if os.path.exists(path):
            try:
                import firebase_admin
                from firebase_admin import credentials, firestore
                if not firebase_admin._apps:
                    firebase_admin.initialize_app(credentials.Certificate(path))
                self.db = firestore.client()
            except Exception as exc:
                self.last_error = str(exc)
                self.db = None
        else:
            self.last_error = f"service account file not found: {path}"

    def enabled(self) -> bool:
        return self.db is not None

    @staticmethod
    def _clean(value: Any) -> Any:
        return json.loads(json.dumps(value, default=json_default))

    @staticmethod
    def doc_id(s: AnalysisSnapshot, tdate) -> str:
        return f"{tdate.isoformat()}_{s.session.value}"

    def _summary(self, s: AnalysisSnapshot) -> dict[str, Any]:
        plan, sc, zone = s.trade_plan, s.scout, (s.zones[0] if s.zones else None)
        return {
            "schema_version": self.SCHEMA_VERSION,
            "date": None, "session": s.session.value, "symbol": s.symbol, "updated_ts": datetime.now(UTC),
            "price": {"bid": s.bid, "ask": s.ask, "spread": s.spread, "freshness": s.freshness.value,
                      "broker_utc_offset_hours": (getattr(s, "analysis", {}).get("broker_clock") or {}).get("broker_utc_offset_hours")},
            "structure": {tf: r.state.value for tf, r in s.structures.items()},
            "pa": {"side": s.pa_side.value if s.pa_side else None, "confluence": s.confluence, "entry_state": s.entry_state.value,
                   "zone": {"lo": zone.low, "hi": zone.high, "kind": zone.kind} if zone else None,
                   "trigger": {"confirmed": s.trigger.confirmed, "source": s.trigger.source, "reason": s.trigger.reason,
                               "touch": s.trigger.touch_time.isoformat() if s.trigger.touch_time else None}},
            "scouts": {"session": sc.session.value, "leader": sc.leader, "verdict": sc.verdict.value, "strength": sc.strength,
                       "market_speed": sc.market_speed, "guidance": sc.guidance,
                       "buy": {"ticket": sc.buy_ticket, "entry": sc.buy_entry, "pnl": sc.buy_pnl, "mfe": sc.buy_mfe, "mae": sc.buy_mae},
                       "sell": {"ticket": sc.sell_ticket, "entry": sc.sell_entry, "pnl": sc.sell_pnl, "mfe": sc.sell_mfe, "mae": sc.sell_mae},
                       "displacement": sc.displacement, "velocity": sc.velocity,
                       "velocity_direction": sc.velocity_direction, "pace_range": sc.pace_range,
                       "pace_window_minutes": sc.pace_window_minutes, "strength_source": sc.strength_source,
                       "calibration_sessions": sc.calibration_sessions, "pace_calibrated": sc.pace_calibrated,
                       "emergency_sl_price_distance": sc.emergency_sl_price_distance,
                       "estimated_pair_risk_actual": sc.estimated_pair_risk_actual,
                       "estimated_pair_risk_reference_lot": sc.estimated_pair_risk_reference_lot},
            "plan": self._clean(asdict(plan)) if plan else None,
            "final": {"action": s.decision.action.value, "go": s.go_status, "reason": s.decision.reason, "ts": s.decision.timestamp},
            "reporting": self._clean(s.reporting),
            "analysis": self._clean(getattr(s, "analysis", {})),
            "patterns": [str(p.get("name", p.get("event", ""))) for p in s.patterns[-12:]],
            "sweeps": [self._clean(asdict(e)) for e in s.sweeps[-6:]],
            "levels": {lv.kind: lv.price for lv in s.liquidity if not lv.kind.startswith("ROUND")},
        }

    def on_snapshot(self, s: AnalysisSnapshot, tdate, force: bool = False) -> None:
        if not self.db:
            return
        now = time.time(); did = self.doc_id(s, tdate)
        if did not in self._series:                                                      # v2.0.0 item 16: continue an existing series after restart
            self._series[did] = self._load_series(did)
        ser = self._series[did]
        if now - self._last_series >= self.series_seconds:
            ser["t"].append(s.timestamp.isoformat()); ser["bid"].append(s.bid)
            ser["buy_pnl"].append(s.scout.buy_pnl); ser["sell_pnl"].append(s.scout.sell_pnl); ser["action"].append(s.decision.action.value)
            for values in ser.values():
                if len(values) > self.series_max_points:
                    del values[:-self.series_max_points]
            self._last_series = now
        if force or now - self._last_push >= self.push_seconds:
            try:
                doc = self._summary(s); doc["date"] = tdate.isoformat()
                ref = self.db.collection("sessions").document(did)
                ref.set(self._clean(doc) | {"updated_ts": datetime.now(UTC)}, merge=True)
                ref.collection("heavy").document("series").set(ser)
                self.db.collection("days").document(tdate.isoformat()).set(
                    {"date": tdate.isoformat(), "sessions": {s.session.value: {"final": s.decision.action.value, "go": s.go_status, "pa": s.pa_side.value if s.pa_side else None,
                                                                                "confluence": s.confluence, "scout_leader": s.scout.leader}}}, merge=True)
                self._last_push = now
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)

    def _load_series(self, did: str) -> dict[str, list]:
        empty = {"t": [], "bid": [], "buy_pnl": [], "sell_pnl": [], "action": []}
        try:
            snap = self.db.collection("sessions").document(did).collection("heavy").document("series").get()
            data = snap.to_dict() if getattr(snap, "exists", False) else None
            if isinstance(data, dict) and all(isinstance(data.get(k), list) for k in empty):
                return {k: list(data[k])[-self.series_max_points:] for k in empty}
        except Exception as exc:
            self.last_error = str(exc)
        return empty

    def event(self, kind: str, payload: dict[str, Any], tdate=None, discord_eligible: bool | None = None,
              event_id: str | None = None) -> None:
        """Written independently of Discord (v2.1.0 item 3). `tdate` is captured at emission. `posted_discord` starts as
        null and is set to true/false by mark_discord() once the Discord worker reports its result; `discord_eligible`
        tells the reader whether a result is expected at all (item 8)."""
        if not self.db:
            return
        try:
            doc = {"ts": datetime.now(UTC), "kind": kind, "date": tdate.isoformat() if tdate else None,
                   "payload": self._clean(payload), "discord_eligible": discord_eligible, "posted_discord": None}
            if event_id:
                self.db.collection("events").document(event_id).set(doc)
            else:
                self.db.collection("events").add(doc)
        except Exception as exc:
            self.last_error = str(exc)

    def mark_discord(self, event_id: str, posted: bool) -> None:
        if not self.db or not event_id:
            return
        try:
            self.db.collection("events").document(event_id).set({"posted_discord": bool(posted)}, merge=True)
        except Exception as exc:
            self.last_error = str(exc)

    def telemetry(self, tdate, payload: dict[str, Any]) -> None:
        if not self.db:
            return
        try:
            self.db.collection("telemetry").document(tdate.isoformat()).set({"updated_ts": datetime.now(UTC), **self._clean(payload)}, merge=True)
        except Exception as exc:
            self.last_error = str(exc)
