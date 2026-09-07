"""Discord embed cards (v3.2.0). One builder per card, all working on the snapshot JSON dict so the webhook path
(dataclass → dict) and the command bot (SQLite payload → dict) render identical cards."""
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .logger import json_default

GREEN, RED, GREY, AMBER, BLUE, PURPLE = 0x1D9E75, 0xD85A30, 0x8A8A8A, 0xEF9F27, 0x3B82F6, 0x7F77DD

RETCODE_HINTS = {
    10027: "AutoTrading is OFF in the terminal — click the green 'Algo Trading' button in MT5",
    10030: "Broker rejects the filling mode — set execution.filling_mode to the one the symbol allows (FOK/IOC/RETURN)",
    10018: "Market closed at the broker — wait for the symbol's session",
    10019: "Not enough money/margin for 2 × scout lot",
    10014: "Invalid volume — scout_lot below the symbol minimum or wrong step",
    10016: "Invalid stops — emergency SL distance inside the broker's stop level",
    10013: "Invalid request — check symbol name and account type",
    10004: "Requote — retried; spread spike at open",
    10006: "Request rejected by dealer — retried",
    10031: "No connection to the trade server — check MT5 login/network",
    10021: "No prices — feed gap at open, retried",
}


# v3.3.0: the message text, not just the retcode, decides the Fix line. Ordered — first match wins.
REASON_HINTS: tuple[tuple[str, str], ...] = (
    ("clock skew", "The MT5 server clock and this PC disagree by more than the allowance AFTER the broker timezone "
                   "offset was applied, so the offset is not the problem — the PC clock is. Enable automatic time "
                   "sync on Windows (Settings → Time & language), or pin the server timezone with "
                   "safety.broker_utc_offset_hours if the broker really did move."),
    ("spread", "The spread was wider than risk.max_spread_price when the session opened — normal at the open. "
               "The bot retries on the next cycle; no action needed unless it stays wide."),
    ("margin", "Free margin does not cover two scout legs. Reduce risk.scout_lot, or close other positions on the account."),
    ("not safe", "An execution-safety check failed (margin, volume budget or trade permission). "
                 "Check risk.max_total_volume against the open volume and that trading is enabled on the account."),
    ("netting", "This account nets positions, so it cannot hold a BUY and a SELL at once. Use a HEDGING demo account, "
                "or set safety.require_hedging_for_scouts=false to run without the pair."),
    ("day lock", "The account-wide demo lock is active for this trading date (daily loss, profit lock or consecutive "
                 "losses). Scouts resume on the next trading date."),
    ("live account", "The connected account is not a demo account. This bot refuses to trade live — connect the demo "
                     "account in the terminal."),
    ("market closed", "The broker calendar says the market is closed (weekend, holiday or early close). "
                      "Scouts open at the next session start."),
    ("no fresh tick", "No fresh tick from the terminal — the feed is down or the symbol is out of session. "
                      "Check the terminal is connected and XAUUSD is in Market Watch."),
    ("existing", "A scout pair for this session already exists at the broker; the duplicate was refused. "
                 "Nothing to do — the running pair is intact."),
)


def reason_hint(payload: dict[str, Any]) -> str:
    """Fix line for a failed scout placement: reason text first, retcode second (v3.3.0)."""
    text = f"{payload.get('message') or ''} {payload.get('reason') or ''}".lower()
    for needle, hint in REASON_HINTS:
        if needle in text:
            if needle == "spread" and payload.get("spread") is not None:
                return f"{hint} (spread {_f(payload.get('spread'))} vs limit {_f(payload.get('max_spread_price'))})"
            if needle in {"margin", "not safe"} and payload.get("margin_free") is not None:
                return f"{hint} (free margin {_f(payload.get('margin_free'))} for {_f(payload.get('pair_volume'))} lot)"
            if needle == "clock skew":
                return (f"{hint} Detected broker offset UTC{float(payload.get('broker_utc_offset_hours') or 0):+g}h, "
                        f"residual skew {_f(payload.get('residual_skew_seconds'), 0)}s "
                        f"(limit {payload.get('max_clock_skew_seconds', 600)}s).")
            if needle == "day lock" and payload.get("day_lock"):
                return f"{hint} Lock: {payload.get('day_lock')}."
            return hint
    retcode = payload.get("retcode")
    if isinstance(retcode, (int, float)) or (isinstance(retcode, str) and str(retcode).isdigit()):
        return RETCODE_HINTS.get(int(retcode), "") or "See !events 10 for the order_attempt retcodes"
    return "See !events 10 for the order_attempt retcodes"


def offset_line(payload: dict[str, Any]) -> str:
    hours = payload.get("broker_utc_offset_hours")
    if hours is None:
        return "not measured yet"
    source = payload.get("broker_clock_source", "auto")
    residual = payload.get("residual_skew_seconds", payload.get("broker_clock_residual_seconds"))
    return f"UTC{float(hours):+g}h ({source}) · residual {_f(residual, 0)}s"


def snapshot_dict(snapshot: Any) -> dict[str, Any]:
    if isinstance(snapshot, dict):
        return snapshot
    if is_dataclass(snapshot):
        return json.loads(json.dumps(asdict(snapshot), default=json_default))
    return {}


def _g(d: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def _f(v: Any, n: int = 2) -> str:
    try:
        return f"{float(v):.{n}f}"
    except (TypeError, ValueError):
        return "n/a"


def _t(value: Any, tz: str) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone(ZoneInfo(tz)).strftime("%H:%M")
    except Exception:
        return "--:--"


def _fields(*pairs: tuple[str, str, bool]) -> list[dict[str, Any]]:
    return [{"name": n, "value": (v or "—")[:1024], "inline": i} for n, v, i in pairs]


def blocked_by_line(s: dict[str, Any]) -> str:
    """Every router veto currently active, in router order — so a NO-GO explains itself (v3.3.0)."""
    trace = _g(s, "analysis", "decision_trace", default={}) or {}
    failed = trace.get("blocked_by") or s.get("blocked_by") or []
    if not failed:
        return "nothing — every router gate passed"
    return "\n".join(f"• {g.get('name')} — {g.get('value')}"
                      + (f" (needs {g.get('threshold')})" if g.get("threshold") not in (None, "") else "")
                      for g in failed[:10])


def next_line(s: dict[str, Any]) -> str:
    trace = _g(s, "analysis", "decision_trace", default={}) or {}
    items = trace.get("next") or []
    return "\n".join(f"• {item}" for item in items[:6]) or "—"


def status_card(s: dict[str, Any], tz: str) -> dict[str, Any]:
    """[GO] / [NO-GO] card: what the router decided, what blocked it, and what would flip it."""
    go = str(s.get("go_status", "NO-GO")); action = str(_g(s, "decision", "action", default="NO_TRADE"))
    pa = s.get("pa_side") or "NEUTRAL"; conf = s.get("confluence", 0)
    colour = GREEN if go == "GO" and action in {"LONG", "SHORT"} else (AMBER if action == "WAIT" else (GREY if go == "GO" else RED))
    st = s.get("structures", {}); sc = s.get("scout", {}); im = _g(s, "analysis", "intermarket", default={})
    zone = (s.get("zones") or [None])[0]; plan = s.get("trade_plan") or {}
    tgt = _g(s, "analysis", "session_target", default={})
    trace = _g(s, "analysis", "decision_trace", default={}) or {}
    clock = _g(s, "analysis", "broker_clock", default={}) or {}
    plan_text = "none"
    if plan:
        tps = " / ".join(f"{_f(tp)} ({_f(rr)}R)" for tp, rr in zip(plan.get("take_profits") or [], plan.get("actual_rr") or []))
        plan_text = f"{plan.get('side')} entry {_f(plan.get('entry'))} · SL {_f(plan.get('stop_loss'))}\nTP {tps or 'none'} · {plan.get('target_realism')}"
    zone_text = f"{_f(zone.get('low'))}–{_f(zone.get('high'))} {zone.get('kind')}" if zone else "no zone"
    gates = trace.get("gates") or []
    gate_line = f"{trace.get('passed_count', 0)}/{trace.get('gate_count', len(gates))} gates passed" if gates else "gate table unavailable"
    return {
        "title": f"{'🟢' if colour == GREEN else '🟠' if colour == AMBER else '⚪' if colour == GREY else '🔴'} {go} · {action} · {pa} {conf}/100",
        "description": f"{s.get('session')} · {_t(s.get('timestamp'), tz)} · {_f(s.get('bid'))}/{_f(s.get('ask'))} spread {_f(s.get('spread'))} · data {s.get('freshness')}",
        "color": colour,
        "fields": _fields(
            ("Verdict", str(trace.get("verdict") or _g(s, "decision", "reason", default="")), False),
            ("Blocked by", blocked_by_line(s), False),
            ("Next", next_line(s), False),
            ("Structure", f"D1 {_g(st, 'D1', 'state')} · H4 {_g(st, 'H4', 'state')} · H1 {_g(st, 'H1', 'state')} · M15 {_g(st, 'M15', 'state')} · M5 {_g(st, 'M5', 'state')}", False),
            ("Scouts", f"{sc.get('leader') or 'none'} · BUY {_f(sc.get('buy_pnl'))} / SELL {_f(sc.get('sell_pnl'))} · {sc.get('verdict')} {sc.get('strength')}/10 · pace {sc.get('market_speed')}", True),
            ("Silver", f"{im.get('regime', 'n/a')} r={im.get('correlation')} · SMT {im.get('smt', 'NONE')} · leading {im.get('silver_leading', 'NONE')}", True),
            ("Entry", f"{zone_text} · {s.get('entry_state')} · trigger {'CONFIRMED' if _g(s, 'trigger', 'confirmed') else 'waiting'}", False),
            ("Plan", plan_text, False),
            ("$10 target", f"{tgt.get('target_verdict', 'n/a')} · {_f(_g(s, 'analysis', 'remaining_session_minutes'), 0)} min left", True),
            ("Broker clock", offset_line(clock) if clock else "n/a", True),
            ("What GO means", str(trace.get("go_meaning") or "GO = this cycle's demo setup passed every router gate; it is a signal, not an order."), False),
        ),
        "footer": {"text": f"{gate_line} · MTF {_g(s, 'analysis', 'multi_timeframe', 'label', default='n/a')} · calibration {_g(s, 'reporting', 'calibration_status', default='n/a')} · demo only"},
    }


def _sweep_lines(s: dict[str, Any], limit: int = 6) -> list[str]:
    """Newest first, deduped on level_type+price, with ROUND_1 collapsed to one line (v3.3.0)."""
    active = [sw for sw in (s.get("sweeps") or []) if sw.get("active", True)]
    active.sort(key=lambda x: x.get("age_bars", 0))
    seen: set[tuple[str, str]] = set()
    unique = []
    for sw in active:
        key = (str(sw.get("level_type")), _f(sw.get("level_price")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(sw)
    rounds = [sw for sw in unique if str(sw.get("level_type")) == "ROUND_1"]
    others = [sw for sw in unique if str(sw.get("level_type")) != "ROUND_1"]
    lines = [f"{'▲' if sw.get('direction') == 'BULLISH' else '▼'} {sw.get('level_type')} {_f(sw.get('level_price'))}"
             f" → wick {_f(sw.get('sweep_price'))} ({sw.get('age_bars')} bars)" for sw in others[:limit]]
    if rounds:
        prices = sorted(float(sw.get("level_price") or 0) for sw in rounds)
        collapsed = (f"ROUND_1 ×{len(rounds)} ({prices[0]:.0f}–{prices[-1]:.0f})" if len(rounds) > 1
                     else f"{'▲' if rounds[0].get('direction') == 'BULLISH' else '▼'} ROUND_1 {_f(rounds[0].get('level_price'))}"
                          f" → wick {_f(rounds[0].get('sweep_price'))} ({rounds[0].get('age_bars')} bars)")
        lines.append(collapsed)
    return lines[:limit]


def detections_card(s: dict[str, Any], tz: str, limit: int = 8) -> dict[str, Any]:
    """What the engine currently sees: candle/chart patterns, structure events, sweeps, live zones."""
    pats = s.get("patterns") or []
    pat_lines = [f"{_t(p.get('timestamp'), tz)} {p.get('name') or p.get('event')}" + (f" ({p.get('timeframe')})" if p.get("timeframe") else "") for p in pats[-limit:]]
    ev_lines = []
    for tf in ("H4", "H1", "M15", "M5"):
        for e in (_g(s, "structures", tf, "events", default=[]) or [])[-2:]:                    # two newest per timeframe
            ev_lines.append(f"{tf} {e.get('event')} @ {_f(e.get('level'))} {_t(e.get('timestamp'), tz)}")
    sweeps = _sweep_lines(s, 6)
    price = float(s.get("bid") or 0) or None
    atr = _g(s, "analysis", "atr", default=None) or _g(s, "reporting", "atr", default=None)
    zones = []
    for z in (s.get("zones") or [])[:5]:
        line = f"{z.get('side')} {z.get('kind')} {_f(z.get('low'))}–{_f(z.get('high'))} · score {_f(z.get('score'), 1)} · {z.get('status')}"
        if price and atr:
            middle = (float(z.get("low", 0)) + float(z.get("high", 0))) / 2
            line += f" · {abs(price - middle) / float(atr):.1f} ATR away"
        zones.append(line)
    return {
        "title": f"🔍 Detected · {s.get('session')} · {_t(s.get('timestamp'), tz)}",
        "color": PURPLE,
        "fields": _fields(
            ("Candle / chart patterns", "\n".join(pat_lines), False),
            ("Structure events", "\n".join(ev_lines), False),
            ("Active sweeps", "\n".join(sweeps), False),
            ("Zones (best first)", "\n".join(zones), False),
        ),
        "footer": {"text": f"PA {s.get('pa_side') or 'NEUTRAL'} {s.get('confluence')}/100 · {len(pats)} patterns · {len(s.get('sweeps') or [])} sweeps · {len(s.get('zones') or [])} zones"},
    }


def detection_signature(s: dict[str, Any]) -> str:
    """Changes only when something new is detected — used to push the detections card on that event."""
    pats = tuple((p.get("name") or p.get("event"), str(p.get("timestamp"))) for p in (s.get("patterns") or [])[-8:])
    evs = tuple((tf, e.get("event"), str(e.get("timestamp"))) for tf in ("H4", "H1", "M15", "M5")
                for e in (_g(s, "structures", tf, "events", default=[]) or [])[-2:])
    sweeps = tuple((sw.get("level_type"), str(sw.get("sweep_time"))) for sw in (s.get("sweeps") or []) if sw.get("active", True))
    return str(hash((pats, evs, sweeps)))


def scout_card(kind: str, payload: dict[str, Any], tz: str = "UTC") -> dict[str, Any]:
    """One card per scout lifecycle event, each telling the whole story (v3.3.0)."""
    session = payload.get("session", "")
    if kind == "scout_open_failed":
        attempt = payload.get("attempt", 1)
        retry = payload.get("next_retry")
        retry_text = (f"{_t(retry, payload.get('display_timezone') or tz)} "
                      f"({payload.get('display_timezone') or tz})") if retry else (
            "automatic on the next cycle" if payload.get("retryable", True) else "no — needs a config or account change")
        return {"title": f"🔴 SCOUTS NOT PLACED · {session}", "color": RED,
                "fields": _fields(("Reason", str(payload.get("message") or payload.get("reason")), False),
                                  ("Fix", reason_hint(payload), False),
                                  ("Detected offset", offset_line(payload), True),
                                  ("Attempt", f"#{attempt}", True),
                                  ("Next retry", retry_text, True))}
    if kind == "scout_session_open":
        failed = int(payload.get("failed_attempts") or 0)
        title = f"🟢 SCOUTS PLACED · {session}" + (f" · after {failed} failed attempt{'s' if failed != 1 else ''}" if failed else "")
        sl = payload.get("emergency_sl_price")
        return {"title": title, "color": GREEN,
                "description": f"BUY + SELL {payload.get('lot', '')} lot · magic {payload.get('magic')}",
                "fields": _fields(
                    ("BUY", f"#{payload.get('buy_ticket')} @ {_f(payload.get('buy_entry'))}"
                            + (f" · SL {_f(payload.get('buy_sl'))}" if payload.get("buy_sl") else ""), True),
                    ("SELL", f"#{payload.get('sell_ticket')} @ {_f(payload.get('sell_entry'))}"
                             + (f" · SL {_f(payload.get('sell_sl'))}" if payload.get("sell_sl") else ""), True),
                    ("Session open", f"{_f(payload.get('open_price'))} at {_t(payload.get('open_time'), tz)}", True),
                    ("Emergency SL", f"{_f(sl)} price distance" if sl else "none configured", True))}
    if kind == "scout_session_close":
        buy, sell = payload.get("buy_pnl"), payload.get("sell_pnl")
        return {"title": f"⚪ SCOUTS CLOSED · {session}", "color": GREY,
                "fields": _fields(
                    ("BUY", f"#{payload.get('buy_ticket')} · P/L {_f(buy)} · MFE {_f(payload.get('buy_mfe'))} / MAE {_f(payload.get('buy_mae'))}", True),
                    ("SELL", f"#{payload.get('sell_ticket')} · P/L {_f(sell)} · MFE {_f(payload.get('sell_mfe'))} / MAE {_f(payload.get('sell_mae'))}", True),
                    ("Pair P/L", _f(payload.get("pnl")), True),
                    ("Leader", f"{payload.get('leader') or 'NONE'} · {payload.get('verdict') or 'NEUTRAL'} {payload.get('strength', 0)}/10", True),
                    ("Pace", f"{payload.get('market_speed') or 'n/a'} · displacement {_f(payload.get('displacement'))}", True),
                    ("Message", str(payload.get("message") or payload.get("reason") or ""), False))}
    if kind == "scout_rollback":
        return {"title": f"🔴 SCOUT LEG ROLLED BACK · {session}", "color": RED,
                "fields": _fields(("Failed leg", f"{payload.get('failed_leg', 'n/a')} · retcode {payload.get('retcode', 'n/a')}", True),
                                  ("Closed", ", ".join(f"#{x}" for x in (payload.get("closed_tickets") or [])) or "nothing to close", True),
                                  ("Reason", str(payload.get("reason") or payload.get("message") or ""), False),
                                  ("Fix", reason_hint(payload), False))}
    if kind == "scout_adopted":
        return {"title": f"🟠 SCOUTS ADOPTED ON RESTART · {session}", "color": AMBER,
                "fields": _fields(("Tickets", f"BUY #{payload.get('buy_ticket')} · SELL #{payload.get('sell_ticket')}", True),
                                  ("Legs", f"{payload.get('legs', 0)} · {payload.get('lot', '')} lot", True),
                                  ("Session open", f"{_f(payload.get('open_price'))} at {_t(payload.get('open_time'), tz)}", True),
                                  ("MFE/MAE restored", ", ".join(str(x) for x in (payload.get("mfe_mae_restored") or [])) or "no", True))}
    return {"title": f"⚠️ SCOUT {kind.replace('scout_', '').replace('_', ' ').upper()} · {session}", "color": AMBER,
            "description": str(payload.get("message") or payload.get("reason") or payload.get("leader") or "pair lifecycle updated")}


def order_card(kind: str, payload: dict[str, Any], tz: str = "UTC") -> dict[str, Any]:
    """Order and management cards: the full trade, then what changed and what is left (v3.3.0)."""
    side = payload.get("side", ""); colour = GREEN if side == "LONG" else RED if side == "SHORT" else BLUE
    titles = {"order": f"📥 ORDER PLACED · {side}", "order_withheld": "⛔ ORDER WITHHELD", "pa_partial": "💰 PARTIAL CLOSE",
              "pa_breakeven": "🔒 SL moved to break-even", "pa_breakeven_retry": "🔒 Break-even retry",
              "pa_tp2_lock": "💰 TP2 — SL locked at TP1", "pa_tp2_lock_retry": "💰 TP2 lock retry",
              "pa_trail": "📈 Trailing stop moved", "pa_close": "🏁 Position closed", "trade_closed": "🏁 Trade closed"}
    if kind == "order":
        tps = " / ".join(f"{_f(tp)} ({_f(rr)}R)" for tp, rr in zip(payload.get("take_profits") or [], payload.get("actual_rr") or [])) or "none"
        risk = f"{_f(payload.get('risk_price'))} price"
        if payload.get("risk_currency") is not None:
            risk += f" ≈ {_f(payload.get('risk_currency'))} at {payload.get('volume')} lot"
        return {"title": titles["order"], "color": colour,
                "description": f"#{payload.get('ticket')} · {payload.get('session')} · entry {_f(payload.get('entry'))} · {payload.get('volume')} lot",
                "fields": _fields(
                    ("Stop loss", f"{_f(payload.get('stop_loss'))} ({payload.get('sl_reason')})", True),
                    ("Take profits", tps, True),
                    ("Risk", risk, True),
                    ("Zone", f"{payload.get('zone_kind') or 'n/a'} {payload.get('zone') or ''}", True),
                    ("Trigger", f"{payload.get('trigger_source')} · {payload.get('trigger_reason')}", False),
                    ("Confluence", f"{payload.get('confluence')}/100 · scouts {payload.get('scout_verdict')} "
                                   f"{payload.get('scout_strength')}/10 (leader {payload.get('scout_leader')})", True),
                    ("Silver", str(payload.get("silver") or "n/a"), True),
                    ("Invalidation", str(payload.get("invalidation") or ""), False))}
    if kind in {"pa_partial", "pa_breakeven", "pa_breakeven_retry", "pa_tp2_lock", "pa_tp2_lock_retry", "pa_trail", "pa_close"}:
        label = payload.get("label")
        title = titles.get(kind, kind.upper())
        if kind == "pa_partial" and label:
            title = f"💰 {label.replace('PA_', '')} — {_f(payload.get('confirmed_volume'))} lot closed"
        ok = payload.get("success", True)
        return {"title": ("" if ok else "⚠️ FAILED · ") + title, "color": (BLUE if ok else AMBER) if kind != "pa_close" else (GREY if ok else AMBER),
                "description": f"#{payload.get('ticket')} {side} · {payload.get('session') or ''} · entry {_f(payload.get('entry'))}",
                "fields": _fields(
                    ("Realised so far", _f(payload.get("realized_pnl")), True),
                    ("Remaining volume", f"{_f(payload.get('remaining_volume'))} of {_f(payload.get('original_volume'))} lot", True),
                    ("New SL", f"{_f(payload.get('new_sl', payload.get('sl')))}"
                               + (f" (was {_f(payload.get('previous_sl'))})" if payload.get("previous_sl") is not None else ""), True),
                    ("Targets", " / ".join(f"{_f(tp)} ({_f(rr)}R)" for tp, rr in
                                           zip(payload.get("take_profits") or [], payload.get("actual_rr") or [])) or "none", False),
                    ("Detail", str(payload.get("reason") or payload.get("message") or ""), False))}
    lines = [f"{k}: {v}" for k, v in payload.items()
             if k in ("ticket", "entry", "price", "sl", "stop_loss", "tp", "take_profits", "volume", "pnl", "r",
                      "exit_reason", "reason", "message", "session", "comment") and v not in (None, "")]
    return {"title": titles.get(kind, kind.upper()), "color": colour if kind != "order_withheld" else AMBER,
            "description": "\n".join(str(x) for x in lines)[:2000]}


def event_card(kind: str, payload: dict[str, Any], tz: str = "UTC") -> dict[str, Any]:
    if kind.startswith("scout_"):
        return scout_card(kind, payload, tz)
    if kind in {"order", "order_withheld", "pa_partial", "pa_breakeven", "pa_breakeven_retry", "pa_tp2_lock",
                "pa_tp2_lock_retry", "pa_trail", "pa_close", "trade_closed"}:
        return order_card(kind, payload, tz)
    if kind == "broker_clock_offset":
        return {"title": f"🕒 BROKER CLOCK · UTC{float(payload.get('broker_utc_offset_hours') or 0):+g}h", "color": BLUE,
                "fields": _fields(("Detected offset", offset_line(payload), True),
                                  ("Server", str(payload.get("server") or "n/a"), True),
                                  ("What this means", str(payload.get("message") or ""), False))}
    if kind == "session_transition":
        ok = payload.get("success", True)
        return {"title": f"{'🕒' if ok else '🔴'} SESSION {payload.get('kind', '')} · {payload.get('session', '')}" + ("" if ok else " · FAILED"),
                "color": BLUE if ok else RED, "description": str(payload.get("message", ""))}
    if kind == "session_summary":
        return {"title": f"📊 SESSION SUMMARY · {payload.get('session')} · {payload.get('go', 'NO-GO')}", "color": BLUE,
                "fields": _fields(("PA trades", f"{payload.get('pa_trades', 0)} · W/L {payload.get('wins', 0)}/{payload.get('losses', 0)} · net {_f(payload.get('net_pnl', 0))}", False),
                                  ("Scouts", f"leader {_g(payload, 'scout', 'leader', default='n/a')} · pair {_f(_g(payload, 'scout', 'pnl'))}", True),
                                  ("Status", str(payload.get("report_status", "COMPLETE")), True))}
    if kind == "smt_divergence":
        return {"title": f"🥈 SMT {payload.get('smt')} · XAU vs {payload.get('symbol')} ({payload.get('timeframe')})", "color": PURPLE,
                "description": f"XAU {_g(payload, 'detail', 'xau')} vs XAG {_g(payload, 'detail', 'xag')} · r={payload.get('correlation')} {payload.get('regime')}"}
    if kind in {"cycle_error", "startup_failed", "integration_disabled"}:
        return {"title": f"🔴 {kind.replace('_', ' ').upper()}", "color": RED, "description": json.dumps(payload, default=str)[:1900]}
    return {"title": f"ℹ️ {kind.replace('_', ' ').upper()}", "color": GREY, "description": json.dumps(payload, default=str)[:1900]}
