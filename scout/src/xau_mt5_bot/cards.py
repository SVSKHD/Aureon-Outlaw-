"""Discord embed cards (v3.3.0). One builder per card, all working on the snapshot JSON dict so the webhook path
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

# v3.3.0: the failure text carries far more than a retcode, so map the reason itself. Ordered — first match wins.
REASON_HINTS: tuple[tuple[str, str], ...] = (
    ("clock skew", "The MT5 tick time still disagrees with system UTC AFTER the broker timezone offset was removed, "
                   "so this is the PC clock (or a genuinely wrong server clock), not the broker's timezone. "
                   "Fix: enable automatic time sync on the Windows PC (w32tm /resync), then the guard clears itself. "
                   "MT5 reports broker-server wall-clock times; the bot detects that offset and only blocks on what is left over."),
    ("no fresh tick", "No tick inside broker_market_stale_seconds — the symbol's session is closed at the broker or the feed dropped. "
                      "Fix: check Market Watch shows XAUUSD ticking; scouts retry automatically."),
    ("spread", "Spread is above risk.max_spread_price. Fix: wait for the spread to normalise after the session open; "
               "nothing to change unless the broker's typical spread is permanently wider."),
    ("margin", "Not enough free margin for BOTH scout legs. Fix: lower risk.scout_lot, or free margin on the demo account."),
    ("not safe", "The account safety gate refused: free margin, total volume or trade permission. "
                 "Fix: check the Free margin vs required figure below and risk.max_total_volume."),
    ("netting", "The account is NETTING, so opposing BUY and SELL legs cannot coexist and scouts are impossible. "
                "Fix: use a HEDGING demo account, or set safety.require_hedging_for_scouts=false to run without scouts."),
    ("day lock", "The account-wide daily lock is active (max loss, profit lock or consecutive losses). "
                 "Fix: nothing today — it clears at the 17:00 New York trading-date rollover."),
    ("live account", "The connected account is LIVE. This build is demo-only and will never place an order on it. "
                     "Fix: log the terminal into the DEMO account."),
    ("market closed", "The weekend/holiday/early-close calendar says the market is shut. "
                      "Fix: nothing — the next session boundary reopens scouts."),
    ("duplicate", "A scout pair for this session already exists at this magic number. "
                  "Fix: nothing — the existing pair is used."),
    ("disabled", "safety.allow_scout_orders is false. Fix: set it true in config.yaml."),
)


def reason_hint(text: str) -> str:
    """Map the human-readable failure reason to what to actually do about it (v3.3.0)."""
    low = str(text or "").lower()
    for needle, hint in REASON_HINTS:
        if needle in low:
            return hint
    return ""


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
    zone_text = f"{_f(zone.get('low'))}–{_f(zone.get('high'))} {zone.get('kind')}" if zone else "no zone"
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
            ("Entry", f"{zone_text} · {s.get('entry_state')} · trigger {'CONFIRMED' if _g(s, 'trigger', 'confirmed') else 'waiting'}", False),
            ("Plan", plan_text, False),
            ("$10 target", f"{tgt.get('target_verdict', 'n/a')} · {_f(_g(s, 'analysis', 'remaining_session_minutes'), 0)} min left", True),
            ("Blocked by", blocked_by_text(s), False),
            ("Why", str(_g(s, "decision", "reason", default="")), False),
        ),
        "footer": {"text": f"MTF {_g(s, 'analysis', 'multi_timeframe', 'label', default='n/a')} · calibration {_g(s, 'reporting', 'calibration_status', default='n/a')} · demo only"},
    }


def _sweep_lines(sweeps: list[dict], tz: str, limit: int = 6) -> list[str]:
    """Newest first, identical level_type+price collapsed, ROUND_1 folded into one counted line (v3.3.0)."""
    active = [sw for sw in (sweeps or []) if sw.get("active", True)]
    unique: dict[tuple, dict] = {}
    for sw in sorted(active, key=lambda x: x.get("age_bars", 0)):
        key = (sw.get("level_type"), round(float(sw.get("level_price") or 0), 2))
        unique.setdefault(key, sw)
    ordered = sorted(unique.values(), key=lambda x: x.get("age_bars", 0))
    rounds = [sw for sw in ordered if str(sw.get("level_type")) == "ROUND_1"]
    others = [sw for sw in ordered if str(sw.get("level_type")) != "ROUND_1"]
    lines = []
    if rounds:
        prices = sorted(float(sw.get("level_price") or 0) for sw in rounds)
        span = _f(prices[0]) if len(prices) == 1 else f"{_f(prices[0])}–{_f(prices[-1])}"
        lines.append(f"{'▲' if rounds[0].get('direction') == 'BULLISH' else '▼'} ROUND_1 ×{len(rounds)} ({span})"
                     f" · newest {rounds[0].get('age_bars')} bars")
    for sw in others:
        lines.append(f"{'▲' if sw.get('direction') == 'BULLISH' else '▼'} {sw.get('level_type')} {_f(sw.get('level_price'))}"
                     f" → wick {_f(sw.get('sweep_price'))} ({sw.get('age_bars')} bars)")
    return lines[:limit]


def detections_card(s: dict[str, Any], tz: str, limit: int = 8) -> dict[str, Any]:
    """What the engine currently sees: candle/chart patterns, structure events, sweeps, live zones."""
    pats = s.get("patterns") or []
    pat_lines = [f"{_t(p.get('timestamp'), tz)} {p.get('name') or p.get('event')}" + (f" ({p.get('timeframe')})" if p.get("timeframe") else "") for p in pats[-limit:]]
    ev_lines = []
    for tf in ("H4", "H1", "M15", "M5"):
        for e in (_g(s, "structures", tf, "events", default=[]) or [])[-2:]:            # two newest per timeframe
            ev_lines.append(f"{tf} {e.get('event')} @ {_f(e.get('level'))} {_t(e.get('timestamp'), tz)}")
    sweeps = _sweep_lines(s.get("sweeps") or [], tz)
    atr = _g(s, "analysis", "atr")
    price = s.get("bid")
    zones = []
    for z in (s.get("zones") or [])[:5]:
        distance = ""
        try:
            mid = (float(z.get("low")) + float(z.get("high"))) / 2
            gap = abs(float(price) - mid)
            distance = f" · {gap / float(atr):.2f} ATR away" if atr else f" · {gap:.2f} away"
        except (TypeError, ValueError, ZeroDivisionError):
            distance = ""
        zones.append(f"{z.get('side')} {z.get('kind')} {_f(z.get('low'))}–{_f(z.get('high'))} · score {_f(z.get('score'), 1)} · {z.get('status')}{distance}")
    return {
        "title": f"🔍 Detected · {s.get('session')} · {_t(s.get('timestamp'), tz)}",
        "color": PURPLE,
        "fields": _fields(
            ("Candle / chart patterns", "\n".join(pat_lines), False),
            ("Structure events (2 newest / TF)", "\n".join(ev_lines), False),
            ("Active sweeps (6 newest)", "\n".join(sweeps), False),
            ("Zones (best first, distance in ATR)", "\n".join(zones), False),
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


VETO_LABELS = {
    "spread": "Spread", "confluence": "Confluence < min", "zone": "Not inside zone", "trigger": "No fresh trigger",
    "slow": "Market SLOW", "scouts": "Scouts contradict", "rr": "Risk-reward", "target": "$10 target",
    "session_feasibility": "Session feasibility", "clock": "Broker clock", "day_lock": "Day lock",
}


def blocked_by_text(s: dict[str, Any]) -> str:
    """Every router veto standing in the way right now, in evaluation order (v3.3.0). A NO-GO explains itself."""
    items = _g(s, "analysis", "blocked_by", default=[]) or []
    if not items:
        return "nothing — every router veto is clear"
    return "\n".join(f"• {VETO_LABELS.get(i.get('veto'), i.get('veto'))}: {i.get('detail')}" for i in items)


def why_text(s: dict[str, Any]) -> str:
    """`!why`: the blocking vetoes plus what would flip each one."""
    items = _g(s, "analysis", "blocked_by", default=[]) or []
    if not items:
        return "Nothing is blocking a trade: every router veto is clear."
    lines = []
    for i in items:
        lines.append(f"• **{VETO_LABELS.get(i.get('veto'), i.get('veto'))}** — {i.get('detail')}\n   flips when: {i.get('flips_when')}")
    return "\n".join(lines)


def _lot_line(payload: dict[str, Any]) -> str:
    return f"{payload.get('lot', payload.get('volume', ''))} lot"


def scout_card(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """v3.3.0: every scout lifecycle event carries the whole story, not a headline."""
    session = payload.get("session", "")
    if kind == "scout_open_failed":
        retcode = payload.get("retcode")
        code_hint = RETCODE_HINTS.get(int(retcode), "") if isinstance(retcode, (int, float)) or (isinstance(retcode, str) and str(retcode).isdigit()) else ""
        message = str(payload.get("message") or payload.get("reason") or "")
        hint = " ".join(x for x in (code_hint, reason_hint(message), payload.get("hint") or "") if x).strip()
        offset = payload.get("broker_utc_offset_hours")
        offset_text = "not measured yet"
        if offset is not None:
            offset_text = (f"broker clock UTC{float(offset):+g} ({payload.get('broker_clock_source', 'auto')}) · "
                           f"residual skew {payload.get('broker_clock_residual_seconds')}s vs limit {payload.get('max_clock_skew_seconds')}s")
        return {"title": f"🔴 SCOUTS NOT PLACED · {session}", "color": RED,
                "fields": _fields(
                    ("Reason", message, False),
                    ("Fix", hint or "See !events 10 for order_attempt retcodes", False),
                    ("Detected offset", offset_text, False),
                    ("Spread", f"{_f(payload.get('spread'))} vs limit {_f(payload.get('max_spread'))}", True),
                    ("Margin", f"free {_f(payload.get('margin_free'))} vs required {_f(payload.get('margin_required'))}", True),
                    ("Attempt", f"#{payload.get('attempt', 1)}", True),
                    ("Next retry", str(payload.get("next_retry_local") or ("automatic on the next cycle" if payload.get("retryable", True) else "no — needs a config/account change")), True),
                )}
    if kind == "scout_session_open":
        after = payload.get("after_failed_attempts") or 0
        title = f"🟢 SCOUTS PLACED · {session}" + (f" · after {after} failed attempt{'s' if after != 1 else ''}" if after else "")
        return {"title": title, "color": GREEN,
                "description": f"BUY + SELL {_lot_line(payload)} · magic {payload.get('magic')}",
                "fields": _fields(
                    ("BUY leg", f"#{payload.get('buy_ticket', 'n/a')} @ {_f(payload.get('buy_entry'))}", True),
                    ("SELL leg", f"#{payload.get('sell_ticket', 'n/a')} @ {_f(payload.get('sell_entry'))}", True),
                    ("Session open price", _f(payload.get("open_price")), True),
                    ("Emergency SL distance", f"{_f(payload.get('emergency_sl_price_distance'))} price", True),
                )}
    if kind == "scout_rollback":
        return {"title": f"🔴 SCOUT LEG ROLLED BACK · {session}", "color": RED,
                "fields": _fields(
                    ("Failed leg", f"{payload.get('failed_leg', 'SELL')} — retcode {payload.get('retcode', 'n/a')}", True),
                    ("Reason", str(payload.get("reason") or payload.get("message") or ""), False),
                    ("Closed", f"{payload.get('closed_tickets') or 'the surviving leg'} · {payload.get('pending', 0)} close(s) queued for retry", False),
                    ("Fix", reason_hint(str(payload.get("reason") or payload.get("message") or "")) or "Retried automatically at the next cycle", False),
                )}
    if kind == "scout_session_close":
        return {"title": f"⚪ SCOUTS CLOSED · {session}", "color": GREY,
                "fields": _fields(
                    ("BUY", f"#{payload.get('buy_ticket', 'n/a')} · P/L {_f(payload.get('buy_pnl'))} · MFE {_f(payload.get('buy_mfe'))} / MAE {_f(payload.get('buy_mae'))}", False),
                    ("SELL", f"#{payload.get('sell_ticket', 'n/a')} · P/L {_f(payload.get('sell_pnl'))} · MFE {_f(payload.get('sell_mfe'))} / MAE {_f(payload.get('sell_mae'))}", False),
                    ("Pair P/L", _f(payload.get("pnl") if payload.get("pnl") is not None else payload.get("pair_pnl")), True),
                    ("Leader", str(payload.get("leader", "n/a")), True),
                    ("Verdict", f"{payload.get('verdict', 'n/a')} {payload.get('strength', 0)}/10", True),
                    ("Message", str(payload.get("message") or payload.get("reason") or ""), False),
                )}
    if kind == "scout_adopted":
        return {"title": f"🟠 SCOUTS ADOPTED · {session}", "color": AMBER,
                "description": "Restart recovery — the existing pair was re-attached instead of re-opened.",
                "fields": _fields(
                    ("Tickets", str(payload.get("tickets") or "n/a"), True),
                    ("Legs", str(payload.get("legs", 0)), True),
                    ("MFE/MAE restored", str(payload.get("mfe_mae_restored") or "no stored extrema"), False),
                    ("Session open", f"{_f(payload.get('open_price'))} at {payload.get('open_time') or 'n/a'}", False),
                )}
    return {"title": f"⚠️ SCOUT {kind.replace('scout_', '').replace('_', ' ').upper()} · {session}", "color": AMBER,
            "description": str(payload.get("message") or payload.get("reason") or payload.get("leader") or "pair lifecycle updated")}


def _tp_text(payload: dict[str, Any]) -> str:
    tps = payload.get("take_profits") or payload.get("tp") or []
    rrs = payload.get("actual_rr") or payload.get("rr") or []
    if isinstance(tps, (int, float)):
        tps = [tps]
    if isinstance(rrs, (int, float)):
        rrs = [rrs]
    if not tps:
        return "none"
    parts = []
    for i, tp in enumerate(tps):
        rr = rrs[i] if i < len(rrs) else None
        parts.append(f"TP{i + 1} {_f(tp)}" + (f" ({_f(rr)}R)" if rr is not None else ""))
    return " · ".join(parts)


def order_card(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """v3.3.0: an order card carries the whole trade, and every management card carries the running result."""
    side = payload.get("side", "")
    colour = GREEN if side == "LONG" else RED if side == "SHORT" else BLUE
    titles = {"order": f"📥 ORDER PLACED · {side}", "order_withheld": "⛔ ORDER WITHHELD", "pa_partial": "💰 TP1 — 50% closed",
              "pa_breakeven": "🔒 SL moved to break-even", "pa_tp2_lock": "💰 TP2 — 25% closed, SL locked at TP1",
              "pa_trail": "📈 Trailing stop moved", "pa_close": "🏁 Position closed", "trade_closed": "🏁 Trade closed"}
    if kind in {"order", "order_withheld"}:
        rejected = kind == "order" and payload.get("success") is False
        title = f"🔴 ORDER REJECTED · {side}" if rejected else titles[kind]
        return {"title": title, "color": RED if rejected else (colour if kind == "order" else AMBER),
                "description": str(payload.get("reason") or payload.get("message") or ""),
                "fields": _fields(
                    ("Ticket / entry", f"#{payload.get('ticket', 'n/a')} @ {_f(payload.get('entry') or payload.get('price'))}", True),
                    ("Volume / risk", f"{payload.get('volume', 'n/a')} lot · risk {_f(payload.get('risk_currency'))} {payload.get('currency', '')}".strip(), True),
                    ("Stop loss", _f(payload.get("stop_loss") or payload.get("sl")), True),
                    ("Take profits", _tp_text(payload), False),
                    ("Zone / trigger", f"{payload.get('zone_kind', 'n/a')} · {payload.get('trigger_reason', 'n/a')}", False),
                    ("Confluence / scouts", f"{payload.get('confluence', 'n/a')}/100 · scouts {payload.get('scout_verdict', 'n/a')} "
                                            f"{payload.get('scout_strength', 0)}/10 (leader {payload.get('scout_leader', 'n/a')})", False),
                    ("Silver", str(payload.get("silver") or payload.get("intermarket") or "n/a"), False),
                )}
    if kind in {"pa_partial", "pa_breakeven", "pa_tp2_lock", "pa_trail", "pa_close", "trade_closed"}:
        return {"title": titles[kind], "color": colour,
                "description": str(payload.get("reason") or payload.get("exit_reason") or payload.get("message") or ""),
                "fields": _fields(
                    ("Ticket", f"#{payload.get('ticket', 'n/a')} · {side or payload.get('kind', '')}", True),
                    ("Realised P/L so far", _f(payload.get("realized_pnl") if payload.get("realized_pnl") is not None else payload.get("pnl")), True),
                    ("Remaining volume", f"{payload.get('remaining_volume', payload.get('volume', 'n/a'))} lot", True),
                    ("New SL", _f(payload.get("new_sl") or payload.get("sl")), True),
                    ("R multiple", _f(payload.get("r")), True),
                )}
    lines = [f"{k}: {v}" for k, v in payload.items()
             if k in ("ticket", "entry", "price", "sl", "stop_loss", "tp", "take_profits", "volume", "pnl", "r",
                      "exit_reason", "reason", "message", "session", "comment") and v not in (None, "")]
    return {"title": titles.get(kind, kind.upper()), "color": colour, "description": "\n".join(str(x) for x in lines)[:2000]}


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
    if kind == "broker_clock_offset":
        hours = payload.get("offset_hours", 0)
        return {"title": f"🕰️ BROKER CLOCK · UTC{float(hours):+g}", "color": BLUE if payload.get("residual_skew_seconds", 0) is not None else GREY,
                "description": str(payload.get("message", "")),
                "fields": _fields(("Detected offset", f"UTC{float(hours):+g} ({payload.get('source', 'auto')})", True),
                                  ("Residual skew", f"{payload.get('residual_skew_seconds')}s vs limit {payload.get('max_clock_skew_seconds')}s", True),
                                  ("Server", str(payload.get("server") or "n/a"), True))}
    if kind in {"cycle_error", "startup_failed", "integration_disabled"}:
        return {"title": f"🔴 {kind.replace('_', ' ').upper()}", "color": RED, "description": json.dumps(payload, default=str)[:1900]}
    return {"title": f"ℹ️ {kind.replace('_', ' ').upper()}", "color": GREY, "description": json.dumps(payload, default=str)[:1900]}
