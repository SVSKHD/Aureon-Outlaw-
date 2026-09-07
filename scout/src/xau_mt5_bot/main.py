from __future__ import annotations

import argparse
import os
import signal
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import load_config, load_environment, mt5_terminal_path
from .engine import TradingEngine
from .sessions import SessionEngine
from .firestore_sink import FirestoreSink
from .logger import AuditLogger
from .mt5_client import MT5Client
from .notify import Discord
from .report import format_report
from .telemetry import Telemetry


def cli() -> int:
    parser = argparse.ArgumentParser(description="XAUUSD price-action + session-scout MT5 bot")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--loop", action="store_true", help="Run continuously (default: one cycle)")
    args = parser.parse_args()
    loaded_env = load_environment(Path(args.config).resolve().parent / ".env", override=True)
    config = load_config(args.config)
    firebase_env = os.getenv("FIREBASE_KEY_PATH")
    if firebase_env and not Path(firebase_env).is_absolute():
        os.environ["FIREBASE_KEY_PATH"] = str((Path(config.project_dir) / firebase_env).resolve())
    logger = AuditLogger(config.logging.sqlite_path, config.logging.jsonl_path, config.logging.retention_days, config.logging.jsonl_rotate_daily)
    integ = config.integrations
    discord = Discord(integ.discord_webhook_env, integ.discord_min_interval_seconds,
                      config.reporting.discord_scout_pair_min_interval_seconds,
                      integ.discord_retry_count, integ.discord_retry_backoff_seconds,
                      integ.discord_status_mode, integ.discord_event_level)
    sink = FirestoreSink(integ.firebase_key_path, integ.firestore_push_seconds, integ.series_sample_seconds,
                         integ.firestore_series_max_points)
    telemetry = Telemetry(flush_seconds=integ.telemetry_flush_seconds)
    # Two independent delivery workers (v2.0.0 item 19): Discord rate-limit back-off can no longer delay Firestore, and both
    # queues are drained on graceful shutdown. Every audit event is fanned out with its trading date and Discord result (item 15).
    import queue, threading
    class Outbox:
        def __init__(self, name: str) -> None:
            self.q: "queue.Queue[tuple]" = queue.Queue(maxsize=2000); self.name = name; self.dropped = 0
            self.stopping = threading.Event()
            self.thread = threading.Thread(target=self._worker, daemon=True, name=f"delivery-{name}"); self.thread.start()
        def _worker(self) -> None:
            while True:
                try: job = self.q.get(timeout=0.5)
                except queue.Empty:
                    if self.stopping.is_set(): return
                    continue
                if job is None: self.q.task_done(); return
                try: job[0](*job[1:])
                except Exception: pass
                self.q.task_done()
        def put(self, fn, *args) -> bool:
            try: self.q.put_nowait((fn, *args)); return True
            except queue.Full:
                self.dropped += 1; print(f"INTEGRATION WARNING — {self.name} delivery queue full; message dropped", flush=True); return False
        def drain(self, timeout: float) -> bool:
            """Bounded: never blocks on a full queue (v2.2.0 item 5). Lets the worker finish what it can, then stops."""
            self.stopping.set()
            try: self.q.put_nowait(None)
            except queue.Full: pass
            self.thread.join(timeout); return not self.thread.is_alive()
    discord_box, firestore_box = Outbox("discord"), Outbox("firestore")
    import uuid
    current_tdate: dict[str, Any] = {"value": None}
    engine_ref: dict[str, Any] = {}
    def _discord_then_mark(kind, payload, event_id):
        posted = False
        try: posted = bool(discord.event(kind, payload))
        except Exception: posted = False
        firestore_box.put(sink.mark_discord, event_id, posted)                   # result flows to Firestore through ITS OWN queue
    _base_event = logger.event
    def fanout(kind, payload):
        _base_event(kind, payload)
        event_id = uuid.uuid4().hex
        tdate = getattr(engine_ref.get("engine"), "cycle_tdate", None) if engine_ref.get("engine") else None
        if tdate is None:                                                          # v2.2.0 item 4: startup / pre-cycle events
            tdate = SessionEngine.broker_trading_date(datetime.now(UTC))
        eligible = discord.is_eligible(kind)
        firestore_box.put(sink.event, kind, payload, tdate, eligible, event_id)  # independent of Discord
        if eligible:
            discord_box.put(_discord_then_mark, kind, payload, event_id)
    logger.event = fanout
    client = MT5Client(mt5_terminal_path(), config.safety.deal_history_max_days, symbol=config.symbol,
                       tick_max_age_seconds=config.safety.broker_market_stale_seconds,
                       broker_timestamp_offset_seconds=config.safety.broker_timestamp_offset_seconds)
    try:
        validation = client.initialize()                                            # attach to the logged-in terminal; no credentials
    except Exception as exc:
        logger.event("startup_failed", {"stage": "mt5_initialize", "error": str(exc), "terminal_path": mt5_terminal_path()})
        print(f"STARTUP FAILED — {exc}", flush=True)
        return 2                                                                     # no trading loop, no order
    logger.event("mt5_validated", validation)
    engine = TradingEngine(client, config, logger); engine_ref["engine"] = engine
    running = True
    consecutive_errors = 0
    fanout("startup", {"symbol": config.symbol, "scouts": config.safety.allow_scout_orders, "pa_orders": config.safety.allow_pa_orders,
                       "live": config.safety.allow_live_account, "discord": discord.enabled(), "firestore": sink.enabled(),
                       "env_file_authoritative_keys": sorted(loaded_env)})
    if not discord.enabled():
        message = "Discord disabled: webhook environment value is missing"
        print(f"INTEGRATION WARNING — {message}", flush=True); fanout("integration_disabled", {"integration": "discord", "reason": message})
    if not sink.enabled():
        message = sink.last_error or "Firestore unavailable"
        print(f"INTEGRATION WARNING — {message}", flush=True); fanout("integration_disabled", {"integration": "firestore", "reason": message})

    def stop(_signum, _frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        while running:
            t0 = time.time()
            try:
                first_cycle = engine.last_cycle is None                                     # v2.0.0 item 17: evaluate BEFORE run_cycle
                snapshot = engine.run_cycle()
                tdate = engine.sessions.broker_trading_date(snapshot.timestamp)
                current_tdate["value"] = tdate
                print(format_report(snapshot, config.display_timezone), flush=True)
                discord_box.put(discord.on_snapshot, snapshot, config.display_timezone)
                firestore_box.put(sink.on_snapshot, snapshot, tdate, first_cycle or engine.session_boundary_event)
                telemetry.record_cycle((time.time() - t0) * 1000, {"action": snapshot.decision.action.value, "session": snapshot.session.value,
                                                                  "freshness": snapshot.freshness.value, "spread": snapshot.spread,
                                                                  "confluence": snapshot.confluence, "market_speed": snapshot.scout.market_speed,
                                                                  "discord_enabled": discord.enabled(), "discord_error": discord.last_error,
                                                                  "firestore_enabled": sink.enabled(), "firestore_error": sink.last_error,
                                                                  "calibration_status": snapshot.reporting.get("calibration_status"),
                                                                  "signal_go": snapshot.go_status, "target_verdict": snapshot.analysis.get("session_target", {}).get("target_verdict")})
                cycle_seconds = time.time() - t0
                if cycle_seconds > config.poll_seconds:                                    # v1.9.0: visible when a cycle overruns the poll
                    logger.event("cycle_slow", {"cycle_seconds": round(cycle_seconds, 2), "poll_seconds": config.poll_seconds,
                                                "pattern_scan": snapshot.reporting.get("pattern_scan")})
                consecutive_errors = 0
                hb = telemetry.heartbeat()
                if hb: firestore_box.put(sink.telemetry, tdate, hb)
            except Exception as exc:
                consecutive_errors += 1
                telemetry.record_error(str(exc))
                logger.event("cycle_error", {"error": str(exc), "consecutive": consecutive_errors})
                print(f"NO TRADE — cycle error: {exc}", flush=True)
                if consecutive_errors >= 3:                      # MT5 link probably dead — reconnect once, else let the supervisor restart us
                    try:
                        client.reconnect()
                        from .history import reset_history_cache; reset_history_cache()
                        engine.last_cycle = None                      # re-run bootstrap: adopt positions, re-validate clock
                        engine.startup_cycle = True
                        logger.event("restart", {"kind": "mt5_reconnect"}); consecutive_errors = 0
                    except Exception as re_exc:
                        logger.event("restart", {"kind": "mt5_reconnect_failed", "error": str(re_exc)})
                        raise SystemExit(3)
            if not args.loop:
                break
            time.sleep(max(0.0, config.poll_seconds - (time.time() - t0)))
    finally:
        fanout("shutdown", {"reason": "loop ended", **telemetry.payload()})
        telemetry.heartbeat(force=True)
        engine.shutdown()
        drained_discord = discord_box.drain(timeout=20.0)                                   # v2.1.0 item 4: no short-circuit
        if not drained_discord:
            fanout_plain = _base_event
            fanout_plain("shutdown_discord_pending", {"queued": discord_box.q.qsize(), "note": "late Discord results will show posted_discord=null"})
        drained_firestore = firestore_box.drain(timeout=20.0)                               # runs after Discord so mark_discord updates are included
        if not (drained_discord and drained_firestore):
            print(f"INTEGRATION WARNING — queues did not drain within 20 s (discord={drained_discord}, firestore={drained_firestore})", flush=True)
        client.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
