"""Operational telemetry: cycle timing, MT5 latency, error counts, data freshness, heartbeat file for the supervisor."""
from __future__ import annotations

import json
import os
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Telemetry:
    def __init__(self, heartbeat_path: str = "data/heartbeat.json", flush_seconds: int = 60) -> None:
        self.path = Path(heartbeat_path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.flush_seconds = flush_seconds
        self.cycle_ms: deque[float] = deque(maxlen=120)
        self.errors: deque[str] = deque(maxlen=50)
        self.cycles = 0; self.error_count = 0; self.started = time.time(); self._last_flush = 0.0
        self.last: dict[str, Any] = {}
        self.broker_clock: dict[str, Any] = {}          # v3.3.0: detected broker offset, visible in heartbeat.json

    def record_cycle(self, ms: float, snapshot_summary: dict[str, Any]) -> None:
        self.cycle_ms.append(ms); self.cycles += 1; self.last = snapshot_summary

    def record_broker_clock(self, payload: dict[str, Any]) -> None:
        self.broker_clock = dict(payload or {})

    def record_error(self, message: str) -> None:
        self.error_count += 1; self.errors.append(f"{datetime.now(UTC).isoformat()} {message[:300]}")

    def payload(self) -> dict[str, Any]:
        ms = list(self.cycle_ms)
        return {"ts": datetime.now(UTC).isoformat(), "ts_epoch": time.time(), "pid": os.getpid(), "uptime_s": int(time.time() - self.started), "cycles": self.cycles,
                "cycle_ms_avg": round(sum(ms) / len(ms), 1) if ms else None, "cycle_ms_max": round(max(ms), 1) if ms else None,
                "errors": self.error_count, "last_errors": list(self.errors)[-5:], "last": self.last,
                "broker_utc_offset_hours": self.broker_clock.get("broker_utc_offset_hours"),
                "broker_clock_residual_seconds": self.broker_clock.get("residual_skew_seconds", self.broker_clock.get("broker_clock_residual_seconds")),
                "broker_clock_source": self.broker_clock.get("broker_clock_source"),
                "broker_clock_ok": self.broker_clock.get("clock_ok"),
                "broker_server": self.broker_clock.get("broker_server"),
                "broker_clock": self.broker_clock or None}

    def heartbeat(self, force: bool = False) -> dict[str, Any] | None:
        """Write heartbeat file every flush_seconds; the supervisor restarts the bot if this file goes stale."""
        if not force and time.time() - self._last_flush < self.flush_seconds:
            return None
        data = self.payload()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(data, default=str))
        temporary.replace(self.path)
        self._last_flush = time.time()
        return data
