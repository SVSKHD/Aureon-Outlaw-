from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .candles import detect_candlestick_patterns
from .charts import detect_chart_patterns
from .config import BotConfig
from .decision_router import blocked_by, final_decision_router, router_vetoes
from .context import premium_discount, session_vwap, trendlines
from .execution import account_is_safe, estimate_pair_margin, execute_pa_trade
from .position_manager import PositionManager
from .liquidity import is_neutral
from .features import closed_bars, freshness_age_seconds, with_candle_features
from .history import analysis_window, load_native_history, pattern_window
from .intermarket import UNAVAILABLE as INTERMARKET_UNAVAILABLE, assess_intermarket, intermarket_patterns
from .pattern_scan import compute_patterns, lower_priority
from .mtf import alignment as mtf_alignment, treatment as mtf_treatment
from .session_target import TargetInputs, assess_session_target
from .outcomes import SetupTracker, atr_bucket, pattern_family, reliability as outcome_reliability, remaining_bucket
from .liquidity import (
    current_daily_levels,
    detect_sweeps,
    previous_period_levels,
    round_number_levels,
    session_levels,
    swing_and_equal_levels,
)
from .logger import AuditLogger
from .models import (
    Action,
    AnalysisSnapshot,
    Decision,
    DecisionInput,
    EntryState,
    Freshness,
    LiquidityLevel,
    ScoutVerdict,
    SessionName,
    Side,
    SpreadState,
    StructureState,
    TargetRealism,
)
from .mt5_client import TradingClient
from .orb import opening_ranges
from .scouts import ScoutManager
from .sessions import SessionBoundary, SessionEngine
from .smc import detect_fvgs, detect_order_blocks, weighted_sr_zones
from .structure import analyze_structure
from .fingerprint import strategy_fingerprint
from .trigger import carry_trigger, choose_trigger, current_zone_touch_time, detect_m1_trigger, detect_m5_confirmation
from .volatility import analyze_volatility
from .zones import build_trade_plan, classify_entry, select_entry_zone


class TradingEngine:
    def __init__(self, client: TradingClient, config: BotConfig, logger: AuditLogger) -> None:
        self.client = client
        self.config = config
        self.logger = logger
        self.sessions = SessionEngine(config.sessions)
        self.scouts = ScoutManager(client, config, logger.event, getattr(logger, "event_once", None))
        self.scouts.orders_ok = self.orders_allowed
        self.scouts.session_bounds = self._session_bounds

        self.last_cycle: datetime | None = None
        self.pending_boundaries: list[tuple[SessionBoundary, int]] = []
        self._boundary_retry_after: dict[str, datetime] = {}
        self.clock_ok = True; self.clock_skew = 0.0
        self.broker_clock: dict[str, Any] = {}                    # v3.3.0: detected broker timezone offset + residual skew
        self._last_offset_reported: float | None = None
        self.last_trigger = None; self.last_trigger_time = None
        self.active_setup_id: str | None = None
        self.account_key = ""
        self.session_boundary_event = False
        self.pa_session_end_event = False
        self.startup_cycle = True
        self.closed_sessions: list[tuple[SessionName, datetime, dict[str, Any]]] = []
        self._history_warnings: set[str] = set()
        self.strategy_fingerprint = strategy_fingerprint(config)                                                     # v1.8.0 item 7
        self.positions = PositionManager(client, config, record=getattr(logger, "trade", None), audit=logger.event,
                                         config_fingerprint=self.strategy_fingerprint)
        if hasattr(logger, "context"):
            logger.context = {**getattr(logger, "context", {}), "config_fingerprint": self.strategy_fingerprint}
        self.strategy_samples = 0                                     # scoped count loaded once the account key is known (v2.2.0 item 1)
        self._pattern_cache: dict[str, Any] | None = None                                                            # v1.9.0
        self._pattern_future = None; self._pattern_pending_key = None; self._pattern_submitted_at = None
        self._pattern_executor = None; self._pattern_executor_kind = "sync"; self._pattern_retry_after = None
        self._pattern_status = "NONE"
        self.last_pattern_scan_ms: float = 0.0
        self.research_translation: dict[str, Any] = {}
        self._last_go_status = "NO-GO"
        self.outcomes = logger.outcome_store() if hasattr(logger, "outcome_store") else None
        self.setup_tracker: SetupTracker | None = None
        self._reliability_cache: tuple[Any, dict[str, Any]] | None = None
        self._session_target_state: dict[str, Any] = {}
        self._intermarket_failures = 0; self._intermarket_reported = False
        self._last_scout_failure_key: str | None = None                                                                  # v3.2.0                                        # v3.1.0
        self.last_intermarket: dict[str, Any] = dict(INTERMARKET_UNAVAILABLE)

    def orders_allowed(self) -> tuple[bool, str]:
        """Gate for every order: broker clock sane, market open, account safe, tick fresh."""
        if not self.clock_ok:
            offset = self.broker_clock.get("offset_hours")
            detail = f" (broker offset {offset:+g}h already removed)" if offset else ""
            return False, (f"broker clock skew {self.clock_skew:.0f}s > "
                           f"{self.config.safety.max_clock_skew_seconds}s{detail}")
        now = getattr(self, "cycle_now", None) or datetime.now(UTC)
        if not self.sessions.calendar_open(now):
            return False, "market closed by weekend/holiday/early-close calendar"
        try:
            tick = self.client.get_tick(self.config.symbol)
            if (now - tick.time).total_seconds() > self.config.safety.broker_market_stale_seconds:
                return False, "no fresh tick — market closed or feed down"
        except Exception as exc:
            return False, str(exc)
        safe, reason = account_is_safe(self.client, self.config)
        if not safe: return safe, reason
        self._finalize_realized(now)                                                  # v2.0.0 items 2/3: losses closed since last cycle count now
        account = self.client.account_state()
        tdate = self.sessions.broker_trading_date(now).isoformat()
        return self.positions.risk_allowed(tdate, account.balance)

    def _go_store(self) -> dict[str, Any]:
        meta = getattr(self.positions, "meta", None)
        if meta is None:
            meta = self.positions.meta = {}
        return meta.setdefault("session_go", {})

    def _session_key(self, session: SessionName, now: datetime) -> str:
        start = self._session_start_for(session, now + timedelta(seconds=1))
        return f"{self.strategy_fingerprint}:{session.value}@{start.isoformat()}"    # v2.3.0 item 2: scoped by fingerprint

    @staticmethod
    def _key_time(key: str) -> str:
        return key.split("@", 1)[1] if "@" in key else ""

    def _record_session_go(self, session: SessionName, go_status: str, now: datetime) -> None:
        if session == SessionName.CLOSED: return
        store = self._go_store()
        key = self._session_key(session, now)
        tally = store.setdefault(key, {"cycles": 0, "go_cycles": 0, "first_go": None, "last_go": None})
        tally["cycles"] += 1
        if go_status == "GO":
            tally["go_cycles"] += 1; tally["last_go"] = now.isoformat()
            tally["first_go"] = tally["first_go"] or now.isoformat()
        if len(store) > 12:                                                   # chronological prune (v2.3.0)
            for stale in sorted(store, key=self._key_time)[:-12]: store.pop(stale, None)
        self.positions._save()                                                # every cycle: cycles_observed exact after a crash

    def _session_go_report(self, session: SessionName, end: datetime | None = None) -> dict[str, Any]:
        """Read-only view of the tally; call _consume_session_go(report) after the report is safely stored (v2.3.0)."""
        store = self._go_store()
        if end is not None:
            key = self._session_key(session, end)
        else:
            suffix = session.value + "@"
            matching = [k for k in store if k.startswith(self.strategy_fingerprint + ":" + suffix)]
            key = max(matching, key=self._key_time) if matching else self.strategy_fingerprint + ":" + session.value
        tally = store.get(key, {"cycles": 0, "go_cycles": 0, "first_go": None, "last_go": None})
        return {"go": "GO" if tally["go_cycles"] > 0 else "NO-GO", "go_basis": "any live GO during this session instance (persisted tally)",
                "session_instance": key, "go_cycles": tally["go_cycles"], "cycles_observed": tally["cycles"],
                "first_go_ts": tally["first_go"], "last_go_ts": tally["last_go"]}

    def _consume_session_go(self, report: dict[str, Any]) -> None:
        if self._go_store().pop(report.get("session_instance"), None) is not None:
            self.positions._save()

    def _remember_friday_close_go(self, close_time: datetime, go_status: str) -> None:
        meta = getattr(self.positions, "meta", None)
        if meta is None: meta = self.positions.meta = {}
        store = meta.setdefault("friday_close_go", {})
        store[f"{self.strategy_fingerprint}:{close_time.isoformat()}"] = go_status          # v2.4.0 item 1: fingerprint-scoped
        for key in sorted(store, key=lambda k: k.split(":", 1)[1] if ":" in k else k)[:-8]: store.pop(key, None)
        self.positions._save()

    def _persist_pending_report(self, session: SessionName, end: datetime, scout_stats: dict[str, Any]) -> None:
        """A session that ended but whose summary has not been written yet — survives a crash mid-boundary-cycle."""
        meta = getattr(self.positions, "meta", None)
        if meta is None: meta = self.positions.meta = {}
        pending = meta.setdefault("pending_reports", {})
        pending[f"{self.strategy_fingerprint}:{session.value}@{end.isoformat()}"] = {
            "session": session.value, "end": end.isoformat(), "scout_stats": scout_stats, "fingerprint": self.strategy_fingerprint}
        self.positions._save()

    def _clear_pending_report(self, session: SessionName, end: datetime) -> None:
        meta = getattr(self.positions, "meta", {}) or {}
        if meta.get("pending_reports", {}).pop(f"{self.strategy_fingerprint}:{session.value}@{end.isoformat()}", None) is not None:
            self.positions._save()

    def _restore_pending_reports(self) -> int:
        """Startup: re-queue session summaries that were closed but never reported (crash between scout close and report).
        Records for another fingerprint, or malformed ones, are discarded and the cleaned state is persisted at once (§5.5)."""
        meta = getattr(self.positions, "meta", {}) or {}
        pending = meta.get("pending_reports", {})
        restored = 0; dropped = []
        for key, item in list(pending.items()):
            reason = None
            if not isinstance(item, dict): reason = "malformed"
            elif item.get("fingerprint") != self.strategy_fingerprint: reason = "fingerprint mismatch"
            else:
                try:
                    session = SessionName(item["session"]); end = datetime.fromisoformat(item["end"])
                    if session == SessionName.CLOSED: reason = "invalid session"
                except Exception:
                    reason = "invalid session/timestamp"
            if reason:
                pending.pop(key, None); dropped.append({"key": key, "reason": reason}); continue
            if not any(s == session and e == end for s, e, _ in self.closed_sessions):
                self.closed_sessions.append((session, end, dict(item.get("scout_stats") or {})))
                restored += 1
        if dropped:
            self.positions._save()
            self.logger.event("pending_report_dropped", {"dropped": dropped})
        if restored:
            self.logger.event("session_summary_recovered", {"count": restored})
            self.session_boundary_event = True
        return restored

    def _friday_close_go(self, close_time: datetime, fallback: str) -> dict[str, Any]:
        stored = (getattr(self.positions, "meta", {}) or {}).get("friday_close_go", {}).get(f"{self.strategy_fingerprint}:{close_time.isoformat()}")
        if stored is not None:
            return {"go": stored, "go_basis": "live GO status recorded at the Friday NY close boundary"}
        return {"go": fallback, "go_basis": "CATCH-UP: Friday-close GO was not recorded (bot down at close); this is the GO at report time"}

    def _audit_research_translation(self) -> dict[str, Any]:
        """Translate the research reference-lot quantities through order_calc_profit so reports use broker arithmetic.
        Research scale only: this never gates trading (v3.0.0)."""
        rep = self.config.reporting
        calc = getattr(self.client, "calc_profit", None)
        result: dict[str, Any] = {"scale": "NORMALISED_RESEARCH_VALUES_NOT_A_TRADING_PERMISSION", "reference_lot": rep.research_reference_lot,
                                  "source": "order_calc_profit" if calc else "unavailable"}
        try:
            tick = self.client.get_tick(self.config.symbol); base = float(tick.bid)
            move_pnl = abs(float(calc(self.config.symbol, "LONG", rep.research_reference_lot, base, base + rep.research_daily_price_move))) if calc else None
        except Exception as exc:
            move_pnl = None; result["error"] = str(exc)
        result.update({"research_daily_price_move": rep.research_daily_price_move, "research_daily_move_pnl_usd": move_pnl,
                       "research_daily_usd_configured": rep.research_daily_usd})
        warnings = []
        if move_pnl is not None and abs(move_pnl - rep.research_daily_usd) > 0.10 * rep.research_daily_usd:
            warnings.append(f"research_daily_usd {rep.research_daily_usd:.0f} differs from broker P/L of a {rep.research_daily_price_move} move at {rep.research_reference_lot} lot ({move_pnl:.0f})")
        result["warnings"] = warnings
        self.logger.event("research_translation", result)
        return result

    def _pattern_reliability(self, now: datetime, current: dict[str, Any], m1_closed: pd.DataFrame) -> dict[str, Any]:
        """Historical reliability of comparable setups, from records decided BEFORE `now`; cached per closed M1 bar + feature set."""
        empty = {"status": "INSUFFICIENT_SAMPLE", "comparison_level": "NONE", "samples": 0, "plus_3_hit_rate": None, "plus_5_hit_rate": None,
                 "plus_10_hit_rate": None, "fakeout_rate": None, "median_mfe": None, "median_mae": None, "median_time_to_plus_5_minutes": None,
                 "scout_agreement_rate": None, "confidence_interval_plus_10": None, "confidence_interval_fakeout": None,
                 "note": "Historical frequencies of comparable setups; they do not guarantee the current outcome."}
        if self.outcomes is None or not self.config.outcomes.enabled or current.get("direction") is None: return empty
        bar = pd.Timestamp(m1_closed.iloc[-1].time).isoformat() if len(m1_closed) else None
        key = (bar, tuple(sorted(current.items())))
        if self._reliability_cache and self._reliability_cache[0] == key: return self._reliability_cache[1]
        fp = self.strategy_fingerprint if self.config.outcomes.scope_to_fingerprint else None
        records = self.outcomes.resolved_before(self.account_key, self.config.symbol, fp, now)
        result = outcome_reliability(records, current, self.config.outcomes.minimum_samples, self.config.outcomes.min_level_samples)
        self._reliability_cache = (key, result)
        return result

    def _outcomes_window(self, start: datetime, end: datetime, session: str | None) -> dict[str, Any]:
        if self.outcomes is None: return {}
        try:
            fp = self.strategy_fingerprint if self.config.outcomes.scope_to_fingerprint else None
            return self.outcomes.window_summary(self.account_key, self.config.symbol, fp, start, end, session)
        except Exception as exc:
            return {"error": str(exc)}

    def _target_feasibility_accuracy(self, session: str, end: datetime) -> dict[str, Any]:
        """How the live target verdicts issued in this session compared with the realised +target outcomes."""
        tally = (getattr(self.positions, "meta", {}) or {}).get("target_verdicts", {}).pop(session, None)
        if tally is None: return {"verdicts": 0}
        self.positions._save()
        return tally

    def _note_target_verdict(self, session: SessionName, verdict: str, resolved_hit: bool | None) -> None:
        if session == SessionName.CLOSED or verdict in ("DISABLED", None): return
        meta = getattr(self.positions, "meta", None)
        if meta is None: return
        store = meta.setdefault("target_verdicts", {}).setdefault(session.value, {"verdicts": 0, "by_verdict": {}})
        store["verdicts"] += 1; store["by_verdict"][verdict] = store["by_verdict"].get(verdict, 0) + 1

    def _spread_evidence(self) -> dict[str, Any]:
        samples = getattr(self, "_spread_samples", [])
        if not samples: return {}
        s = sorted(samples)
        return {"samples": len(s), "median_spread": round(s[len(s) // 2], 3), "max_spread": round(s[-1], 3),
                "scout_pair_spread_cost_price": round(2 * s[len(s) // 2], 3), "note": "two spreads per scout pair are paid in demo P/L"}

    def _session_windows(self, now: datetime) -> list[tuple[str, str, str]]:
        """(start, end, session) UTC windows over the pattern lookback, for historical session-range percentiles (once per day)."""
        day = now.date()
        if getattr(self, "_session_windows_cache", None) and self._session_windows_cache[0] == day:
            return self._session_windows_cache[1]
        windows: list[tuple[str, str, str]] = []
        events = []
        for delta in range(-self.config.analysis.pattern_lookback_days - 1, 1):
            events.extend(self.sessions.boundaries_for_utc_day((now + timedelta(days=delta)).date()))
        events = sorted(events, key=lambda e: e.timestamp)
        for i, e in enumerate(events[:-1]):
            if e.kind == "START":
                windows.append((e.timestamp.isoformat(), events[i + 1].timestamp.isoformat(), e.session.value))
        self._session_windows_cache = (day, windows)
        return windows

    def _session_end(self, now: datetime) -> datetime | None:
        current = self.sessions.session_at(now)
        if current == SessionName.CLOSED: return None
        events = []
        for delta in (0, 1):
            events.extend(self.sessions.boundaries_for_utc_day((now + timedelta(days=delta)).date()))
        later = sorted(e.timestamp for e in events if e.timestamp > now)
        return later[0] if later else None

    def _session_extrema(self, m1_closed: pd.DataFrame, session_start: datetime | None, tick) -> tuple[float | None, float | None]:
        if session_start is None or m1_closed is None or len(m1_closed) == 0: return None, None
        times = pd.to_datetime(m1_closed["time"], utc=True)
        chunk = m1_closed[times >= pd.Timestamp(session_start)]
        if chunk.empty: return None, None
        return round(max(float(chunk.high.max()), float(tick.ask)), 2), round(min(float(chunk.low.min()), float(tick.bid)), 2)

    def _setup_features(self, setup_id: str, now: datetime, snapshot: AnalysisSnapshot, structures, mtf, treat, atr: float,
                        remaining_minutes: float, plan, selected_zone) -> dict[str, Any]:
        names = [str(p.get("name", "")) for p in snapshot.patterns][-8:]
        sweep = next((sw for sw in snapshot.sweeps if getattr(sw, "active", False)), None)
        levels = sorted(snapshot.liquidity, key=lambda l: abs(l.price - snapshot.bid))[:3]
        return {
            "setup_id": setup_id, "account": self.account_key, "symbol": self.config.symbol, "config_fingerprint": self.strategy_fingerprint,
            "timestamp": now.isoformat(), "trading_date": self.sessions.broker_trading_date(now).isoformat(), "session": snapshot.session.value,
            "direction": snapshot.pa_side.value if snapshot.pa_side else None, "pattern_names": names, "pattern_family": pattern_family(names),
            "trigger_source": snapshot.trigger.source, "trigger_bar_time": (snapshot.trigger.trigger_bar_time or now).isoformat(),
            "entry_price": float(plan.entry) if plan else (snapshot.ask if snapshot.pa_side == Side.LONG else snapshot.bid),
            "invalidation_price": float(plan.stop_loss) if plan else None,
            "d1_structure": mtf["timeframes"]["D1"]["state"], "h4_structure": mtf["timeframes"]["H4"]["state"], "h1_structure": mtf["timeframes"]["H1"]["state"],
            "m15_structure": mtf["timeframes"]["M15"]["state"], "m5_structure": mtf["timeframes"]["M5"]["state"],
            "alignment": mtf["label"], "treatment": treat["treatment"],
            "liquidity_context": [{"kind": l.kind, "price": l.price} for l in levels],
            "sweep_context": f"{sweep.level_type}:{sweep.direction}" if sweep else "NONE",
            "zone_type": getattr(selected_zone, "kind", None), "confluence": int(snapshot.confluence), "market_speed": snapshot.scout.market_speed,
            "atr": round(atr, 3), "atr_bucket": atr_bucket(atr), "scout_leader": snapshot.scout.leader, "scout_strength": int(snapshot.scout.strength),
            "scout_verdict": snapshot.scout.verdict.value, "spread": round(snapshot.spread, 3),
            "session_minutes_remaining": round(remaining_minutes, 1), "remaining_bucket": remaining_bucket(remaining_minutes),
        }

    def _finalize_realized(self, now: datetime) -> None:
        """Recognise trades that closed at the broker since the last check (SL/TP/manual) before any order decision."""
        if getattr(self, "_finalizing", False):
            return
        self._finalizing = True
        try:
            tick = self.client.get_tick(self.config.symbol)
            for closed_trade in self.positions.update(pd.DataFrame(), bid=tick.bid, ask=tick.ask):
                self.logger.event("pa_trade_result" if closed_trade["kind"] == "PA" else "scout_trade_result", closed_trade)
                if closed_trade["kind"] == "PA" and closed_trade.get("config_fingerprint") == self.strategy_fingerprint and closed_trade.get("account") == self.account_key: self.strategy_samples += 1   # v2.1.0 item 10 / v2.2.0 item 1
        finally:
            self._finalizing = False

    def _validate_clock(self, now: datetime) -> None:
        """v3.3.0: the broker TIMEZONE offset is removed by the client first; only what is left over is skew.

        Before v3.3.0 this compared a broker-server tick stamp with system UTC, so any broker that is not on
        UTC (a UTC+3 server → 10 800 s) blocked every scout and PA order forever."""
        info = self._broker_clock_info(now)
        self.broker_clock = info
        self.clock_skew = float(info.get("residual_seconds", 0.0))
        ok = abs(self.clock_skew) <= self.config.safety.max_clock_skew_seconds or not self.sessions.calendar_open(now)
        offset_hours = float(info.get("offset_hours", 0.0))
        if self._last_offset_reported != offset_hours or self.last_cycle is None:
            self.logger.event("broker_clock_offset", {
                "offset_hours": offset_hours, "residual_skew_seconds": round(self.clock_skew, 1),
                "raw_delta_seconds": info.get("raw_delta_seconds"), "source": info.get("source"),
                "server": info.get("server"), "max_clock_skew_seconds": self.config.safety.max_clock_skew_seconds,
                "message": (f"Broker server clock is UTC{offset_hours:+g}; all MT5 tick and bar times are converted to UTC. "
                            f"Residual skew {self.clock_skew:.0f}s."),
            })
            self._last_offset_reported = offset_hours
        if ok != self.clock_ok or self.last_cycle is None:
            self.logger.event("clock_check", {"tick_minus_system_seconds": round(self.clock_skew, 1), "ok": ok,
                                              "broker_utc_offset_hours": offset_hours,
                                              "message": "ok" if ok else "MT5 tick time far from system UTC after removing the broker timezone offset — orders blocked; check the PC clock"})
        self.clock_ok = ok

    def _broker_clock_info(self, now: datetime) -> dict[str, Any]:
        """Single conversion point: ask the client. Clients without the v3.3.0 API fall back to raw tick-vs-UTC."""
        refresh = getattr(self.client, "refresh_broker_offset", None)
        if callable(refresh):
            try:
                return dict(refresh(now=now) or {})
            except Exception as exc:
                self.logger.event("broker_clock_error", {"error": str(exc)})
        tick = self.client.get_tick(self.config.symbol)
        delta = (tick.time - now).total_seconds()
        return {"offset_hours": 0.0, "offset_seconds": 0.0, "residual_seconds": round(delta, 1),
                "raw_delta_seconds": round(delta, 1), "source": "none", "server": "", "measured_at": None}

    def _handle_sessions(self, now: datetime) -> None:
        self.session_boundary_event = False
        self.pa_session_end_event = False
        self.closed_sessions = []
        current = self.sessions.session_at(now)
        if self.last_cycle is None:
            self.scouts.adopt_existing(current, now)                                   # 1. restart recovery
            self.positions.adopt_untracked(current.value, self.sessions.broker_trading_date(now).isoformat())
            if current != self.scouts.current_session and current.value != "CLOSED":
                result = self.scouts.open_session(current, now)
                self.logger.event("scout_bootstrap", {"success": result.success, "message": result.message})
                if not result.success:
                    self._report_scout_failure(current, result)
                if not result.success and result.retryable:
                    self.pending_boundaries.append((SessionBoundary(now, "START", current), 0))
            return
        new = [(b, 0) for b in self.sessions.events_between(self.last_cycle, now)]
        queue = self.pending_boundaries + new; self.pending_boundaries = []
        self.session_boundary_event = bool(queue)
        for boundary, attempts in queue:                                              # 2. retry failed transitions
            retry_key = f"{boundary.kind}:{boundary.session.value}:{boundary.timestamp.isoformat()}"
            if now < self._boundary_retry_after.get(retry_key, now):
                self.pending_boundaries.append((boundary, attempts)); continue
            if boundary.kind == "START" and self.sessions.session_at(now) != boundary.session:
                continue                                                              # superseded by a later boundary
            ended_session = self.scouts.current_session
            self.scouts.last_closed_summary = None
            # Close and finalize the old scout pair before opening the next pair so its
            # realized loss can activate the account-wide day lock immediately.
            if boundary.kind == "START" and ended_session != SessionName.CLOSED:
                close_result = self.scouts.close_session(ended_session)
                if close_result.success:
                    self.scouts.current_session = SessionName.CLOSED
                    self._finalize_boundary_closes(now)
                    result = self.scouts.open_session(boundary.session, boundary.timestamp)
                else:
                    result = close_result
            elif boundary.kind == "CLOSE":
                result = self.scouts.close_session(boundary.session)
                if result.success:
                    self.scouts.current_session = SessionName.CLOSED
                    self._finalize_boundary_closes(now)
            else:
                result = self.scouts.open_session(boundary.session, boundary.timestamp)
            self.logger.event("session_transition", {"session": boundary.session.value, "kind": boundary.kind, "attempt": attempts + 1,
                                                     "success": result.success, "message": result.message})
            if boundary.kind == "START" and not result.success:
                planned_retry = (now + timedelta(seconds=min(300, 5 * (2 ** min(attempts, 6))))) if result.retryable else None
                self._report_scout_failure(boundary.session, result, attempts + 1, planned_retry)
            old_positions_remain = ended_session != SessionName.CLOSED and bool(
                self.client.positions(self.config.symbol, self.scouts.magic(ended_session)))
            ended = ended_session != SessionName.CLOSED and not old_positions_remain and (boundary.kind == "CLOSE" or boundary.session != ended_session)
            if ended:
                self.closed_sessions.append((ended_session, boundary.timestamp, dict(self.scouts.last_closed_summary or {})))
                self._persist_pending_report(ended_session, boundary.timestamp, dict(self.scouts.last_closed_summary or {}))   # v2.4.0 item 4
                self.pa_session_end_event = True
                if boundary.kind == "CLOSE" and boundary.timestamp.astimezone(ZoneInfo(self.config.sessions.new_york_timezone)).weekday() == 4:
                    self._remember_friday_close_go(boundary.timestamp, self._last_go_status)                 # v2.1.0 item 6
            if not result.success and result.retryable:
                self.pending_boundaries.append((boundary, attempts + 1))
                self._boundary_retry_after[retry_key] = now + timedelta(seconds=min(300, 5 * (2 ** min(attempts, 6))))
                if attempts + 1 == self.config.safety.transition_retry_limit:
                    self.logger.event("session_transition_delayed", {"session": boundary.session.value, "kind": boundary.kind,
                                                                     "message": "Still unresolved; retries continue until confirmed"})
            elif result.success:
                self._boundary_retry_after.pop(retry_key, None)
        self.scouts.retry_pending_closures()                                         # items 7/8
        repair = self.scouts.repair_leg(now)                                         # 14. missing leg
        if repair is not None:
            self.logger.event("scout_repair", {"success": repair.success, "message": repair.message})

    def _finalize_boundary_closes(self, now: datetime) -> None:
        tick = self.client.get_tick(self.config.symbol)
        for closed_trade in self.positions.update(pd.DataFrame(), bid=tick.bid, ask=tick.ask):
            self.logger.event("pa_trade_result" if closed_trade["kind"] == "PA" else "scout_trade_result", closed_trade)
            if closed_trade["kind"] == "PA" and closed_trade.get("config_fingerprint") == self.strategy_fingerprint and closed_trade.get("account") == self.account_key: self.strategy_samples += 1   # v2.1.0 item 10 / v2.2.0 item 1

    def _scan_patterns(self, full_m5: pd.DataFrame, now: datetime, atr: float) -> tuple[list, list, dict]:
        """30-day candlestick/chart/volatility scan on the long M5 window (v1.9.0).
        Synchronous once at startup; afterwards each new closed M5 bar is scanned in a separate worker *process*
        (no GIL contention with the trading cycle). The previous bar's result is served until the worker finishes,
        typically within a few cycles of the bar close; `reporting.pattern_scan` carries bar time, age and status.
        Failures are audited (`pattern_scan_failed`) and retried after `pattern_scan_retry_seconds`."""
        long_m5 = closed_bars(pattern_window(full_m5, self.config, now))
        key = (pd.Timestamp(long_m5.iloc[-1].time).isoformat() if len(long_m5) else None, len(long_m5))
        args = (atr, self.config.analysis.atr_period, self.config.analysis.swing_left, self.config.analysis.swing_right, self._session_windows(now))
        self._collect_pattern_future(now)
        cached = self._pattern_cache
        if cached is not None and cached["key"] == key:
            self._pattern_status = "CURRENT"
            return cached["candles"], cached["charts"], cached["volatility"]
        if cached is None:                                                          # startup: compute once, synchronously
            result = compute_patterns(long_m5, *args)
            self._store_pattern_result(key, result, now)
            self._pattern_status = "CURRENT"
            return result["candles"], result["charts"], result["volatility"]
        busy = self._pattern_future is not None and not self._pattern_future.done()
        retry_blocked = self._pattern_retry_after is not None and now < self._pattern_retry_after
        if not busy and not retry_blocked and self._pattern_pending_key != key:
            self._pattern_pending_key = key
            self._pattern_submitted_at = now
            self._pattern_future = self._submit_pattern_job(long_m5, args)
        self._pattern_status = "PENDING" if (busy or self._pattern_pending_key == key) else "STALE"
        return cached["candles"], cached["charts"], cached["volatility"]

    def _submit_pattern_job(self, long_m5: pd.DataFrame, args: tuple):
        from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
        if self._pattern_executor is None:
            try:
                import multiprocessing as mp
                self._pattern_executor = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"), initializer=lower_priority)
                self._pattern_executor_kind = "process"
            except Exception as exc:                                                # restricted environments: degrade to a thread
                self._pattern_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pattern-scan")
                self._pattern_executor_kind = "thread"
                self.logger.event("pattern_scan_executor", {"kind": "thread", "reason": str(exc)})
        try:
            return self._pattern_executor.submit(compute_patterns, long_m5, *args)
        except Exception as exc:                                                    # broken pool (worker died): rebuild once as thread
            self.logger.event("pattern_scan_executor", {"kind": "thread", "reason": f"submit failed: {exc}"})
            self._pattern_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pattern-scan")
            self._pattern_executor_kind = "thread"
            return self._pattern_executor.submit(compute_patterns, long_m5, *args)

    def _collect_pattern_future(self, now: datetime) -> None:
        future = self._pattern_future
        if future is None or not future.done():
            return
        self._pattern_future = None
        key = self._pattern_pending_key
        self._pattern_pending_key = None
        try:
            result = future.result()
        except Exception as exc:
            self._pattern_retry_after = now + timedelta(seconds=self.config.analysis.pattern_scan_retry_seconds)
            self.logger.event("pattern_scan_failed", {"error": str(exc), "executor": self._pattern_executor_kind,
                                                      "retry_after": self._pattern_retry_after.isoformat()})
            return
        self._pattern_retry_after = None
        self._store_pattern_result(key, result, now)

    def _store_pattern_result(self, key, result: dict[str, Any], now: datetime) -> None:
        self.last_pattern_scan_ms = float(result.get("elapsed_ms", 0.0))
        self._pattern_cache = {"key": key, "candles": result["candles"], "charts": result["charts"],
                               "volatility": result["volatility"], "bar_time": result.get("bar_time"),
                               "completed_at": now, "bars": result.get("bars"), "session_ranges": result.get("session_ranges", {})}
        self.logger.event("pattern_scan_complete", {"bar_time": result.get("bar_time"), "bars": result.get("bars"),
                                                    "elapsed_ms": round(self.last_pattern_scan_ms, 1),
                                                    "executor": self._pattern_executor_kind})

    def pattern_scan_report(self, now: datetime) -> dict[str, Any]:
        cache = self._pattern_cache or {}
        completed = cache.get("completed_at")
        return {"status": self._pattern_status, "bar_time": cache.get("bar_time"), "bars": cache.get("bars"),
                "age_seconds": round((now - completed).total_seconds(), 1) if completed else None,
                "executor": self._pattern_executor_kind, "last_scan_ms": round(self.last_pattern_scan_ms, 1)}

    def shutdown(self) -> None:
        if self._pattern_executor is not None:
            try: self._pattern_executor.shutdown(wait=False, cancel_futures=True)
            except Exception: pass
            self._pattern_executor = None

    @staticmethod
    def _latest_levels(levels: list[LiquidityLevel], now: datetime) -> list[LiquidityLevel]:
        eligible = [level for level in levels if level.valid_from <= now]
        newest: dict[str, LiquidityLevel] = {}
        for level in eligible:
            keep_multiple = level.kind.startswith(("ROUND", "SWING_", "EQUAL_"))
            key = f"{level.kind}:{level.price:.2f}" if keep_multiple else level.kind
            if key not in newest or level.valid_from > newest[key].valid_from:
                newest[key] = level
        return list(newest.values())

    def run_cycle(self, now: datetime | None = None) -> AnalysisSnapshot:
        now = (now or datetime.now(UTC)).astimezone(UTC)
        self.cycle_now = now
        self.cycle_tdate = self.sessions.broker_trading_date(now)                    # v2.2.0 item 4: known before any event of this cycle
        self._validate_clock(now)                                                     # 3. before any order
        if self.last_cycle is None:
            try:
                getter = getattr(self.client, "account_id", None)
                self.account_key = getter() if getter else str(self.client.account_state().login)
            except Exception: self.account_key = "unknown"
            self.positions = PositionManager(self.client, self.config, record=getattr(self.logger, "trade", None), audit=self.logger.event,
                                             account_key=self.account_key, config_fingerprint=self.strategy_fingerprint)                                          # item 9
            self.scouts.state_path = str(self.positions.path.with_name(self.positions.path.name.replace("positions_", "scouts_")))
            self.research_translation = self._audit_research_translation()                                   # v3.0.0: research scale only
            if hasattr(self.logger, "confirmed_trade_count"):
                try:
                    self.strategy_samples = self.logger.confirmed_trade_count(self.strategy_fingerprint, account=self.account_key, symbol=self.config.symbol)
                except TypeError:
                    self.strategy_samples = self.logger.confirmed_trade_count(self.strategy_fingerprint)
            self.scouts.fingerprint = self.strategy_fingerprint; self.scouts.account_key = self.account_key
            self.scouts.restore()
            self._recover_reports_after_sessions = True                                                       # v3.0.0 §5.1
            if self.outcomes is not None and self.config.outcomes.enabled:
                self.setup_tracker = SetupTracker(self.outcomes, self.positions.meta, self.positions._save, self.config.outcomes)
            if self.scouts.calibration_source == "none" and hasattr(self.logger, "scoped_event_count"):   # v2.0.0 item 5: scoped SQLite fallback
                self.scouts.calibration_sessions = self.logger.scoped_event_count("scout_session_stats", self.account_key,
                                                                                  self.config.symbol, self.strategy_fingerprint)
                self.scouts.calibration_source = "sqlite"
            if hasattr(self.logger, "context"):
                self.logger.context = {"account": self.account_key, "symbol": self.config.symbol, "config_fingerprint": self.strategy_fingerprint}   # item 38
        self._handle_sessions(now)
        if getattr(self, "_recover_reports_after_sessions", False):                                         # v3.0.0 §5.1: after the reset inside _handle_sessions
            self._recover_reports_after_sessions = False
            self._restore_pending_reports()
        full = load_native_history(self.client, self.config, account_key=self.account_key)   # 20. cache per symbol+account
        for timeframe, frame in full.items():
            if not frame.attrs.get("history_complete", True) and timeframe not in self._history_warnings:
                self.logger.event("history_incomplete", {"timeframe": timeframe,
                    "requested": frame.attrs.get("requested_bars"), "received": frame.attrs.get("received_bars"),
                    "impact": "Long-horizon levels/patterns may be incomplete"})
                self._history_warnings.add(timeframe)
        frames = analysis_window(full, self.config)
        tick = self.client.get_tick(self.config.symbol)
        m1_age = freshness_age_seconds(frames["M1"], pd.Timestamp(now))
        if m1_age <= 120:
            freshness = Freshness.LIVE
        elif m1_age <= self.config.safety.max_m1_age_seconds:
            freshness = Freshness.WARNING
        else:
            freshness = Freshness.STALE
        spread = tick.spread
        if spread <= self.config.risk.max_spread_price * 0.75:
            spread_state = SpreadState.NORMAL
        elif spread <= self.config.risk.max_spread_price:
            spread_state = SpreadState.ELEVATED
        else:
            spread_state = SpreadState.ABNORMAL

        closed = {key: closed_bars(value) for key, value in frames.items()}
        structures = {
            timeframe: analyze_structure(
                closed[timeframe], timeframe,
                self.config.analysis.swing_left, self.config.analysis.swing_right,
            )
            for timeframe in ("D1", "H4", "H1", "M15", "M5")
        }
        m5_features = with_candle_features(closed["M5"], self.config.analysis.atr_period)
        atr = float(m5_features.iloc[-1].atr) if not m5_features.empty and pd.notna(m5_features.iloc[-1].atr) else 1.0
        pattern_cutoff = pd.Timestamp(now - timedelta(days=self.config.analysis.pattern_lookback_days))
        candle_patterns, chart_patterns, volatility = self._scan_patterns(full["M5"], now, atr)          # v1.8.0 item 2: once per closed M5 bar
        patterns: list[dict[str, Any]] = candle_patterns[-20:] + chart_patterns
        patterns += [{"name": name, "timestamp": now} for name in volatility.get("patterns", [])]
        patterns += [event for result in structures.values() for event in result.events
                     if pd.Timestamp(event["timestamp"]) >= pattern_cutoff][-12:]

        all_levels = previous_period_levels(full["D1"])                                 # 4. native broker-day PDH/PDL/PDC
        all_levels += session_levels(closed["M1"], self.sessions)
        all_levels += opening_ranges(closed["M1"], self.sessions)
        all_levels += current_daily_levels(frames["M1"], full["D1"])
        all_levels += round_number_levels((tick.bid + tick.ask) / 2, now, valid_from=pd.Timestamp(full["D1"].iloc[-1].time).to_pydatetime())   # items 3/5
        all_levels += swing_and_equal_levels(structures["M5"].pivots, atr, self.config.analysis.equal_level_atr_tolerance)
        session_start = self._session_start(now)
        closed_m1_price = float(closed["M1"].iloc[-1].close) if len(closed["M1"]) else (tick.bid + tick.ask) / 2
        vwap_level, vwap_info = session_vwap(closed["M1"], session_start, now, closed_m1_price, closed["M5"])
        if vwap_level: all_levels.append(vwap_level)
        liquidity = self._latest_levels(all_levels, now)
        pd_info = premium_discount(structures["M15"], (tick.bid + tick.ask) / 2)
        lines = trendlines(structures["M5"].pivots, closed["M5"], atr, self.config.analysis.trendline_min_touches, self.config.analysis.trendline_max_slope_atr_per_bar)
        patterns += [{"name": f"VWAP {vwap_info['state']}", "timestamp": now, **vwap_info}] if vwap_info.get("vwap") else []
        patterns += [{"name": pd_info["zone"], "timestamp": now, **pd_info}] if pd_info.get("pct") is not None else []
        patterns += [{"name": f"{l['name']} {l['state']}", "timestamp": l["timestamp"], **l} for l in lines]
        sweeps = detect_sweeps(
            with_candle_features(closed["M5"], self.config.analysis.atr_period),
            liquidity,
            self.config.analysis.sweep_reclaim_bars,
            max_age_bars=self.config.analysis.sweep_max_age_bars,
        )
        zones = detect_fvgs(closed["M5"], self.config.analysis.atr_period, self.config.analysis.fvg_min_atr)
        zones += detect_order_blocks(
            closed["M5"], structures["M5"], self.config.analysis.atr_period,
            self.config.analysis.displacement_atr,
        )
        zones += weighted_sr_zones(structures, atr)
        intermarket = self._intermarket(closed, now)                                                                # v3.1.0: silver evidence
        patterns += intermarket_patterns(intermarket, now)

        daily = with_candle_features(closed["D1"], self.config.analysis.atr_period)
        daily_atr = float(daily.iloc[-1].atr) if not daily.empty and pd.notna(daily.iloc[-1].atr) else atr * 10
        day_start = pd.Timestamp(full["D1"].iloc[-1].time)
        today = frames["M1"][pd.to_datetime(frames["M1"].time, utc=True) >= day_start]
        today_range = float(today.high.max() - today.low.min()) if not today.empty else 0.0
        middle = (tick.bid + tick.ask) / 2
        pa_side, confluence = self._price_action_direction(structures, sweeps, zones, patterns, m5_features, today_range, daily_atr,
                                                           middle, atr, pd_info, vwap_info, intermarket)        # 17. current-cycle inputs
        scout = self.scouts.snapshot(pa_side)
        if scout.verdict == ScoutVerdict.CONFIRMS:
            confluence = min(100, confluence + min(5, scout.strength // 2))
        elif scout.verdict == ScoutVerdict.CONTRADICTS:
            confluence = max(0, confluence - min(10, scout.strength))
        selected = select_entry_zone(zones, pa_side, middle, now, atr) if pa_side else None
        setup_id = f"{pa_side.value}|{selected.kind}|{selected.low:.2f}-{selected.high:.2f}|{selected.valid_from.isoformat()}" if selected and pa_side else None
        if setup_id != self.active_setup_id:                                         # item 1: trigger belongs to one setup only
            if self.last_trigger is not None:
                self.logger.event("trigger_cleared", {"previous_setup": self.active_setup_id, "new_setup": setup_id})
            self.last_trigger = None; self.last_trigger_time = None; self.active_setup_id = setup_id
        selected_zones = [selected] if selected else []
        entry_state = classify_entry(middle, selected, atr, pa_side)
        touch = current_zone_touch_time(frames["M1"], selected, middle) if selected else None
        if selected and pa_side:
            m1_trigger = detect_m1_trigger(
                closed["M1"], selected, pa_side, touch, self.config.analysis.atr_period,
                self.config.analysis.displacement_atr, self.config.analysis.trigger_expiry_m1_bars,
            )
            m5_trigger = detect_m5_confirmation(closed["M5"], selected, pa_side, touch, structures["M5"].pivots)
            trigger = choose_trigger(m1_trigger, m5_trigger)
            trigger = carry_trigger(self.last_trigger, self.last_trigger_time, trigger, pa_side, selected, execution_price_for(pa_side, tick),
                                    atr, closed["M1"], self.config.analysis.trigger_grace_m1_bars)          # 19. keep a valid trigger briefly
            newly_confirmed = trigger.confirmed and (self.last_trigger is None or not self.last_trigger.confirmed)
            if newly_confirmed:                                                        # v1.8.0 items 5/6: delay applies to first detection only; carried triggers keep grace
                tf_seconds = 60 if trigger.source == "M1" else 300
                bar_open = trigger.trigger_bar_time or (pd.Timestamp(closed[trigger.source].iloc[-1].time).to_pydatetime())   # v2.0.0 item 6: the confirming bar, not the newest bar
                detection_delay = max(0.0, (now - (bar_open + timedelta(seconds=tf_seconds))).total_seconds())
                if detection_delay > self.config.analysis.max_trigger_detection_delay_seconds:
                    from dataclasses import replace
                    trigger = replace(trigger, confirmed=False, fresh=False,
                                      reason=f"Trigger detected {detection_delay:.1f}s after bar close; limit is {self.config.analysis.max_trigger_detection_delay_seconds}s")
            if trigger.confirmed:
                trigger.setup_id = setup_id; trigger.direction = pa_side; trigger.zone_kind = selected.kind
                trigger.zone_low = selected.low; trigger.zone_high = selected.high; trigger.zone_valid_from = selected.valid_from
                trigger.confirmation_timestamp = trigger.trigger_bar_time or (pd.Timestamp(closed[trigger.source].iloc[-1].time).to_pydatetime() if trigger.source in closed and len(closed[trigger.source]) else now)
                entry_state = EntryState.CONFIRMED
                if self.last_trigger is None or not self.last_trigger.confirmed:
                    self.last_trigger_time = trigger.trigger_bar_time or (pd.Timestamp(closed["M1"].iloc[-1].time).to_pydatetime() if len(closed["M1"]) else now)   # grace counted from the confirming bar
                self.last_trigger = trigger
            elif self.last_trigger is not None and self.last_trigger_time is not None and \
                    int((pd.to_datetime(closed["M1"].time, utc=True) > pd.Timestamp(self.last_trigger_time)).sum()) > self.config.analysis.trigger_grace_m1_bars:
                self.last_trigger = None
        else:
            from .models import TriggerResult
            trigger = TriggerResult(False, "NONE", reason="No selected PA entry zone")

        execution_price = execution_price_for(pa_side, tick)
        plan = None
        if selected and pa_side:
            plan = build_trade_plan(
                pa_side, execution_price, selected, structures["M5"], sweeps, liquidity,
                atr, daily_atr, today_range, self.config.risk.fixed_pa_lot, self.config.risk.min_actual_rr,   # 6. TP1 ≥ min RR
            )
        trade_results: dict[str, str] = {}
        for closed_trade in self.positions.update(closed["M5"], atr=atr, session_end=self.pa_session_end_event,
                                                  bid=tick.bid, ask=tick.ask):                             # v2.0.0 item 2: finalize BEFORE gating
            self.logger.event("pa_trade_result" if closed_trade["kind"] == "PA" else "scout_trade_result", closed_trade)
            if closed_trade["kind"] == "PA" and closed_trade.get("setup_id"):
                trade_results[str(closed_trade["setup_id"])] = "WIN" if float(closed_trade.get("pnl", 0) or 0) > 0 else "LOSS" if float(closed_trade.get("pnl", 0) or 0) < 0 else "FLAT"
            if closed_trade["kind"] == "PA" and closed_trade.get("config_fingerprint") == self.strategy_fingerprint and closed_trade.get("account") == self.account_key: self.strategy_samples += 1   # v2.1.0 item 10 / v2.2.0 item 1
        if self.setup_tracker is not None:
            try:
                for note_id, note_result in trade_results.items(): self.setup_tracker.note_trade_result(note_id, note_result)
                for resolved in self.setup_tracker.update(closed["M1"], now, session_ended=self.pa_session_end_event, trade_results=trade_results):
                    self.logger.event("setup_outcome_resolved", resolved); self._reliability_cache = None
            except Exception as exc:
                self.logger.event("setup_outcome_error", {"stage": "update", "error": str(exc)})
        account_safe, safe_reason = self.orders_allowed()                                                    # 3/12/15 gate
        if account_safe and plan is not None:
            tdate = self.sessions.broker_trading_date(now).isoformat()
            balance = self.client.account_state().balance
            ok_day, why_day = self.positions.entry_allowed(tdate, self.sessions.session_at(now).value, balance)   # items 20-24
            ok_setup, why_setup = self.positions.setup_allowed(setup_id or "unknown", now)                        # item 16
            if not ok_day or not ok_setup:
                account_safe, safe_reason = False, why_day if not ok_day else why_setup
        decision_input = DecisionInput(
            pa_side, bool(selected), entry_state, trigger, scout, freshness, spread_state,
            account_safe, plan.actual_rr[0] if plan and plan.actual_rr else None,
            self.config.risk.min_actual_rr, plan.target_realism if plan else None,
            confluence, self.config.analysis.min_confluence, setup_id,
            self.config.scout_analysis.strong_contradiction_strength,
            self.config.scout_analysis.hold_when_slow and
            (scout.pace_calibrated or self.config.scout_analysis.hold_before_calibrated),
        )
        decision = final_decision_router(decision_input, now)
        self._decision_input = decision_input                          # v3.3.0: reused for the "Blocked by" list
        snapshot = AnalysisSnapshot(
            now, self.config.symbol, self.sessions.session_at(now), tick.bid, tick.ask, spread,
            freshness, structures, patterns, liquidity, sweeps, selected_zones, pa_side,
            confluence, scout, entry_state, trigger, plan, decision,
        )
        # ---- v3.0.0: multi-timeframe treatment, historical reliability, $target session feasibility ----------------
        mtf = mtf_alignment(structures, pa_side)
        treat = mtf_treatment(pa_side, mtf["label"], sweeps, selected_zones, trigger, structures, patterns)
        session_end = self._session_end(now)
        remaining_minutes = max(0.0, (session_end - now).total_seconds() / 60.0) if session_end else 0.0
        session_total = ((session_end - session_start).total_seconds() / 60.0) if session_end and session_start else 0.0
        session_high, session_low = self._session_extrema(closed["M1"], session_start, tick)
        reference_entry = float(plan.entry) if plan is not None else (tick.ask if pa_side == Side.LONG else tick.bid if pa_side == Side.SHORT else None)
        current_features = {"session": snapshot.session.value, "direction": pa_side.value if pa_side else None,
                            "pattern_family": pattern_family([str(p.get("name", "")) for p in patterns][-8:]), "alignment": mtf["label"],
                            "market_speed": scout.market_speed, "sweep_context": next((f"{sw.level_type}:{sw.direction}" for sw in sweeps if getattr(sw, "active", False)), "NONE"),
                            "atr_bucket": atr_bucket(atr), "remaining_bucket": remaining_bucket(remaining_minutes)}
        rel = self._pattern_reliability(now, current_features, closed["M1"])
        ranges = (self._pattern_cache or {}).get("session_ranges", {}).get(snapshot.session.value, {}) if self._pattern_cache else {}
        target = assess_session_target(TargetInputs(
            side=pa_side, reference_entry=reference_entry, bid=tick.bid, ask=tick.ask, spread=spread, atr=atr,
            remaining_minutes=remaining_minutes, session_minutes_total=session_total, session_high=session_high, session_low=session_low,
            pace_range_per_min=(scout.pace_range / scout.pace_window_minutes) if scout.pace_window_minutes else 0.0,
            velocity_direction=scout.velocity_direction, market_speed=scout.market_speed, liquidity=liquidity,
            alignment_label=mtf["label"], alignment_score=mtf["score"], scout_leader=scout.leader, scout_strength=scout.strength,
            scout_verdict=scout.verdict.value, historical_samples=rel["samples"], historical_plus_target_rate=rel.get("plus_10_hit_rate"),
            historical_fakeout_rate=rel.get("fakeout_rate"), session_range_p50=ranges.get("p50"), session_range_p75=ranges.get("p75"),
            session_range_p90=ranges.get("p90"),
        ), self.config.session_target) if self.config.session_target.enabled else {"target_verdict": "DISABLED"}
        effective_target = target.get("structural_verdict") if target.get("target_verdict") == "INSUFFICIENT_HISTORY" else target.get("target_verdict")
        overrides: dict[str, Any] = {}                              # v3.3.0: post-router vetoes, for the "Blocked by" list
        if (self.config.session_target.enabled and self.config.session_target.block_when_unlikely and effective_target == "UNLIKELY"
                and decision.action.value in {"LONG", "SHORT"}):
            overrides["session_target_unlikely"] = True
            overrides["session_target_detail"] = (f"${self.config.session_target.target_price_move:.0f} session move UNLIKELY: "
                                                  f"{target.get('target_reason')}")
            decision = Decision(Action.NO_TRADE, f"${self.config.session_target.target_price_move:.0f} session move UNLIKELY: {target.get('target_reason')}", now)
            snapshot.decision = decision
            self.logger.event("order_withheld", {**self._withheld_context(plan, selected, trigger, confluence, scout, snapshot), "reason": "session target unlikely", "setup_id": setup_id, "target": target})
        if self.config.session_target.enabled and mtf["label"] == "HIGHER_TF_CONFLICT" and decision.action.value in {"LONG", "SHORT"} \
                and target.get("target_verdict") == "STRETCHED":
            overrides["higher_tf_conflict"] = True
            overrides["higher_tf_conflict_detail"] = f"MTF {mtf['label']} with a STRETCHED session target"
            decision = Decision(Action.WAIT, "Higher-timeframe conflict with a STRETCHED session target; waiting for alignment", now)
            snapshot.decision = decision
        if self.setup_tracker is not None and trigger.confirmed and pa_side is not None and setup_id:
            try:
                self.setup_tracker.register(self._setup_features(setup_id, now, snapshot, structures, mtf, treat, atr, remaining_minutes, plan, selected))
            except Exception as exc:
                self.logger.event("setup_outcome_error", {"stage": "register", "error": str(exc)})
        snapshot.analysis = {
            "multi_timeframe": mtf, "treatment": treat, "session_target": target, "historical_pattern_reliability": rel,
            "live_scout_evidence": {"leader": scout.leader, "strength": scout.strength, "verdict": scout.verdict.value,
                                    "buy_pnl": scout.buy_pnl, "sell_pnl": scout.sell_pnl, "market_speed": scout.market_speed,
                                    "role": "SECONDARY_CONFIRMATION_ONLY"},
            "fakeout_risk": rel.get("fakeout_rate"), "remaining_session_minutes": round(remaining_minutes, 1),
            "atr": round(atr, 3),                                     # v3.3.0: zone distance on the Detected card
            "intermarket": intermarket,                                                                            # v3.1.0
        }
        if self.startup_cycle and decision.action.value in {"LONG", "SHORT"}:                                  # v2.0.0 item 7: never trade on the cold/reconnect cycle
            overrides["cold_start"] = True
            decision = Decision(Action.NO_TRADE, "Cold start: inputs captured before the synchronous history/pattern load; re-evaluating next cycle", now)
            snapshot.decision = decision
            self.logger.event("order_withheld", {**self._withheld_context(plan, selected, trigger, confluence, scout, snapshot), "reason": "cold start cycle", "setup_id": setup_id})
        if plan is not None and decision.action.value in {"LONG", "SHORT"}:
            recheck_ok, recheck_why = self.orders_allowed()                                                   # v2.0.0 item 2: lock re-evaluated at send time
            if recheck_ok:
                recheck_ok, recheck_why = self.positions.entry_allowed(self.sessions.broker_trading_date(now).isoformat(),
                                                                       self.sessions.session_at(now).value, self.client.account_state().balance)
            if not recheck_ok:
                overrides["send_time_withheld"] = True
                overrides["send_time_detail"] = str(recheck_why)
                decision = Decision(Action.NO_TRADE, f"Order withheld at send: {recheck_why}", now)
                snapshot.decision = decision
                self.logger.event("order_withheld", {**self._withheld_context(plan, selected, trigger, confluence, scout, snapshot), "reason": recheck_why, "setup_id": setup_id})
        if plan is not None and decision.action.value in {"LONG", "SHORT"}:
            result = execute_pa_trade(self.client, self.config, decision, plan, self.logger.event)            # 9-13. sizing, order_check, retry, verify
            self.logger.order("PA", result, {"decision": decision, "plan": plan, "result": result, "setup_id": setup_id})
            self.logger.event("order", {                                                          # v3.3.0: the whole trade on one card
                "side": decision.action.value, "ticket": result.ticket, "success": result.success, "retcode": result.retcode,
                "message": result.message, "session": snapshot.session.value, "setup_id": setup_id,
                "entry": round(result.price if result.price else plan.entry, 2), "stop_loss": round(plan.stop_loss, 2),
                "take_profits": [round(x, 2) for x in plan.take_profits], "actual_rr": plan.actual_rr,
                "volume": result.volume_filled or None, "risk_currency": self._plan_risk_currency(plan, result),
                "zone_kind": getattr(selected, "kind", None), "trigger_reason": trigger.reason,
                "confluence": int(confluence), "scout_verdict": scout.verdict.value, "scout_strength": int(scout.strength),
                "scout_leader": scout.leader,
                "silver": f"{intermarket.get('regime', 'n/a')} r={intermarket.get('correlation')} · SMT {intermarket.get('smt', 'NONE')}",
            })
            if result.success:
                from dataclasses import asdict
                for p in self.client.positions(self.config.symbol, self.config.magic.pa):
                    if str(int(p.ticket)) not in self.positions.tracked:
                        inv = selected.low if pa_side == Side.LONG else selected.high
                        self.positions.track(p, "PA", snapshot.session.value, invalidation_price=inv, side=pa_side.value, setup_id=setup_id,
                                             plan=asdict(plan), tdate=self.sessions.broker_trading_date(now).isoformat())
                trigger.consumed = True
                self.last_trigger = None; self.last_trigger_time = None
                self.logger.event("trigger_consumed", {"setup_id": setup_id, "ticket": result.ticket})
        for p in self.scouts_positions_untracked():
            self.positions.track(p, "SCOUT", snapshot.session.value,
                                 tdate=self.sessions.broker_trading_date(now).isoformat())
        snapshot.active_positions = self.positions.status_reports(tick.bid, tick.ask)
        strategy_samples = self.strategy_samples
        strategy_calibrated = strategy_samples >= self.config.analysis.min_strategy_sample_trades
        calibration_status = "CALIBRATED" if (strategy_calibrated and scout.pace_calibrated) else "COLLECTING"
        target_block = snapshot.analysis.get("session_target", {})
        snapshot.go_status = "GO" if snapshot.decision.action.value in {"LONG", "SHORT"} else "NO-GO"    # v3.0.0 §6: demo-signal semantics
        self._last_go_status = snapshot.go_status
        self._record_session_go(snapshot.session, snapshot.go_status, now)                                   # v2.0.0 item 13
        self._spread_samples = (getattr(self, "_spread_samples", []) + [float(spread)])[-2000:]
        if pa_side is not None and target_block.get("target_verdict") not in (None, "DISABLED"):
            self._note_target_verdict(snapshot.session, target_block.get("target_verdict"), None)
        if scout.verdict == ScoutVerdict.CONTRADICTS and scout.strength >= self.config.scout_analysis.strong_contradiction_strength and pa_side is not None:
            key = (snapshot.session.value, pa_side.value, scout.leader)
            if getattr(self, "_last_contradiction_key", None) != key:
                self._last_contradiction_key = key
                self.logger.event("strong_scout_contradiction", {"pa_side": pa_side.value, "leader": scout.leader, "strength": scout.strength, "session": snapshot.session.value})
        try:
            day_ok, day_reason = self.positions.risk_allowed(self.cycle_tdate.isoformat(), self.client.account_state().balance)
        except Exception as exc:                                               # never let the explanation break a cycle
            day_ok, day_reason = True, f"day-lock state unavailable: {exc}"
        gate = {
            "clock_ok": self.clock_ok,
            "clock_detail": (f"residual skew {self.clock_skew:.0f}s > {self.config.safety.max_clock_skew_seconds}s "
                             f"(broker offset {self.broker_clock.get('offset_hours', 0):+g}h already removed)"),
            "day_lock": not day_ok, "day_lock_detail": day_reason,
            "spread": round(snapshot.spread, 3), "max_spread": self.config.risk.max_spread_price,
            "remaining_minutes": remaining_minutes,
            "minimum_remaining_minutes": self.config.session_target.minimum_remaining_minutes if self.config.session_target.enabled else None,
            "account_reason": safe_reason,
            **overrides,
        }
        snapshot.analysis["router_vetoes"] = router_vetoes(self._decision_input, gate)
        snapshot.analysis["blocked_by"] = blocked_by(self._decision_input, gate)
        snapshot.analysis["broker_clock"] = dict(self.broker_clock)
        snapshot.analysis["decision_summary"] = {
            "signal_go": snapshot.go_status, "trade_action": snapshot.decision.action.value, "scout_verdict": scout.verdict.value,
            "target_verdict": target_block.get("target_verdict", "DISABLED"), "calibration_status": calibration_status,
            "reason": snapshot.decision.reason,
            "meaning": "GO = the current demo PA setup is valid and executable under price action, scout evidence, market health, remaining-session conditions and demo risk controls.",
        }
        plan_stop_distance = round(abs(plan.entry - plan.stop_loss), 2) if plan is not None else None
        snapshot.reporting = {
            "pattern_scan": self.pattern_scan_report(now),
            "config_fingerprint": self.strategy_fingerprint,
            "account_mode": "DEMO_ONLY",
            "research_scale": "NORMALISED_RESEARCH_VALUES_NOT_A_TRADING_PERMISSION",
            "research_reference_lot": self.config.reporting.research_reference_lot,
            "research_daily_price_move": self.config.reporting.research_daily_price_move,
            "research_daily_usd": self.config.reporting.research_daily_usd,
            "research_monthly_usd": self.config.reporting.research_monthly_usd,
            "research_translation": self.research_translation,
            "plan_stop_distance_price": plan_stop_distance,
            "scout_buy_pnl_reference_lot": snapshot.scout.buy_pnl_reference_lot,
            "scout_sell_pnl_reference_lot": snapshot.scout.sell_pnl_reference_lot,
            "active_pa_pnl_reference_lot": round(sum(p["pnl_reference_lot"] for p in snapshot.active_positions), 2),
            "pace_metric": "rolling_range_per_minute",
            "pace_range": snapshot.scout.pace_range,
            "velocity_direction": snapshot.scout.velocity_direction,
            "pace_calibrated": snapshot.scout.pace_calibrated,
            "pace_calibration_sessions": snapshot.scout.calibration_sessions,
            "strategy_calibrated": strategy_calibrated,
            "strategy_sample_trades": strategy_samples,
            "calibration_status": calibration_status,
            "scout_mirroring": "PROHIBITED_EVIDENCE_ONLY",
            "demo_combined_realized_pnl": self.positions.day.get("pnl", 0.0),
            "demo_pa_realized_pnl": self.positions.day.get("pa_pnl", 0.0),
            "demo_scout_realized_pnl": self.positions.day.get("scout_pnl", 0.0),
            "scout_pair_risk_actual": snapshot.scout.estimated_pair_risk_actual,
            "scout_pair_risk_reference_lot": snapshot.scout.estimated_pair_risk_reference_lot,
            "setup_outcomes": self.outcomes.counts(self.account_key, self.config.symbol, self.strategy_fingerprint) if self.outcomes is not None else {},
        }
        self.logger.snapshot(snapshot)
        self._emit_reports(now, snapshot, liquidity)
        self.last_cycle = now
        self.startup_cycle = False
        return snapshot

    def _session_start_for(self, session: SessionName, end: datetime) -> datetime:
        events = []
        for delta in (-2, -1, 0):
            events.extend(self.sessions.boundaries_for_utc_day((end + timedelta(days=delta)).date()))
        starts = [e.timestamp for e in events if e.kind == "START" and e.session == session and e.timestamp < end]
        return max(starts) if starts else end - timedelta(hours=12)

    def _emit_reports(self, now: datetime, snapshot: AnalysisSnapshot, liquidity: list[LiquidityLevel]) -> None:
        summarize = getattr(self.logger, "performance_summary", None)
        once = getattr(self.logger, "report_once", None)
        if not summarize or not once or not (self.session_boundary_event or self.startup_cycle):
            return
        report_args = (self.config.reporting.research_reference_lot, self.config.reporting.research_daily_usd,
                       self.config.reporting.research_monthly_usd, self.strategy_fingerprint)
        scope = f"{self.account_key}:{self.config.symbol}:{self.strategy_fingerprint}"        # v2.1.0 item 7: keys and queries per account/symbol/fingerprint
        base_summarize = summarize
        try:
            base_summarize(now, now, None, *report_args, account=self.account_key, symbol=self.config.symbol)
            def summarize(*a, _b=base_summarize): return _b(*a, account=self.account_key, symbol=self.config.symbol)
        except TypeError:
            pass
        for session, end, scout_stats in self.closed_sessions:
            start = self._session_start_for(session, end)
            payload = summarize(start, end, session.value, *report_args)
            pending = []
            for rec in self.positions.pending_finalize.values():
                opened = datetime.fromisoformat(rec["open_time"])
                if rec.get("kind") == "PA" and rec.get("session") == session.value and start <= opened <= end:
                    pending.append({"ticket": rec.get("ticket"), "side": rec.get("side"), "status": "PENDING FINALIZATION"})
            go_report = self._session_go_report(session, end)
            outcomes_block = self._outcomes_window(start, end, session.value)
            target_acc = self._target_feasibility_accuracy(session.value, end)
            payload.update({"symbol": self.config.symbol, "scout": scout_stats, **go_report, "setup_outcomes": outcomes_block,
                            "target_feasibility_accuracy": target_acc, "pattern_scout_agreement_rate": outcomes_block.get("scout_agreement_rate"),
                            "spread_cost_evidence": self._spread_evidence(),
                            "pending_pa_trades": pending, "pending_pa_count": len(pending),
                            "report_status": "COMPLETE", "actionability": "HISTORICAL_SUMMARY",
                            "message": f"{session.value} session closed — review PA and scout evidence before the next session"})
            payload["account"] = self.account_key; payload["config_fingerprint"] = self.strategy_fingerprint
            once("session_summary", f"{scope}:{session.value}:{end.isoformat()}", payload)   # raises on storage failure → tally kept
            self._consume_session_go(go_report)
            self._clear_pending_report(session, end)

        trading_day = self.sessions.broker_trading_date(now)
        week_start_date = trading_day - timedelta(days=trading_day.weekday())
        week_start = datetime.combine(week_start_date, datetime.min.time(), tzinfo=UTC)
        previous_start, previous_end = week_start - timedelta(days=7), week_start
        # Startup is a durable catch-up point; report_once makes repeated boundaries harmless.
        exists = getattr(self.logger, "report_exists", lambda _kind, _key: False)
        weekly_key = previous_start.date().isoformat()
        if not exists("weekly_report", f"{scope}:{weekly_key}"):
            weekly = summarize(previous_start, previous_end, None, *report_args)
            scout_fn = getattr(self.logger, "scout_performance_summary", None)
            try:
                scout_weekly = scout_fn(previous_start, previous_end, self.config.reporting.research_reference_lot,
                                        account=self.account_key, config_fingerprint=self.strategy_fingerprint,
                                        symbol=self.config.symbol) if scout_fn else {}
            except TypeError:                                                          # older fakes without the scope kwargs
                scout_weekly = scout_fn(previous_start, previous_end, self.config.reporting.research_reference_lot)
            sessions_in_week = [item for item in getattr(self.logger, "reports_between", lambda *_a, **_k: [])("session_summary", previous_start, previous_end)
                                if item.get("account") == self.account_key and item.get("symbol") == self.config.symbol
                                and item.get("config_fingerprint") == self.strategy_fingerprint]                   # v2.2.0 minor 2: legacy reports excluded
            go_sessions = sum(1 for item in sessions_in_week if item.get("go") == "GO")
            weekly_outcomes = self._outcomes_window(previous_start, previous_end, None)
            weekly.update({"symbol": self.config.symbol, "week": weekly_key, "scouts": scout_weekly, "setup_outcomes": weekly_outcomes,
                           "pattern_reliability_by_session": weekly_outcomes.get("reliability_by_session"),
                           "target_achievement_rate_plus_10": weekly_outcomes.get("plus_10_rate"), "fakeout_rate": weekly_outcomes.get("fakeout_rate"),
                           "scout_agreement_vs_pa": weekly_outcomes.get("scout_agreement_rate"),
                           "calibration_samples": {"pa_trades": self.strategy_samples, "scout_sessions": self.scouts.calibration_sessions,
                                                   "setups_resolved": weekly_outcomes.get("setups_resolved")},
                           "spread_cost_evidence": self._spread_evidence(),
                           "go": "GO" if go_sessions > 0 else "NO-GO", "go_basis": "sessions with a live GO during the week",
                           "go_sessions": go_sessions, "sessions_reported": len(sessions_in_week),
                           "report_status": "CATCH_UP" if self.startup_cycle else "COMPLETE",
                           "actionability": "HISTORICAL_SUMMARY",
                           "message": "Completed-week report; results are observed performance, not a profit forecast"})
            weekly["account"] = self.account_key; weekly["config_fingerprint"] = self.strategy_fingerprint
            once("weekly_report", f"{scope}:{weekly_key}", weekly)

        close_time = self.sessions.most_recent_friday_close(now)
        if close_time <= now:
            close_local = close_time.astimezone(ZoneInfo(self.config.sessions.new_york_timezone))
            next_monday = close_local.date() + timedelta(days=3)
            outlook_key = next_monday.isoformat()
            if exists("next_week_open_report", f"{scope}:{outlook_key}"):
                return
            levels = {level.kind: level.price for level in liquidity if level.kind in {"PYH", "PYL", "PMH", "PML", "PWH", "PWL"}}
            close_week_start_date = close_local.date() - timedelta(days=close_local.weekday())
            close_week_start = datetime.combine(close_week_start_date, datetime.min.time(), tzinfo=ZoneInfo(self.config.sessions.new_york_timezone)).astimezone(UTC)
            week_to_close = summarize(close_week_start, close_time + timedelta(seconds=1), None, *report_args)
            outlook = {"symbol": self.config.symbol, "week_open": next_monday.isoformat(), "reference_levels": levels,
                       **self._friday_close_go(close_time, getattr(snapshot, "go_status", "NO-GO")),
                       "report_status": "CATCH_UP" if self.startup_cycle else "COMPLETE",
                       "actionability": "WAIT_FOR_LIVE_GO",
                       "friday_close_time": close_time.isoformat(),
                       "completed_week_to_close": week_to_close,
                       "latest_available_pa_bias": snapshot.pa_side.value if snapshot.pa_side else "NEUTRAL",
                       "scout_leader": snapshot.scout.leader, "market_speed": snapshot.scout.market_speed,
                       "guidance": "WAIT for the new-week PA setup; scouts may confirm or contradict but cannot choose direction"}
            outlook["account"] = self.account_key; outlook["config_fingerprint"] = self.strategy_fingerprint
            once("next_week_open_report", f"{scope}:{outlook_key}", outlook)

    def _session_ending(self, now: datetime) -> bool:
        bounds = self._session_bounds(now)
        return bool(bounds) and (bounds[1] - now).total_seconds() <= self.config.poll_seconds * 2

    def scouts_positions_untracked(self) -> list[Any]:
        return [p for p in self.positions.scout_positions() if str(int(p.ticket)) not in self.positions.tracked]

    def _session_bounds(self, now: datetime):
        current = self.sessions.session_at(now)
        if current.value == "CLOSED":
            return None
        events = []
        for delta in (-1, 0, 1):
            events.extend(self.sessions.boundaries_for_utc_day((now + timedelta(days=delta)).date()))
        starts = [e.timestamp for e in events if e.kind == "START" and e.session == current and e.timestamp <= now]
        ends = [e.timestamp for e in events if e.timestamp > now and (e.kind == "CLOSE" or (e.kind == "START" and e.session != current))]
        if not starts or not ends:
            return None
        return max(starts), min(ends)

    def _session_start(self, now: datetime):
        current = self.sessions.session_at(now)
        if current.value == "CLOSED":
            return None
        events = []
        for delta in (-1, 0):
            events.extend(self.sessions.boundaries_for_utc_day((now + timedelta(days=delta)).date()))
        starts = [e.timestamp for e in events if e.kind == "START" and e.session == current and e.timestamp <= now]
        return max(starts) if starts else None

    @staticmethod
    def _plan_risk_currency(plan, result) -> float | None:
        """Rough currency risk of the filled volume at the plan's stop distance (reporting only)."""
        try:
            return round(abs(plan.entry - plan.stop_loss) * float(result.volume_filled or 0) * 100.0, 2) or None
        except (TypeError, ValueError):
            return None

    def _withheld_context(self, plan, selected, trigger, confluence, scout, snapshot) -> dict[str, Any]:
        """Everything an ORDER WITHHELD card needs to be self-explanatory (v3.3.0)."""
        return {"side": snapshot.decision.action.value if snapshot.decision else None,
                "session": snapshot.session.value,
                "entry": round(plan.entry, 2) if plan is not None else None,
                "stop_loss": round(plan.stop_loss, 2) if plan is not None else None,
                "take_profits": [round(x, 2) for x in plan.take_profits] if plan is not None else [],
                "actual_rr": plan.actual_rr if plan is not None else [],
                "zone_kind": getattr(selected, "kind", None), "trigger_reason": trigger.reason,
                "confluence": int(confluence), "scout_verdict": scout.verdict.value,
                "scout_strength": int(scout.strength), "scout_leader": scout.leader}

    def _report_scout_failure(self, session, result, attempt: int = 1, next_retry: datetime | None = None) -> None:
        """v3.3.0: one card-worthy event per failed scout placement, carrying everything needed to act on it —
        the detected broker offset and residual skew, the live spread, free vs required margin, and the next retry."""
        import re
        match = re.search(r"\b(10\d{3})\b", result.message or "")
        retcode = int(match.group(1)) if match else None
        self.scouts.failed_attempts[session.value] = attempt
        key = f"{session.value}:{result.message}"
        if self._last_scout_failure_key == key and attempt > 1:
            return                                                                    # same failure repeating on retries — one card
        self._last_scout_failure_key = key
        payload = {"session": session.value, "message": result.message, "retcode": retcode,
                   "retryable": result.retryable, "attempt": attempt,
                   "broker_utc_offset_hours": self.broker_clock.get("offset_hours"),
                   "broker_clock_source": self.broker_clock.get("source"),
                   "broker_clock_residual_seconds": self.broker_clock.get("residual_seconds"),
                   "max_clock_skew_seconds": self.config.safety.max_clock_skew_seconds,
                   "max_spread": self.config.risk.max_spread_price}
        try:
            payload["spread"] = round(self.client.get_tick(self.config.symbol).spread, 3)
        except Exception:
            payload["spread"] = None
        try:
            account = self.client.account_state()
            payload["margin_free"] = round(account.margin_free, 2)
            payload["margin_required"] = round(estimate_pair_margin(self.client, self.config), 2)
        except Exception:
            payload["margin_free"] = payload["margin_required"] = None
        if next_retry is not None:
            payload["next_retry_utc"] = next_retry.isoformat()
            payload["next_retry_local"] = next_retry.astimezone(ZoneInfo(self.config.display_timezone)).strftime("%H:%M:%S %Z")
        elif result.retryable:
            payload["next_retry_local"] = "automatic on the next cycle"
        else:
            payload["next_retry_local"] = "no — needs a config/account change"
        self.logger.event("scout_open_failed", payload)

    def _intermarket(self, closed: dict[str, pd.DataFrame], now: datetime) -> dict[str, Any]:
        """v3.1.0: load silver from the same terminal and assess correlation/SMT. Any failure → UNAVAILABLE, never blocks the cycle."""
        cfg = self.config.intermarket
        if not cfg.enabled:
            return dict(INTERMARKET_UNAVAILABLE)
        try:
            silver_full = load_native_history(self.client, self.config, account_key=self.account_key, symbol=cfg.symbol,
                                              history_bars={tf: cfg.bars[tf] for tf in cfg.timeframes})
            silver_closed = {tf: closed_bars(frame) for tf, frame in silver_full.items()}
            info = assess_intermarket(closed, silver_closed, cfg, now)
            self._intermarket_failures = 0
        except Exception as exc:
            self._intermarket_failures += 1
            info = dict(INTERMARKET_UNAVAILABLE); info.update({"symbol": cfg.symbol, "reason": f"{type(exc).__name__}: {exc}"})
            if not self._intermarket_reported:
                self._intermarket_reported = True
                self.logger.event("intermarket_unavailable", {"symbol": cfg.symbol, "error": str(exc)[:300],
                                                              "impact": "confluence runs without silver evidence"})
        if info.get("status") == "OK" and info.get("smt") != self.last_intermarket.get("smt") and info.get("smt") in {"BULLISH", "BEARISH"}:
            self.logger.event("smt_divergence", {"symbol": cfg.symbol, "smt": info["smt"], "timeframe": info.get("smt_timeframe"),
                                                 "detail": info.get("smt_detail"), "correlation": info.get("correlation"), "regime": info.get("regime")})
        self.last_intermarket = info
        return info

    @staticmethod
    def _price_action_direction(structures, sweeps, zones, patterns, m5=None, today_range=0.0, daily_atr=0.0,
                                price=0.0, atr=1.0, pd_info=None, vwap_info=None, intermarket=None) -> tuple[Side | None, int]:
        long_families = {"htf": 0, "structure": 0, "liquidity": 0, "location": 0, "momentum": 0, "candle": 0, "intermarket": 0}
        short_families = dict(long_families)
        weights = {"D1": 7, "H4": 7, "H1": 4, "M15": 2, "M5": 2}
        for timeframe, result in structures.items():
            if result.state == StructureState.BULLISH:
                long_families["htf"] += weights.get(timeframe, 1)
            elif result.state == StructureState.BEARISH:
                short_families["htf"] += weights.get(timeframe, 1)
            for event in result.events[-2:]:
                bullish = str(event["event"]).startswith("Bullish"); failed = event.get("status") == "failed"
                if failed:                                                   # item 2: a failed break is evidence for the other side
                    (short_families if bullish else long_families)["structure"] += 4
                    (long_families if bullish else short_families)["structure"] -= 3
                elif bullish:
                    long_families["structure"] += 5 + (2 if event.get("status") == "retested" else 0)
                else:
                    short_families["structure"] += 5 + (2 if event.get("status") == "retested" else 0)
        for event in [e for e in sweeps if e.active][-6:]:                   # item 34: age-limited
            target = long_families if event.direction == "BULLISH" else short_families
            target["liquidity"] += 4
        invalid_states = {"broken", "invalidated", "fully mitigated", "failed"}
        for zone in [z for z in zones[-30:] if z.status.lower() not in invalid_states]:
            target = long_families if zone.side == Side.LONG else short_families
            target["location"] += min(3, int(zone.score // 3) + 1)
        bullish_names = {"Bullish Engulfing", "Bullish Pin Bar", "Morning Star", "Three White Soldiers", "Strong displacement"}
        bearish_names = {"Bearish Engulfing", "Bearish Pin Bar", "Evening Star", "Three Black Crows"}
        names = {item.get("name") for item in patterns[-12:]}
        long_families["candle"] = min(5, 2 * len(names & bullish_names))
        short_families["candle"] = min(5, 2 * len(names & bearish_names))
        if m5 is not None and len(m5) >= 6 and pd.notna(m5.iloc[-1].atr) and float(m5.iloc[-1].atr) > 0:
            net = float(m5.close.iloc[-1] - m5.open.iloc[-5]) / float(m5.iloc[-1].atr)
            if net >= 0.4: long_families["momentum"] = min(10, int(5 * net))
            elif net <= -0.4: short_families["momentum"] = min(10, int(-5 * net))
        if "Strong displacement" in names and m5 is not None and len(m5) and float(m5.close.iloc[-1]) < float(m5.open.iloc[-1]):
            short_families["candle"] = min(5, short_families["candle"] + 2)
        if intermarket and intermarket.get("status") == "OK":                                                      # v3.1.0: silver evidence only
            long_families["intermarket"] = int(intermarket.get("long_points", 0)); short_families["intermarket"] = int(intermarket.get("short_points", 0))
        caps = {"htf": 20, "structure": 20, "liquidity": 20, "location": 15, "momentum": 10, "candle": 5, "intermarket": 8}
        long_score = sum(min(caps[key], max(0, value)) for key, value in long_families.items())
        short_score = sum(min(caps[key], max(0, value)) for key, value in short_families.items())
        # penalties: counter-trend vs H4/D1, exhausted daily range
        htf_bias = sum(1 if structures[t].state == StructureState.BULLISH else -1 if structures[t].state == StructureState.BEARISH else 0 for t in ("D1", "H4") if t in structures)
        if htf_bias < 0: long_score -= 10
        if htf_bias > 0: short_score -= 10
        if daily_atr > 0 and today_range / daily_atr >= 0.85: long_score -= 10; short_score -= 10
        # 17. nearby opposing level penalty: strong S/R within 1.5 ATR against the trade
        for zone in [z for z in zones if z.status.lower() not in invalid_states]:
            if zone.kind not in {"RESISTANCE", "SUPPORT"} or zone.score < 6: continue
            if zone.kind == "RESISTANCE" and 0 < zone.low - price <= 1.5 * atr: long_score -= 10; break
        for zone in [z for z in zones if z.status.lower() not in invalid_states]:
            if zone.kind not in {"RESISTANCE", "SUPPORT"} or zone.score < 6: continue
            if zone.kind == "SUPPORT" and 0 < price - zone.high <= 1.5 * atr: short_score -= 10; break
        # 18. premium/discount + VWAP as location/momentum context
        if pd_info and pd_info.get("pct") is not None:
            if pd_info["pct"] < 0.5: long_score += 5
            else: short_score += 5
        if vwap_info and vwap_info.get("relation") == "above": long_score += 3
        if vwap_info and vwap_info.get("relation") == "below": short_score += 3
        long_score, short_score = max(0, min(100, long_score)), max(0, min(100, short_score))
        if abs(long_score - short_score) < 8:
            return None, max(long_score, short_score)
        return (Side.LONG, long_score) if long_score > short_score else (Side.SHORT, short_score)


def execution_price_for(side, tick) -> float:
    return tick.ask if side == Side.LONG else tick.bid
