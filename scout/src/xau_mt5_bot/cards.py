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


def status_card(s: dict[str, Any], tz: str) -> dict[str, Any]:
    """[GO] / [NO-GO] card: what the router decided and why, in one screen."""
    go = str(s.get("go_status", "NO-GO")); action = str(_g(s, "decision", "action", default="NO_TRADE"))
    pa = s.get("pa_side") or "NEUTRAL"; conf = s.get("confluence", 0)
    colour = GREEN if go == "GO" and action in {"LONG", "SHORT"} else (AMBER if action == "WAIT" else (GREY if go == "GO" else RED))
    st = s.get("structures", {}); sc = s.get("scout", {}); im = _g(s, "analysis", "intermarket", default={})
    zone = (s.get("zones") or [None])[0]; plan = s.get("trade_plan") or {}
    tgt = _g(s, "analysis", "session_target", default={})
    plan_text = "none"
    if plan:
        tps = " / ".join(f"{_f(tp)} ({_f(rr)}R)" for tp, rr in zip(plan.get("take_profits") or [], plan.get("actual_rr") or []))
        plan_text = f"{plan.get('side')} entry {_f(plan.get('entry'))} · SL {_f(plan.get('stop_loss'))}\nTP {tps or 'none'} · {plan.get('target_realism')}"
    return {
        "title": f"{'🟢' if colour == GREEN else '🟠' if colour == AMBER else '⚪' if colour == GREY else '🔴'} {go} · {action} · {pa} {conf}/100",
        "description": f"{s.get('session')} · {_t(s.get('timestamp'), tz)} · {_f(s.get('bid'))}/{_f(s.get('ask'))} spread {_f(s.get('spread'))} · data {s.get('freshness')}",
        "color": colour,
        "fields": _fields(
            ("Structure", f"D1 {_g(st, 'D1', 'state')} · H4 {_g(st, 'H4', 'state')} · H1 {_g(st, 'H1', 'state')} · M15 {_g(st, 'M15', 'state')} · M5 {_g(st, 'M5', 'state')}", False),
            ("Scouts", f"{sc.get('leader') or 'none'} · BUY {_f(sc.get('buy_pnl'))} / SELL {_f(sc.get('sell_pnl'))} · {sc.get('verdict')} {sc.get('strength')}/10 · pace {sc.get('market_speed')}", True),
            ("Silver", f"{im.get('regime', 'n/a')} r={im.get('correlation')} · SMT {im.get('smt', 'NONE')} · leading {im.get('silver_leading', 'NONE')}", True),
            ("Entry", f"{f'{_f(zone.get('low'))}–{_f(zone.get('high'))} {zone.get('kind')}' if zone else 'no zone'} · {s.get('entry_state')} · trigger {'CONFIRMED' if _g(s, 'trigger', 'confirmed') else 'waiting'}", False),
            ("Plan", plan_text, False),
            ("$10 target", f"{tgt.get('target_verdict', 'n/a')} · {_f(_g(s, 'analysis', 'remaining_session_minutes'), 0)} min left", True),
            ("Why", str(_g(s, "decision", "reason", default="")), False),
        ),
        "footer": {"text": f"MTF {_g(s, 'analysis', 'multi_timeframe', 'label', default='n/a')} · calibration {_g(s, 'reporting', 'calibration_status', default='n/a')} · demo only"},
    }


def detections_card(s: dict[str, Any], tz: str, limit: int = 8) -> dict[str, Any]:
    """What the engine currently sees: candle/chart patterns, structure events, sweeps, live zones."""
    pats = s.get("patterns") or []
    pat_lines = [f"{_t(p.get('timestamp'), tz)} {p.get('name') or p.get('event')}" + (f" ({p.get('timeframe')})" if p.get("timeframe") else "") for p in pats[-limit:]]
    ev_lines = []
    for tf in ("H4", "H1", "M15", "M5"):
        for e in (_g(s, "structures", tf, "events", default=[]) or [])[-2:]:
            ev_lines.append(f"{tf} {e.get('event')} @ {_f(e.get('level'))} {_t(e.get('timestamp'), tz)}")
    sweeps = [f"{'▲' if sw.get('direction') == 'BULLISH' else '▼'} {sw.get('level_type')} {_f(sw.get('level_price'))} → wick {_f(sw.get('sweep_price'))} ({sw.get('age_bars')} bars)"
              for sw in sorted((sw for sw in (s.get("sweeps") or []) if sw.get("active", True)), key=lambda x: x.get("age_bars", 0))][:limit]
    zones = [f"{z.get('side')} {z.get('kind')} {_f(z.get('low'))}–{_f(z.get('high'))} · score {_f(z.get('score'), 1)} · {z.get('status')}" for z in (s.get("zones") or [])[:5]]
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


def scout_card(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    session = payload.get("session", "")
    if kind == "scout_open_failed":
        retcode = payload.get("retcode")
        hint = RETCODE_HINTS.get(int(retcode), "") if isinstance(retcode, (int, float)) or (isinstance(retcode, str) and retcode.isdigit()) else payload.get("hint", "")
        return {"title": f"🔴 SCOUTS NOT PLACED · {session}", "color": RED,
                "fields": _fields(("Reason", str(payload.get("message") or payload.get("reason")), False),
                                  ("Fix", hint or "See !events 10 for order_attempt retcodes", False),
                                  ("Retry", "automatic on the next cycle" if payload.get("retryable", True) else "no — needs a config/account change", True))}
    if kind == "scout_session_open":
        return {"title": f"🟢 SCOUTS PLACED · {session}", "color": GREEN,
                "description": f"BUY + SELL {payload.get('lot', '')} lot · magic {payload.get('magic')} · session open {payload.get('open_price', '')}"}
    if kind == "scout_session_close":
        return {"title": f"⚪ SCOUTS CLOSED · {session}", "color": GREY,
                "fields": _fields(("Leader", str(payload.get("leader", "n/a")), True), ("Pair P/L", _f(payload.get("pnl")), True),
                                  ("Message", str(payload.get("message") or payload.get("reason") or ""), False))}
    return {"title": f"⚠️ SCOUT {kind.replace('scout_', '').replace('_', ' ').upper()} · {session}", "color": AMBER,
            "description": str(payload.get("message") or payload.get("reason") or payload.get("leader") or "pair lifecycle updated")}


def order_card(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    side = payload.get("side", ""); colour = GREEN if side == "LONG" else RED if side == "SHORT" else BLUE
    titles = {"order": f"📥 ORDER PLACED · {side}", "order_withheld": "⛔ ORDER WITHHELD", "pa_partial": "💰 TP1 — 50% closed",
              "pa_breakeven": "🔒 SL moved to break-even", "pa_tp2_lock": "💰 TP2 — 25% closed, SL locked at TP1",
              "pa_trail": "📈 Trailing stop moved", "pa_close": "🏁 Position closed", "trade_closed": "🏁 Trade closed"}
    lines = [f"{k}: {v}" for k, v in payload.items() if k in ("ticket", "entry", "price", "sl", "stop_loss", "tp", "take_profits", "volume", "pnl", "r", "exit_reason", "reason", "message", "session", "comment") and v not in (None, "")]
    return {"title": titles.get(kind, kind.upper()), "color": colour if kind != "order_withheld" else AMBER, "description": "\n".join(str(x) for x in lines)[:2000]}


def event_card(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind.startswith("scout_"):
        return scout_card(kind, payload)
    if kind in {"order", "order_withheld", "pa_partial", "pa_breakeven", "pa_tp2_lock", "pa_trail", "pa_close", "trade_closed"}:
        return order_card(kind, payload)
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
