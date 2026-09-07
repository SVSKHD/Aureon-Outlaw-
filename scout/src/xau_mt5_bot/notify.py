"""Discord webhook messaging — decision changes, session/scout events, errors, periodic status. Never blocks trading."""
from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from typing import Any

from .models import AnalysisSnapshot
from .cards import detection_signature, detections_card, event_card, snapshot_dict, status_card


class Discord:
    TRADE_EVENTS: frozenset = frozenset({"startup", "startup_failed", "shutdown", "restart", "cycle_error", "integration_disabled",
                   "session_transition", "session_summary", "weekly_report", "next_week_open_report",
                   "scout_session_open", "scout_session_close", "scout_rollback", "scout_stale_pair_closed",
                   "order", "order_withheld", "pa_partial", "pa_breakeven", "pa_tp2_lock", "pa_trail", "pa_close", "trade_closed",
                   "pa_breakeven_retry", "pa_tp2_lock_retry",
                   "strong_scout_contradiction", "smt_divergence", "state_file_recovered", "scout_open_failed",
                   # v3.3.0: restart adoption and a changed broker clock offset are both rare and operationally important
                   "scout_adopted", "scout_leg_repaired", "broker_clock_offset"})                                    # v3.1.1: quiet default

    def __init__(self, webhook_env: str, min_interval: int = 300, scout_pair_min_interval: int = 60,
                 retry_count: int = 3, retry_backoff_seconds: float = 1.0,
                 status_mode: str = "events", event_level: str = "trade") -> None:
        self.status_mode, self.event_level = status_mode, event_level                         # v3.1.1
        self.url = os.environ.get(webhook_env, "") or os.environ.get("DISCORD_WEBHOOK_URL", "")     # v3.1.0: accept the common alias
        self.min_interval = min_interval
        self._last_status = 0.0
        self._last_key: str | None = None
        self._last_detection_sig: str | None = None; self._last_detection_push = 0.0                 # v3.2.0
        self._last_hour: int | None = None                                                           # v3.4.0
        self.scout_pair_min_interval = scout_pair_min_interval
        self._last_pair_event: dict[str, float] = {}
        self.retry_count = retry_count
        self.retry_backoff_seconds = retry_backoff_seconds
        self.last_error: str | None = None

    def enabled(self) -> bool:
        return bool(self.url)

    def send(self, text: str = "", embed: dict[str, Any] | None = None) -> bool:
        """Post plain text (chunked) or one embed card (v3.2.0)."""
        if not self.url:
            return False
        if embed is not None:
            bodies = [{"embeds": [embed]}]
        else:
            bodies = [{"content": text[i:i + 1900]} for i in range(0, len(text), 1900)] or [{"content": ""}]
        for body in bodies:
            delivered = False
            for attempt in range(self.retry_count + 1):
                try:
                    req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                                 headers={"Content-Type": "application/json", "User-Agent": "dacoit-bot"})
                    urllib.request.urlopen(req, timeout=10).read()
                    self.last_error = None; delivered = True; break
                except urllib.error.HTTPError as exc:
                    self.last_error = f"HTTP {exc.code}"
                    if exc.code != 429 or attempt >= self.retry_count:
                        break
                    raw_retry = exc.headers.get("Retry-After") or exc.headers.get("X-RateLimit-Reset-After")
                    try: retry_after = float(raw_retry) if raw_retry is not None else self.retry_backoff_seconds * (2 ** attempt)
                    except (TypeError, ValueError): retry_after = self.retry_backoff_seconds * (2 ** attempt)
                    time.sleep(min(retry_after, 30.0))
                except Exception as exc:
                    self.last_error = str(exc)
                    if attempt >= self.retry_count: break
                    time.sleep(min(self.retry_backoff_seconds * (2 ** attempt), 30.0))
            if not delivered:
                return False
        return True

    @staticmethod
    def format_snapshot(s: AnalysisSnapshot, tz: str) -> str:
        from zoneinfo import ZoneInfo
        t = s.timestamp.astimezone(ZoneInfo(tz)).strftime("%H:%M %Z")
        plan = s.trade_plan; sc = s.scout; zone = s.zones[0] if s.zones else None
        lines = [f"**[{s.go_status}] [{s.decision.action.value}]** {t} · {s.session.value} · {s.bid:.2f}/{s.ask:.2f} spread {s.spread:.2f} · data {s.freshness.value}",
                 f"PA {s.pa_side.value if s.pa_side else 'NEUTRAL'} · confluence {s.confluence}/100 · D1 {s.structures['D1'].state.value} H4 {s.structures['H4'].state.value} H1 {s.structures['H1'].state.value} M5 {s.structures['M5'].state.value}",
                 f"Scouts: leader {sc.leader} · BUY {sc.buy_pnl:+.2f} (MFE {sc.buy_mfe:+.2f}) · SELL {sc.sell_pnl:+.2f} (MFE {sc.sell_mfe:+.2f}) · {sc.verdict.value} {sc.strength}/10",
                 f"Market pace: {sc.market_speed} · {sc.guidance}",
                 f"Pace: range {sc.pace_range:.2f} / {sc.pace_window_minutes:.1f} min = {sc.velocity:.3f} price/min · displacement direction {sc.velocity_direction}",
                 f"MTF {s.analysis.get('multi_timeframe', {}).get('label', 'n/a')} · treatment {s.analysis.get('treatment', {}).get('treatment', 'n/a')} · reliability n={s.analysis.get('historical_pattern_reliability', {}).get('samples', 0)} +10 {s.analysis.get('historical_pattern_reliability', {}).get('plus_10_hit_rate')} fakeout {s.analysis.get('historical_pattern_reliability', {}).get('fakeout_rate')}",
                 f"${s.analysis.get('session_target', {}).get('session_target_price_move', 10):.0f} target: {s.analysis.get('session_target', {}).get('target_verdict', 'DISABLED')} · required {s.analysis.get('session_target', {}).get('required_price_move')} · {s.analysis.get('remaining_session_minutes', 0):.0f} min left · calibration {s.reporting.get('calibration_status', 'COLLECTING')}",
                 f"Research 1-lot scale (normalised, demo-only, not a permission): scout BUY {sc.buy_pnl_reference_lot:+.2f} · SELL {sc.sell_pnl_reference_lot:+.2f} · active PA {s.reporting.get('active_pa_pnl_reference_lot', 0):+.2f} · scouts are EVIDENCE ONLY, DO NOT MIRROR",
                 f"Scout emergency SL: {sc.emergency_sl_price_distance:.2f} price distance · estimated pair risk actual {sc.estimated_pair_risk_actual if sc.estimated_pair_risk_actual is not None else 'n/a'} · 1-lot translation {sc.estimated_pair_risk_reference_lot if sc.estimated_pair_risk_reference_lot is not None else 'n/a'}",
                 f"Silver: {s.analysis.get('intermarket', {}).get('regime', 'n/a')} r={s.analysis.get('intermarket', {}).get('correlation')} · SMT {s.analysis.get('intermarket', {}).get('smt', 'NONE')} · leading {s.analysis.get('intermarket', {}).get('silver_leading', 'NONE')} · +{s.analysis.get('intermarket', {}).get('long_points', 0)}L/+{s.analysis.get('intermarket', {}).get('short_points', 0)}S",
                 f"Entry: {f'{zone.low:.2f}–{zone.high:.2f} ({zone.kind})' if zone else 'none'} · {s.entry_state.value} · trigger {'CONFIRMED' if s.trigger.confirmed else 'waiting'} — {s.trigger.reason}"]
        if plan:
            lines.append(f"Plan: entry {plan.entry:.2f} · SL {plan.stop_loss:.2f} ({plan.sl_reason}) · TP {' / '.join(f'{v:.2f}' for v in plan.take_profits)} · RR {' / '.join(f'{v:.2f}' for v in plan.actual_rr)} · {plan.target_realism.value}")
        lines.append(f"Reason: {s.decision.reason}")
        return "\n".join(lines)

    def on_snapshot(self, s: AnalysisSnapshot, tz: str) -> None:
        """Send immediately when the decision/entry-state changes; otherwise at most one status per min_interval."""
        d = snapshot_dict(s); now = time.time()
        sig = detection_signature(d)                                                                # v3.2.0: detections card on new detection
        if self._last_detection_sig is not None and sig != self._last_detection_sig and now - self._last_detection_push >= 60 and self.status_mode != "off":
            if self.send(embed=detections_card(d, tz)):
                self._last_detection_push = now
        self._last_detection_sig = sig
        if self.status_mode in {"events", "off"}:
            return                                                     # v3.1.1: status only on request (!status)
        # v3.4.0: the decision card is pushed when the VERDICT or the blocking gate changes, not just the action.
        trace = ((d.get("analysis") or {}).get("decision_trace") or {}) if isinstance(d, dict) else {}
        key = (f"{s.decision.action.value}|{s.entry_state.value}|{s.pa_side}|{s.session.value}|{s.go_status}"
               f"|{trace.get('verdict')}|{trace.get('blocking_gate')}")
        hour = int(now // 3600)                                        # v3.4.0: `hourly` = one card an hour, plus every change
        due = (key != self._last_key
               or (self.status_mode == "hourly" and hour != self._last_hour)
               or (self.status_mode == "interval" and now - self._last_status >= self.min_interval))
        if due:
            if self.send(embed=status_card(d, tz)):
                self._last_key, self._last_status, self._last_hour = key, now, hour

    ELIGIBLE_EVENTS: frozenset = frozenset({"session_transition", "session_summary", "weekly_report", "next_week_open_report", "scout_session_stats",
                   "scout_session_open", "scout_session_close", "scout_rollback", "scout_repair", "scout_leg_repaired", "scout_adopted",
                   "scout_stale_pair_closed", "scout_bootstrap", "scout_strength_fallback",
                   "cycle_error", "clock_warning", "clock_check", "order", "restart", "startup", "shutdown", "integration_disabled",
                   "pa_partial", "pa_breakeven", "pa_breakeven_retry", "pa_tp2_lock", "pa_tp2_lock_retry", "pa_trail",
                   "pa_close", "trade_closed", "trigger_consumed",
                   "cycle_slow", "pattern_scan_failed", "pattern_scan_executor", "pace_calibration_reset",     # v2.0.0 item 18
                   "order_withheld", "state_file_recovered", "research_translation", "session_transition_delayed", "session_summary_recovered",
                   "pending_report_dropped", "startup_failed", "mt5_validated", "setup_outcome_resolved", "strong_scout_contradiction",
                   "scout_pending_stats_dropped", "shutdown_discord_pending",
                   "smt_divergence", "intermarket_unavailable", "scout_open_failed",
                   "broker_clock_offset", "broker_clock_error", "scout_leg_rolled_back"})     # v3.3.0                                                    # v3.1.0

    def is_eligible(self, kind: str) -> bool:
        if kind not in self.ELIGIBLE_EVENTS:
            return False
        return kind in self.TRADE_EVENTS if getattr(self, "event_level", "trade") == "trade" else True

    def event(self, kind: str, payload: dict[str, Any]) -> bool:
        """Returns True when a Discord message was delivered for this event (v2.0.0 item 15)."""
        if not self.is_eligible(kind):
            return False
        if kind == "session_transition" and payload.get("kind") == "START" and not payload.get("success", True):
            return False                                                     # v3.2.0: the SCOUTS NOT PLACED card already covers it
        pair_events = {"scout_session_open", "scout_session_close", "scout_rollback", "scout_repair", "scout_leg_repaired", "scout_adopted", "scout_stale_pair_closed", "scout_open_failed"}
        if kind in pair_events:
            key = f"{kind}:{payload.get('session', '')}"; now = time.time()
            if now - self._last_pair_event.get(key, 0) < self.scout_pair_min_interval:
                return False
            self._last_pair_event[key] = now
            return self.send(embed=event_card(kind, payload))
        elif kind in {"session_transition", "order", "order_withheld", "pa_partial", "pa_breakeven", "pa_tp2_lock", "pa_trail", "pa_close",
                      "trade_closed", "smt_divergence", "cycle_error", "startup_failed", "session_summary",
                      "pa_breakeven_retry", "pa_tp2_lock_retry"}:
            return self.send(embed=event_card(kind, payload))                                        # v3.2.0 cards
        elif kind == "session_summary_legacy":
            sc = payload.get("scout", {})
            return self.send(f"**SESSION SUMMARY — {payload.get('session')} — {payload.get('go', 'NO-GO')} — {payload.get('report_status', 'COMPLETE')}**\nPA trades {payload.get('pa_trades', 0)} · W/L {payload.get('wins', 0)}/{payload.get('losses', 0)} · net {payload.get('net_pnl', 0):+.2f}\n"
                      f"Scout leader {sc.get('leader', 'NONE')} · speed {sc.get('market_speed', 'UNKNOWN')} · {sc.get('guidance', '')}\n"
                      f"1-lot view {payload.get('net_pnl_reference_lot', 0):+.2f} · pending {payload.get('pending_pa_count', 0)} · daily target progress {payload.get('daily_target_progress_pct', 0):.1f}%")
        elif kind == "weekly_report":
            scouts = payload.get("scouts", {})
            accuracy = scouts.get("leader_accuracy_pct")
            accuracy_text = f"{accuracy:.1f}%" if isinstance(accuracy, (int, float)) else "n/a"
            return self.send(f"**WEEKLY REPORT — {payload.get('week')} — {payload.get('go', 'NO-GO')} — {payload.get('report_status', 'COMPLETE')}**\nPA trades {payload.get('pa_trades', 0)} · wins {payload.get('wins', 0)} · losses {payload.get('losses', 0)} · net {payload.get('net_pnl', 0):+.2f}\n"
                      f"1-lot PA view {payload.get('net_pnl_reference_lot', 0):+.2f} · scout net at 1 lot {scouts.get('scout_net_pnl_reference_lot', 0):+.2f} · monthly-target progress {payload.get('monthly_target_progress_pct', 0):.1f}% · scout leader accuracy {accuracy_text}\n"
                      "Scouts are evidence only — do not mirror the pair.\n"
                      "Observed demo results only — not a profit forecast.")
        elif kind == "next_week_open_report":
            levels = payload.get("reference_levels", {})
            level_text = " · ".join(f"{k} {v:.2f}" for k, v in sorted(levels.items())) or "levels unavailable"
            week = payload.get("completed_week_to_close", {})
            return self.send(f"**NEXT-WEEK OPEN — {payload.get('week_open')} — {payload.get('go', 'NO-GO')} — {payload.get('report_status', 'COMPLETE')}**\n{level_text}\nLatest available PA bias {payload.get('latest_available_pa_bias')} · scout {payload.get('scout_leader')} · pace {payload.get('market_speed')}\n"
                      f"Friday-close 1-lot view {week.get('net_pnl_reference_lot', 0):+.2f} · monthly progress {week.get('monthly_target_progress_pct', 0):.1f}%\n{payload.get('guidance')}")
        elif kind == "research_translation":
            warn = payload.get("warnings") or []
            return self.send(f"**RESEARCH SCALE** (normalised, not a permission) · {payload.get('reference_lot')} lot · ${payload.get('research_daily_price_move')} move ≈ ${payload.get('research_daily_move_pnl_usd') or 0:.0f}"
                             + ("\n" + "\n".join(f"WARNING: {w}" for w in warn) if warn else ""))
        elif kind == "setup_outcome_resolved":
            return self.send(f"**SETUP RESOLVED** `{payload.get('setup_id')}` · {payload.get('continuation_classification')} / {payload.get('fakeout_classification')} · MFE {payload.get('mfe')} MAE {payload.get('mae')} · +3 {payload.get('reached_plus_3')} +5 {payload.get('reached_plus_5')} +10 {payload.get('reached_plus_10')} · trade {payload.get('final_trade_result')}")
        elif kind == "strong_scout_contradiction":
            return self.send(f"**STRONG SCOUT CONTRADICTION** · PA {payload.get('pa_side')} vs leader {payload.get('leader')} strength {payload.get('strength')} · setup vetoed (scouts are evidence only)")
        elif kind == "session_summary_recovered":
            return self.send(f"**RECOVERY** · {payload.get('count')} session summary(ies) recovered after restart")
        elif kind == "mt5_validated":
            offset = payload.get("broker_utc_offset_hours")
            clock_text = (f" · broker clock UTC{offset:+g} ({payload.get('broker_clock_source', 'auto')}), residual skew "
                          f"{payload.get('broker_clock_residual_seconds')}s") if offset is not None else ""
            return self.send(f"**MT5 ATTACHED** · account {payload.get('login')}@{payload.get('server')} · demo={payload.get('is_demo')} hedging={payload.get('is_hedging')} · algo={payload.get('algo_trading')} · tick age {payload.get('tick_age_seconds')}s{clock_text}")
        else:
            return self.send(embed=event_card(kind, payload))
