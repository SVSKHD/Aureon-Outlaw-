from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from .config import BotConfig
from .models import ScoutSnapshot, ScoutVerdict, SessionName, Side
from .execution import account_is_safe, normalize_price, normalize_volume, send_with_retry
from .mt5_client import OrderResult, TradingClient, broker_epoch_to_utc
from .sessions import SessionBoundary


@dataclass(slots=True)
class ScoutTransitionResult:
    success: bool
    message: str
    retryable: bool = True


class ScoutManager:
    def __init__(self, client: TradingClient, config: BotConfig, audit: Callable[[str, dict], None] | None = None,
                 audit_once: Callable[[str, str, dict], bool] | None = None) -> None:
        self.client = client
        self.config = config
        self.audit = audit or (lambda _event, _payload: None)
        self.audit_once = audit_once or (lambda event, _key, payload: (self.audit(event, payload), True)[1])
        self.current_session = SessionName.CLOSED
        self.session_open_price: float | None = None
        self.session_open_time: datetime | None = None
        self.extrema: dict[int, tuple[float, float]] = {}
        self.orders_ok: Callable[[], tuple[bool, str]] = lambda: (True, "ok")   # engine injects clock/market/account gate
        self.session_bounds: Callable[[datetime], tuple[datetime, datetime] | None] = lambda now: None   # engine injects DST-aware start/end
        self.pending_closures: list[dict] = []     # tickets whose close was not confirmed — retried every cycle (items 7, 8)
        self.state_path: str | None = None          # engine sets an account-specific path; extrema persisted here (item 36)
        self.last_closed_summary: dict[str, Any] | None = None
        self.price_samples: list[tuple[str, float]] = []
        self._strength_fallback_reported = False
        self.calibration_sessions = 0
        self.calibration_source = "none"                  # none | file | reset | sqlite (v2.0.0)
        self.fingerprint: str = ""                        # set by the engine before restore()
        self.account_key: str = ""
        self._pending_stats: dict | None = None            # session summary awaiting confirmed closure (v2.0.0 item 4)

    def _send(self, side: str, lot: float, magic: int, comment: str) -> OrderResult:
        """Every scout order goes through normalisation, order_check, retry and fill verification (item 5)."""
        sl = 0.0
        if self.config.risk.emergency_scout_sl_price > 0:                                       # item 15
            tick = self.client.get_tick(self.config.symbol); info = self.client.symbol_info(self.config.symbol)
            sl = normalize_price(tick.ask - self.config.risk.emergency_scout_sl_price if side == "BUY" else tick.bid + self.config.risk.emergency_scout_sl_price, info)
        result = send_with_retry(self.client, self.config, self.config.symbol, side, lot, magic, comment, sl, 0.0, self.audit)
        self.audit("scout_order", {"side": side, "lot": lot, "magic": magic, "comment": comment, "success": result.success, "retcode": result.retcode, "message": result.message})   # item 37
        return result

    def _close(self, ticket: int, magic: int, why: str) -> bool:
        """Close and confirm; unconfirmed closes are queued and retried (items 7, 8)."""
        result = self.client.close_position(int(ticket), magic)
        still = [p for p in self.client.positions(self.config.symbol, magic) if int(p.ticket) == int(ticket)]
        ok = result.success and not still
        self.audit("scout_close", {"ticket": int(ticket), "magic": magic, "why": why, "success": ok, "message": result.message})
        if not ok:
            if not any(x["ticket"] == int(ticket) and x["magic"] == magic for x in self.pending_closures):
                self.pending_closures.append({"ticket": int(ticket), "magic": magic, "why": why, "attempts": 1})
        self._persist()
        return ok

    def retry_pending_closures(self) -> None:
        for item in list(self.pending_closures):
            still = [p for p in self.client.positions(self.config.symbol, item["magic"]) if int(p.ticket) == item["ticket"]]
            if not still:
                self.pending_closures.remove(item); continue
            result = self.client.close_position(item["ticket"], item["magic"])
            item["attempts"] += 1
            self.audit("scout_close_retry", {**item, "success": result.success, "message": result.message})
            confirmed_absent = not [p for p in self.client.positions(self.config.symbol, item["magic"]) if int(p.ticket) == item["ticket"]]
            if confirmed_absent:
                self.pending_closures.remove(item)
        self._persist()

    def _persist(self) -> None:
        if not self.state_path: return
        from .statefile import atomic_write_json
        atomic_write_json(self.state_path, {"extrema": {str(k): v for k, v in self.extrema.items()}, "pending_closures": self.pending_closures,
                                            "session": self.current_session.value, "open_price": self.session_open_price,
                                            "open_time": self.session_open_time.isoformat() if self.session_open_time else None,
                                            "price_samples": self.price_samples[-1000:],
                                            "calibration_sessions": int(self.calibration_sessions),
                                            "pending_stats": self._pending_stats,
                                            "calibration_scope": {"account": self.account_key, "symbol": self.config.symbol, "fingerprint": self.fingerprint}})

    def restore(self) -> None:
        if not self.state_path: return
        from pathlib import Path
        from .statefile import load_json_state
        data, source = load_json_state(self.state_path)
        if source == "backup" or (source == "none" and Path(self.state_path).exists()):
            self.audit("state_file_recovered", {"file": self.state_path, "source": source})
        if source == "none": return
        pending = data.get("pending_stats")
        if isinstance(pending, dict):
            if (pending.get("account") == self.account_key and pending.get("symbol") == self.config.symbol
                    and pending.get("config_fingerprint") == self.fingerprint):
                self._pending_stats = pending                                  # v2.1.0 item 9
            else:                                                               # v2.2.0 item 3: other scope → dropped with audit
                self.audit("scout_pending_stats_dropped", {"reason": "scope mismatch", "pending_scope": {k: pending.get(k) for k in ("account", "symbol", "config_fingerprint")},
                                                           "current_scope": {"account": self.account_key, "symbol": self.config.symbol, "config_fingerprint": self.fingerprint}})
        self.extrema = {int(k): tuple(v) for k, v in data.get("extrema", {}).items()}
        self.pending_closures = data.get("pending_closures", [])
        self.price_samples = [(str(t), float(p)) for t, p in data.get("price_samples", [])]
        if self.config.scout_analysis.reset_pace_calibration:                       # v1.9.0: explicit reset wins over both stores
            self.calibration_sessions = 0; self.calibration_source = "reset"
            self.audit("pace_calibration_reset", {"reason": "scout_analysis.reset_pace_calibration=true"})
        elif "calibration_sessions" in data:                                          # state file is authoritative (per account+symbol)
            scope = data.get("calibration_scope") or {}
            same_scope = (scope.get("account") == self.account_key and scope.get("symbol") == self.config.symbol
                          and scope.get("fingerprint") == self.fingerprint)                 # v2.1.0 item 11: legacy/unscoped file never matches
            if same_scope:
                self.calibration_sessions = int(data["calibration_sessions"]); self.calibration_source = "file"
            else:                                                                     # v2.0.0 item 5: parameter/account change restarts calibration
                self.calibration_sessions = 0; self.calibration_source = "reset"
                self.audit("pace_calibration_reset", {"reason": "calibration scope changed", "previous_scope": scope,
                                                      "current_scope": {"account": self.account_key, "symbol": self.config.symbol, "fingerprint": self.fingerprint}})
        raw_session = data.get("session", SessionName.CLOSED.value)
        try: self.current_session = SessionName(raw_session)
        except ValueError: self.current_session = SessionName.CLOSED
        self.session_open_price = float(data["open_price"]) if data.get("open_price") is not None else None
        self.session_open_time = datetime.fromisoformat(data["open_time"]) if data.get("open_time") else None

    def magic(self, session: SessionName) -> int:
        return {
            SessionName.ASIA: self.config.magic.scout_asia,
            SessionName.LONDON: self.config.magic.scout_london,
            SessionName.NEW_YORK: self.config.magic.scout_new_york,
        }[session]

    @property
    def scout_magics(self) -> set[int]:
        return {self.config.magic.scout_asia, self.config.magic.scout_london, self.config.magic.scout_new_york}

    def adopt_existing(self, current: SessionName, now: datetime) -> ScoutTransitionResult:
        """Restart recovery: adopt the pair that belongs to the current session, close pairs left from ended sessions."""
        adopted = False
        for session in (SessionName.ASIA, SessionName.LONDON, SessionName.NEW_YORK):
            positions = self.client.positions(self.config.symbol, self.magic(session))
            if not positions:
                continue
            if session == current:
                self.current_session = session
                self.session_open_price = sum(float(p.price_open) for p in positions) / len(positions)
                t = getattr(positions[0], "time", None)
                self.session_open_time = broker_epoch_to_utc(self.client, int(t)) if isinstance(t, (int, float)) else now.astimezone(UTC)   # v3.3.0
                adopted = True
                self.audit("scout_adopted", {"session": session.value, "tickets": [int(p.ticket) for p in positions], "legs": len(positions),
                                             "mfe_mae_restored": [int(p.ticket) in self.extrema for p in positions]})
            else:
                for p in positions:
                    self._close(int(p.ticket), self.magic(session), "stale pair after restart")
                self.audit("scout_stale_pair_closed", {"session": session.value, "queued": len(self.pending_closures)})
        return ScoutTransitionResult(True, "Adopted existing scouts" if adopted else "No existing scouts for current session")

    def repair_leg(self, now: datetime) -> ScoutTransitionResult | None:
        """One leg missing (SL/manual close): re-open it early in the session, otherwise close the survivor so the pair stays paired."""
        if self.current_session == SessionName.CLOSED or not self.config.safety.allow_scout_orders or not self.config.safety.repair_missing_scout_leg:
            return None
        magic = self.magic(self.current_session)
        positions = self.client.positions(self.config.symbol, magic)
        if len(positions) != 1:
            return None
        surviving = positions[0]; missing = "SELL" if int(getattr(surviving, "type", 0)) == 0 else "BUY"
        bounds = self.session_bounds(now)                                                          # item 14: DST-aware start/end
        if bounds:
            start, end = bounds; fraction = (now.astimezone(UTC) - start).total_seconds() / max((end - start).total_seconds(), 1)
        else:
            fraction = (now.astimezone(UTC) - (self.session_open_time or now.astimezone(UTC))).total_seconds() / (8 * 3600)
        ok, why = self.orders_ok()
        safe, safe_why = account_is_safe(self.client, self.config, True, float(surviving.volume))     # item 6
        if fraction <= self.config.safety.scout_repair_max_session_fraction and ok and safe:
            r = self._send(missing, float(surviving.volume), magic, f"SCOUT_{self.current_session.value}_{missing}_R")
            repaired = self.client.positions(self.config.symbol, magic)
            if r.success and (len(repaired) != 2 or abs(float(repaired[0].volume) - float(repaired[1].volume)) > 1e-9):
                for p in repaired:
                    self._close(int(p.ticket), magic, "rollback: repair volume imbalance")
                return ScoutTransitionResult(False, "Repair created unequal scout volumes; pair closed")
            self.audit("scout_leg_repaired", {"session": self.current_session.value, "missing": missing, "success": r.success, "message": r.message})
            return ScoutTransitionResult(r.success, f"Re-opened missing {missing} leg" if r.success else f"Repair failed: {r.message}")
        ok2 = self._close(int(surviving.ticket), magic, "orphan: " + ("late in session" if ok and safe else (why if not ok else safe_why)))
        return ScoutTransitionResult(ok2, "Closed orphan scout leg" if ok2 else "Orphan close queued for retry")

    def handle_boundary(self, boundary: SessionBoundary) -> ScoutTransitionResult:
        if boundary.kind == "CLOSE":
            result = self.close_session(boundary.session)
            if result.success:
                self.current_session = SessionName.CLOSED
            return result
        if self.current_session != SessionName.CLOSED:
            close_result = self.close_session(self.current_session)
            if not close_result.success:
                return close_result
            self.current_session = SessionName.CLOSED
        return self.open_session(boundary.session, boundary.timestamp)

    def open_session(self, session: SessionName, timestamp: datetime) -> ScoutTransitionResult:
        if not self.config.safety.allow_scout_orders:
            self.current_session = session
            return ScoutTransitionResult(True, "Scout orders disabled by configuration")
        account = self.client.account_state()
        if self.config.safety.require_hedging_for_scouts and not account.is_hedging:
            self.audit("scout_account_unsupported", {"session": session.value, "reason": "netting account cannot hold opposing legs"})
            return ScoutTransitionResult(False, "SCOUTS DISABLED — ACCOUNT IS NETTING MODE", retryable=False)
        magic = self.magic(session)
        if self.client.positions(self.config.symbol, magic):
            return ScoutTransitionResult(False, f"Existing {session.value} scout positions prevent duplicate pair")
        if not account.is_demo:
            return ScoutTransitionResult(False, "Live account not authorized", retryable=False)
        ok, why = self.orders_ok()
        if not ok:
            return ScoutTransitionResult(False, f"Scout orders blocked: {why}")
        lot = normalize_volume(self.config.risk.scout_lot, self.client.symbol_info(self.config.symbol))
        safe, safe_why = account_is_safe(self.client, self.config, True, 2 * lot)                   # item 6: both legs
        if not safe:
            return ScoutTransitionResult(False, f"Scout pair blocked: {safe_why}")
        buy = self._send("BUY", lot, magic, f"SCOUT_{session.value}_BUY")
        if not buy.success:
            return ScoutTransitionResult(False, f"BUY scout failed: {buy.retcode} {buy.message}")
        sell = self._send("SELL", lot, magic, f"SCOUT_{session.value}_SELL")
        if not sell.success:
            for p in self.client.positions(self.config.symbol, magic):
                self._close(int(p.ticket), magic, "rollback: SELL leg failed")
            self.audit("scout_rollback", {"session": session.value, "reason": sell.message, "pending": len(self.pending_closures)})
            return ScoutTransitionResult(False, f"SELL scout failed; BUY rolled back: {sell.retcode} {sell.message}")
        positions = self.client.positions(self.config.symbol, magic)
        if len(positions) != 2:
            for position in positions:
                self._close(int(position.ticket), magic, "rollback: pair verification failed")
            return ScoutTransitionResult(False, "Scout pair verification failed and was rolled back")
        buy_positions = [p for p in positions if int(getattr(p, "type", 1)) == 0]
        sell_positions = [p for p in positions if int(getattr(p, "type", 1)) == 1]
        equal = len(buy_positions) == len(sell_positions) == 1 and abs(float(buy_positions[0].volume) - float(sell_positions[0].volume)) < 1e-9
        if not equal:
            for position in positions:
                self._close(int(position.ticket), magic, "rollback: unequal scout volume")
            return ScoutTransitionResult(False, "Scout legs were not equal volume; pair rolled back")
        tick = self.client.get_tick(self.config.symbol)
        self.current_session = session
        self.session_open_price = (tick.bid + tick.ask) / 2
        self.session_open_time = timestamp.astimezone(UTC)
        self.price_samples = []
        self.audit("scout_session_open", {"session": session.value, "magic": magic, "lot": lot, "open_price": round(self.session_open_price, 2)})
        return ScoutTransitionResult(True, f"Opened paired {session.value} scouts")

    def close_session(self, session: SessionName) -> ScoutTransitionResult:
        if session == SessionName.CLOSED or not self.config.safety.allow_scout_orders:
            return ScoutTransitionResult(True, "No enabled scouts to close")
        magic = self.magic(session)
        positions = self.client.positions(self.config.symbol, magic)
        if positions:
            final = self.snapshot(None)
            summary = {
                    "session": session.value,
                    "open_time": self.session_open_time.isoformat() if self.session_open_time else None,
                    "close_time": self.client.get_tick(self.config.symbol).time.isoformat(),
                    "buy_pnl": final.buy_pnl,
                    "sell_pnl": final.sell_pnl,
                    "buy_mfe": final.buy_mfe,
                    "buy_mae": final.buy_mae,
                    "sell_mfe": final.sell_mfe,
                    "sell_mae": final.sell_mae,
                    "displacement": final.displacement,
                    "velocity": final.velocity,
                    "velocity_direction": final.velocity_direction,
                    "pace_range": final.pace_range,
                    "leader": final.leader,
                    "verdict": final.verdict.value, "strength": final.strength,
                    "market_speed": final.market_speed, "guidance": final.guidance,
                    "buy_pnl_reference_lot": final.buy_pnl_reference_lot,
                    "sell_pnl_reference_lot": final.sell_pnl_reference_lot,
                    "strength_source": final.strength_source,
                    "calibration_sessions": final.calibration_sessions,
                    "pace_calibrated": final.pace_calibrated,
                    "emergency_sl_price_distance": final.emergency_sl_price_distance,
                    "estimated_pair_risk_actual": final.estimated_pair_risk_actual,
                    "estimated_pair_risk_reference_lot": final.estimated_pair_risk_reference_lot,
                }
            summary["account"] = self.account_key; summary["symbol"] = self.config.symbol; summary["config_fingerprint"] = self.fingerprint
            summary["session_id"] = f"{session.value}@{(self.session_open_time or datetime.now(UTC)).isoformat()}"   # session instance
            summary["stats_key"] = f"{self.account_key}:{self.config.symbol}:{self.fingerprint}:{summary['session_id']}"   # v3.0.0 §5.2: scoped idempotency identity
            self.last_closed_summary = summary
            if self._pending_stats is None or self._pending_stats.get("session") != session.value or len(positions) == 2:
                self._pending_stats = summary                               # v2.1.0 item 9: a one-leg retry never overwrites the pair summary
                self._persist()
        results = [self._close(int(position.ticket), magic, f"session {session.value} close") for position in positions]
        all_ok = all(results)
        if not all_ok or self.client.positions(self.config.symbol, magic):
            return ScoutTransitionResult(False, f"Could not confirm both {session.value} scouts closed — queued for retry")
        if self._pending_stats is not None:
            stats = self._pending_stats
            self.audit_once("scout_session_stats", stats.get("stats_key") or f"{self.account_key}:{self.config.symbol}:{self.fingerprint}:{stats['session_id']}", stats)   # §5.2 scoped key; raises → pending kept
            self._pending_stats = None; self.calibration_sessions += 1
            self._persist()
        self.audit("scout_session_close", {"session": session.value, "magic": magic})
        self.extrema.clear()
        self.price_samples = []
        self._persist()
        return ScoutTransitionResult(True, f"Closed {session.value} scout pair")

    def snapshot(self, pa_side: Side | None) -> ScoutSnapshot:
        session = self.current_session
        result = ScoutSnapshot(session)
        if session == SessionName.CLOSED or not self.config.safety.allow_scout_orders:
            return result
        magic = self.magic(session)
        positions = self.client.positions(self.config.symbol, magic)
        for position in positions:
            is_buy = int(getattr(position, "type", 1)) == 0
            ticket = int(position.ticket)
            pnl = float(position.profit)
            best, worst = self.extrema.get(ticket, (pnl, pnl))
            best, worst = max(best, pnl), min(worst, pnl)
            self.extrema[ticket] = (best, worst)
            if is_buy:
                result.buy_ticket, result.buy_entry, result.buy_pnl = ticket, float(position.price_open), pnl
                result.buy_mfe, result.buy_mae = best, worst
            else:
                result.sell_ticket, result.sell_entry, result.sell_pnl = ticket, float(position.price_open), pnl
                result.sell_mfe, result.sell_mae = best, worst
        self._persist()
        tick = self.client.get_tick(self.config.symbol)
        middle = (tick.bid + tick.ask) / 2
        result.displacement = middle - (self.session_open_price or middle)
        session_elapsed = max(0.0, (tick.time - (self.session_open_time or tick.time)).total_seconds() / 60)
        self.price_samples.append((tick.time.isoformat(), middle))
        cutoff = tick.time - timedelta(minutes=self.config.scout_analysis.velocity_window_minutes)
        samples = [(datetime.fromisoformat(t), p) for t, p in self.price_samples if datetime.fromisoformat(t) >= cutoff]
        self.price_samples = [(t.isoformat(), p) for t, p in samples]
        observed_minutes = max(0.0, (samples[-1][0] - samples[0][0]).total_seconds() / 60) if len(samples) > 1 else 0.0
        sample_minutes = max(1.0, observed_minutes)
        result.pace_range = max((p for _, p in samples), default=middle) - min((p for _, p in samples), default=middle)
        result.pace_window_minutes = min(float(self.config.scout_analysis.velocity_window_minutes), observed_minutes)
        rolling_pace = result.pace_range / sample_minutes
        # Velocity is deliberately unsigned rolling activity. Direction belongs to displacement.
        result.velocity = rolling_pace
        result.velocity_direction = "UP" if result.displacement > 0 else "DOWN" if result.displacement < 0 else "FLAT"
        difference = result.buy_pnl - result.sell_pnl
        result.leader = "BUY" if difference > 0 else "SELL" if difference < 0 else "NONE"
        calc = getattr(self.client, "calc_profit", None)
        one_price_divergence = 0.0
        broker_calc_ok = False
        if calc is not None:
            try:
                one_side = abs(float(calc(self.config.symbol, "LONG", self.config.risk.scout_lot, middle, middle + 1.0) or 0))
                one_price_divergence = 2 * one_side
                if one_price_divergence > 0:
                    result.strength_source = "BROKER_CALC"
                    broker_calc_ok = True
            except Exception:
                one_price_divergence = 0.0
        if one_price_divergence <= 0:
            info = self.client.symbol_info(self.config.symbol)
            tick_size = float(getattr(info, "trade_tick_size", 0) or 0)
            tick_value = float(getattr(info, "trade_tick_value", 0) or 0)
            if tick_size > 0 and tick_value > 0:
                one_price_divergence = 2 * self.config.risk.scout_lot * tick_value / tick_size
                result.strength_source = "SYMBOL_TICK_VALUE"
        if one_price_divergence <= 0:
            per_lot = self.config.scout_analysis.fallback_usd_per_price_per_lot_by_symbol.get(self.config.symbol)
            if per_lot:
                one_price_divergence = 2 * self.config.risk.scout_lot * per_lot
                result.strength_source = "CONFIG_FALLBACK"
            else:
                result.strength_source = "UNAVAILABLE"
        if not broker_calc_ok and not self._strength_fallback_reported:
            self.audit("scout_strength_fallback", {"symbol": self.config.symbol, "source": result.strength_source,
                                                    "configured_usd_per_price_per_lot": self.config.scout_analysis.fallback_usd_per_price_per_lot_by_symbol.get(self.config.symbol),
                                                    "reason": "order_calc_profit unavailable or returned None/zero"})
            self._strength_fallback_reported = True
        price_equivalent = abs(difference) / one_price_divergence if one_price_divergence > 0 else 0.0
        result.strength = min(10, int(round(price_equivalent / self.config.scout_analysis.strength_price_step)))
        speed = result.velocity
        result.calibration_sessions = int(self.calibration_sessions)
        result.pace_calibrated = result.calibration_sessions >= self.config.scout_analysis.min_calibration_sessions
        if session_elapsed < self.config.scout_analysis.session_open_grace_minutes or observed_minutes < self.config.scout_analysis.pace_min_observation_minutes:
            result.market_speed = "WARMUP"
            result.guidance = f"SESSION WARM-UP — observe scouts for {self.config.scout_analysis.session_open_grace_minutes:.0f} minutes; slow hold is not active"
        elif speed < self.config.scout_analysis.slow_velocity_price_per_min:
            result.market_speed = "SLOW"
            result.guidance = "HOLD — wait for the next session unless velocity improves"
        elif speed >= self.config.scout_analysis.fast_velocity_price_per_min:
            result.market_speed = "FAST"
            result.guidance = "FAST MARKET — require normal PA confirmation; do not chase entry"
        else:
            result.market_speed = "NORMAL"
            result.guidance = "NORMAL MARKET — follow the confirmed PA plan"
        reference = self.config.reporting.research_reference_lot / max(self.config.risk.scout_lot, 1e-9)
        result.buy_pnl_reference_lot = result.buy_pnl * reference
        result.sell_pnl_reference_lot = result.sell_pnl * reference
        result.emergency_sl_price_distance = self.config.risk.emergency_scout_sl_price
        if self.config.risk.emergency_scout_sl_price > 0:
            risks = []
            for side in ("LONG", "SHORT"):
                try:
                    close = middle - self.config.risk.emergency_scout_sl_price if side == "LONG" else middle + self.config.risk.emergency_scout_sl_price
                    value = calc(self.config.symbol, side, self.config.risk.scout_lot, middle, close) if calc else None
                    if value is not None: risks.append(abs(float(value)))
                except Exception: pass
            if len(risks) == 2:
                result.estimated_pair_risk_actual = sum(risks)
                result.estimated_pair_risk_reference_lot = sum(risks) * reference
        if pa_side is None or result.strength_source == "UNAVAILABLE" or result.strength < self.config.scout_analysis.min_verdict_strength:
            result.verdict = ScoutVerdict.NEUTRAL
        else:
            agrees = (pa_side == Side.LONG and difference > 0) or (pa_side == Side.SHORT and difference < 0)
            result.verdict = ScoutVerdict.CONFIRMS if agrees else ScoutVerdict.CONTRADICTS
        self._persist()
        return result
