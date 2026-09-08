"""Read-only Discord command bot (v3.1.0).

Runs as a SEPARATE process next to the trading bot and answers `!commands` from the bot's own files:

    data/heartbeat.json                       cycle timing, errors, last decision
    data/logs/trading.sqlite3                 analysis_snapshots, trades, events, generated_reports
    data/positions_<account>_<symbol>.json     tracked PA positions, daily P/L + locks, session GO tallies
    data/scouts_<account>_<symbol>.json        scout extrema / pending closures

It never imports MetaTrader5, never sends orders and never writes to the bot's state. The trading loop is the single
authorisation point; there is deliberately no `!buy` / `!close`.

Env: DISCORD_BOT_TOKEN (required), DISCORD_COMMAND_CHANNEL_ID or DISCORD_CHANNEL_ID (optional: answer only there),
     DISCORD_ALLOWED_USER_IDS (optional, comma-separated: answer only these users), DISCORD_COMMAND_PREFIX (default "!").

Commands: !status !why !clock !detected !text !plan !silver !scouts !positions !day !trades [n] !go !events [n] !heartbeat !reports [n] !help
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from .cards import detections_card, status_card

HELP = (
    "`!why` (`!decide`) full gate table: verdict, every gate, what flips it · `!clock` broker time offset and clock guard\n""`!detected full` everything the engine sees (the plain `!detected` card is the compact companion)\n"
    "`!status` GO/NO-GO card · `!detected` patterns/structure/sweeps/zones card · `!text` old status block · `!plan` entry/SL/TP/RR · `!silver` XAU/XAG correlation + SMT · `!scouts` session pair\n"
    "`!positions` open PA trades · `!day` realised P/L and locks · `!trades [n]` last closed trades · `!go` session GO tallies\n"
    "`!events [n]` last audit events · `!reports [n]` session/weekly reports · `!heartbeat` process health · `!help`\n"
    "Read-only. Demo bot. No order commands exist."
)


class BotState:
    """Thin reader over the trading bot's files. Every call re-reads, so answers are always current."""

    def __init__(self, project_dir: str | Path) -> None:
        self.base = Path(project_dir).resolve()
        cfg = yaml.safe_load((self.base / "config.yaml").read_text(encoding="utf-8")) or {}
        self.symbol = str(cfg.get("symbol", "XAUUSD"))
        self.tz = ZoneInfo(str(cfg.get("display_timezone", "UTC")))
        logging_cfg = cfg.get("logging", {}) or {}
        self.sqlite_path = (self.base / logging_cfg.get("sqlite_path", "data/logs/trading.sqlite3")).resolve()
        self.heartbeat_path = self.base / "data" / "heartbeat.json"

    # ---- raw readers --------------------------------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.sqlite_path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def latest_snapshot(self) -> dict[str, Any] | None:
        if not self.sqlite_path.exists():
            return None
        with self._connect() as con:
            row = con.execute("SELECT timestamp, payload_json FROM analysis_snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["payload_json"]) if row else None

    def trades(self, n: int = 5) -> list[dict[str, Any]]:
        if not self.sqlite_path.exists():
            return []
        with self._connect() as con:
            rows = con.execute("SELECT * FROM trades ORDER BY COALESCE(close_time, open_time) DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def events(self, n: int = 8) -> list[dict[str, Any]]:
        if not self.sqlite_path.exists():
            return []
        with self._connect() as con:
            rows = con.execute("SELECT timestamp, event_type, payload_json FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [{"timestamp": r["timestamp"], "event_type": r["event_type"], "payload": _safe_json(r["payload_json"])} for r in rows]

    def reports(self, n: int = 3) -> list[dict[str, Any]]:
        if not self.sqlite_path.exists():
            return []
        with self._connect() as con:
            rows = con.execute("SELECT report_type, report_key, created_at, payload_json FROM generated_reports ORDER BY created_at DESC LIMIT ?", (n,)).fetchall()
        return [{"type": r["report_type"], "key": r["report_key"], "created_at": r["created_at"], "payload": _safe_json(r["payload_json"])} for r in rows]

    def heartbeat(self) -> dict[str, Any] | None:
        try:
            return json.loads(self.heartbeat_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _state_file(self, prefix: str) -> dict[str, Any]:
        files = sorted((self.base / "data").glob(f"{prefix}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        files = [f for f in files if not f.name.endswith((".bak", ".tmp"))]
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data["_file"] = f.name
                    return data
            except Exception:
                continue
        return {}

    def positions_state(self) -> dict[str, Any]:
        return self._state_file("positions")

    def scouts_state(self) -> dict[str, Any]:
        return self._state_file("scouts")

    # ---- formatting helpers -------------------------------------------------------------------------------------
    def local(self, value: str | None) -> str:
        if not value:
            return "n/a"
        try:
            return datetime.fromisoformat(value).astimezone(self.tz).strftime("%d %b %H:%M %Z")
        except Exception:
            return str(value)


def _safe_json(text: str | None) -> Any:
    try:
        return json.loads(text or "{}")
    except Exception:
        return {}


def _f(value: Any, digits: int = 2, default: str = "n/a") -> str:
    try:
        return f"{float(value):.{digits}f}" if value is not None else default
    except (TypeError, ValueError):
        return default


def _get(d: Any, *keys: str, default: Any = None) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


# ---- command formatters (pure functions; tested without Discord) -----------------------------------------------------
def fmt_status(state: BotState) -> str:
    s = state.latest_snapshot()
    if not s:
        return "No analysis snapshot yet — is the trading bot running?"
    ts = state.local(s.get("timestamp"))
    age = _age_seconds(s.get("timestamp"))
    stale = f" (⚠ {age:.0f}s old)" if age is not None and age > 60 else ""
    st = s.get("structures", {})
    sc = s.get("scout", {})
    im = _get(s, "analysis", "intermarket", default={})
    tgt = _get(s, "analysis", "session_target", default={})
    mtf = _get(s, "analysis", "multi_timeframe", default={})
    zone = (s.get("zones") or [None])[0]
    zone_text = f"{_f(zone.get('low'))}–{_f(zone.get('high'))} ({zone.get('kind')})" if zone else "none"
    lines = [
        f"**[{s.get('go_status')}] [{_get(s, 'decision', 'action')}]** {ts}{stale} · {s.get('session')} · {_f(s.get('bid'))}/{_f(s.get('ask'))} spread {_f(s.get('spread'))} · data {s.get('freshness')}",
        f"PA {s.get('pa_side') or 'NEUTRAL'} · confluence {s.get('confluence')}/100 · D1 {_get(st, 'D1', 'state')} H4 {_get(st, 'H4', 'state')} H1 {_get(st, 'H1', 'state')} M15 {_get(st, 'M15', 'state')} M5 {_get(st, 'M5', 'state')}",
        f"MTF {mtf.get('label', 'n/a')} · target {tgt.get('target_verdict', 'n/a')} · {_f(_get(s, 'analysis', 'remaining_session_minutes'), 0)} min left · calibration {_get(s, 'reporting', 'calibration_status', default='n/a')}",
        f"Scouts: leader {sc.get('leader')} · BUY {_f(sc.get('buy_pnl'))} · SELL {_f(sc.get('sell_pnl'))} · {sc.get('verdict')} {sc.get('strength')}/10 · pace {sc.get('market_speed')}",
        f"Silver: {im.get('regime', 'n/a')} r={im.get('correlation')} · SMT {im.get('smt', 'NONE')} · leading {im.get('silver_leading', 'NONE')} · +{im.get('long_points', 0)}L/+{im.get('short_points', 0)}S",
        f"Entry: {zone_text} · {s.get('entry_state')} · trigger {'CONFIRMED' if _get(s, 'trigger', 'confirmed') else 'waiting'} — {_get(s, 'trigger', 'reason')}",
        f"Reason: {_get(s, 'decision', 'reason')}",
    ]
    return "\n".join(lines)


def fmt_plan(state: BotState) -> str:
    s = state.latest_snapshot()
    if not s:
        return "No snapshot yet."
    plan = s.get("trade_plan")
    if not plan:
        return f"No trade plan this cycle — PA {s.get('pa_side') or 'NEUTRAL'}, {s.get('entry_state')}. Reason: {_get(s, 'decision', 'reason')}"
    tps = plan.get("take_profits") or []
    rrs = plan.get("actual_rr") or []
    risk = abs(float(plan.get("entry", 0)) - float(plan.get("stop_loss", 0)))
    return "\n".join([
        f"**{plan.get('side')} plan** entry {_f(plan.get('entry'))} · SL {_f(plan.get('stop_loss'))} ({plan.get('sl_reason')}) · risk {risk:.2f} price · volume {plan.get('volume')} lot",
        "TP " + " / ".join(f"{_f(tp)} ({_f(rr)}R)" for tp, rr in zip(tps, rrs)) if tps else "TP none",
        f"Realism {plan.get('target_realism')} · invalidation {plan.get('invalidation')}",
        f"Router: {_get(s, 'decision', 'action')} — {_get(s, 'decision', 'reason')}",
    ])


def fmt_silver(state: BotState) -> str:
    s = state.latest_snapshot()
    if not s:
        return "No snapshot yet."
    im = _get(s, "analysis", "intermarket", default={})
    if not im or im.get("status") != "OK":
        return f"Silver evidence unavailable: {im.get('reason', 'intermarket disabled or no data') if im else 'not present in snapshot (pre-v3.1 bot?)'}"
    detail = im.get("smt_detail") or {}
    lines = [
        f"**XAU/{im.get('symbol')}** correlation r={im.get('correlation')} → {im.get('regime')}",
        f"Relative strength (XAG% − XAU%): {im.get('relative_strength')} pp → silver leading {im.get('silver_leading')}",
        f"SMT {im.get('smt')} on {im.get('smt_timeframe')}" + (f": XAU {detail.get('xau')} vs XAG {detail.get('xag')} ({detail.get('kind')} pivots, {state.local(detail.get('timestamp'))})" if detail else ""),
        f"Confluence contribution: +{im.get('long_points', 0)} LONG / +{im.get('short_points', 0)} SHORT (evidence only; only counts while COUPLED)",
    ]
    return "\n".join(lines)


def fmt_scouts(state: BotState) -> str:
    s = state.latest_snapshot()
    if not s:
        return "No snapshot yet."
    sc = s.get("scout", {})
    if not sc.get("buy_ticket") and not sc.get("sell_ticket"):
        return f"No scout pair open ({s.get('session')}). Pace {sc.get('market_speed')} · calibration sessions {sc.get('calibration_sessions')}"
    return "\n".join([
        f"**{s.get('session')} scouts** leader {sc.get('leader')} · verdict {sc.get('verdict')} {sc.get('strength')}/10 ({sc.get('strength_source')})",
        f"BUY #{sc.get('buy_ticket')} @ {_f(sc.get('buy_entry'))} P/L {_f(sc.get('buy_pnl'))} MFE {_f(sc.get('buy_mfe'))} MAE {_f(sc.get('buy_mae'))}",
        f"SELL #{sc.get('sell_ticket')} @ {_f(sc.get('sell_entry'))} P/L {_f(sc.get('sell_pnl'))} MFE {_f(sc.get('sell_mfe'))} MAE {_f(sc.get('sell_mae'))}",
        f"Displacement {_f(sc.get('displacement'))} ({sc.get('velocity_direction')}) · pace {_f(sc.get('velocity'), 3)}/min {sc.get('market_speed')} · {sc.get('guidance')}",
        f"Pair risk at emergency SL: {_f(sc.get('estimated_pair_risk_actual'))} (1-lot {_f(sc.get('estimated_pair_risk_reference_lot'))}) — evidence only, do not mirror",
    ])


def fmt_positions(state: BotState) -> str:
    s = state.latest_snapshot()
    active = (s or {}).get("active_positions") or []
    if not active:
        tracked = state.positions_state().get("tracked", {})
        pending = state.positions_state().get("pending_finalize", {})
        return f"No active PA position. Tracked records {len(tracked)} · pending finalisation {len(pending)}"
    lines = []
    for p in active:
        lines.append(
            f"**{p.get('side')} #{p.get('ticket')}** P/L {_f(p.get('pnl'))} · {_f(p.get('r'))}R · SL {_f(p.get('sl'))} · locked {_f(p.get('locked_currency'))} · "
            f"TP1 {'DONE' if p.get('tp1_done') else 'pending'} TP2 {'DONE' if p.get('tp2_done') else 'pending'} · remaining {p.get('remaining_volume')} lot · next {_f(p.get('next_target'))} · "
            f"trailing {p.get('trailing')} · invalidation {_f(p.get('invalidation'))}"
        )
    return "\n".join(lines)


def fmt_day(state: BotState) -> str:
    ps = state.positions_state()
    day = ps.get("day") or {}
    if not day:
        return "No daily state yet."
    per = day.get("per_session") or {}
    return "\n".join([
        f"**{day.get('date')}** realised P/L {_f(day.get('pnl'))} (PA {_f(day.get('pa_pnl'))} · scouts {_f(day.get('scout_pnl'))})",
        f"PA trades {day.get('trades', 0)} · per session {', '.join(f'{k} {v}' for k, v in per.items()) or 'none'} · consecutive losses {day.get('consecutive_losses', 0)}",
        f"Lock: {day.get('locked') or 'none'} · state file {ps.get('_file')}",
    ])


def fmt_trades(state: BotState, n: int = 5) -> str:
    rows = state.trades(n)
    if not rows:
        return "No trades recorded."
    lines = [f"**Last {len(rows)} trades**"]
    for r in rows:
        lines.append(
            f"{r.get('kind')} {r.get('side') or ''} #{r.get('ticket')} {r.get('session') or ''} · {_f(r.get('volume'))} lot · "
            f"{_f(r.get('open_price'))} → {_f(r.get('close_price'))} · P/L {_f(r.get('total_realized_pnl') if r.get('total_realized_pnl') is not None else r.get('pnl'))} · "
            f"MFE {_f(r.get('mfe'))} MAE {_f(r.get('mae'))} · {r.get('exit_reason') or ''} · {state.local(r.get('close_time'))}"
            + ("" if r.get("result_confirmed") else " (unconfirmed)")
        )
    return "\n".join(lines)


def fmt_go(state: BotState) -> str:
    store = _get(state.positions_state(), "meta", "session_go", default={}) or {}
    if not store:
        return "No session GO tallies yet."
    lines = ["**Session GO tallies (current fingerprint scope)**"]
    for key in sorted(store, key=lambda k: k.split("@", 1)[1] if "@" in k else "")[-8:]:
        t = store[key]
        session = key.split(":", 1)[1].split("@", 1)[0] if ":" in key else key
        start = key.split("@", 1)[1] if "@" in key else ""
        lines.append(f"{session} {state.local(start)} · {'GO' if t.get('go_cycles') else 'NO-GO'} · GO cycles {t.get('go_cycles')}/{t.get('cycles')} · last GO {state.local(t.get('last_go'))}")
    return "\n".join(lines)


def fmt_events(state: BotState, n: int = 8) -> str:
    rows = state.events(n)
    if not rows:
        return "No events."
    lines = [f"**Last {len(rows)} events**"]
    for r in rows:
        payload = r["payload"] if isinstance(r["payload"], dict) else {}
        brief = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:4] if not isinstance(v, (dict, list)))[:160]
        lines.append(f"{state.local(r['timestamp'])} `{r['event_type']}` {brief}")
    return "\n".join(lines)


def fmt_reports(state: BotState, n: int = 3) -> str:
    rows = state.reports(n)
    if not rows:
        return "No generated reports yet."
    lines = [f"**Last {len(rows)} reports**"]
    for r in rows:
        p = r["payload"] if isinstance(r["payload"], dict) else {}
        keys = ("session", "go", "report_status", "actionability", "pa_trades", "pa_pnl", "scout_pnl", "plus_10_rate", "fakeout_rate")
        brief = ", ".join(f"{k}={p[k]}" for k in keys if k in p)[:220]
        lines.append(f"{state.local(r['created_at'])} `{r['type']}` {r['key']} {brief}")
    return "\n".join(lines)


def fmt_heartbeat(state: BotState) -> str:
    hb = state.heartbeat()
    if not hb:
        return "No heartbeat file — trading bot not started."
    age = time.time() - float(hb.get("ts_epoch", 0))
    health = "OK" if age < 180 else "STALE — supervisor should restart it"
    last = hb.get("last") or {}
    return "\n".join([
        f"**Heartbeat {health}** age {age:.0f}s · pid {hb.get('pid')} · uptime {int(hb.get('uptime_s', 0)) // 60} min · cycles {hb.get('cycles')}",
        f"Cycle ms avg {hb.get('cycle_ms_avg')} max {hb.get('cycle_ms_max')} · errors {hb.get('errors')}",
        f"Last: {', '.join(f'{k}={v}' for k, v in list(last.items())[:6])}" if last else "Last: n/a",
        ("Recent errors: " + " | ".join(str(e)[:120] for e in hb.get("last_errors", [])[-2:])) if hb.get("last_errors") else "Recent errors: none",
    ])


def fmt_why(state: BotState) -> str:
    """v3.3.0: the whole router gate table for the latest cycle — why it is a NO-GO, and what would change it."""
    s = state.latest_snapshot()
    if not s:
        return "No snapshot yet — is the trading bot running?"
    trace = _get(s, "analysis", "decision_trace", default={}) or {}
    if not trace:
        return (f"This snapshot predates v3.3.0, so there is no gate table. Router said: "
                f"{_get(s, 'decision', 'action')} — {_get(s, 'decision', 'reason')}")
    lines = [f"**{trace.get('headline')}**",
             f"{trace.get('verdict')}",
             f"{state.local(s.get('timestamp'))} · {trace.get('passed_count')}/{trace.get('gate_count')} gates passed",
             "```"]
    for gate in trace.get("gates") or []:
        icon = gate.get("icon") or ("✅" if gate.get("passed", True) else "❌")
        threshold = f" (need {gate['threshold']})" if gate.get("threshold") not in (None, "") else ""
        lines.append(f"{icon} {str(gate.get('name'))[:20]:20} {str(gate.get('value'))[:30]:30}{threshold}")
    lines.append("```")
    if trace.get("flips"):
        lines.append("**What flips it**\n" + "\n".join(str(item) for item in trace["flips"][:3]))
    evidence = trace.get("evidence") or {}
    for label, key in (("FOR", "for"), ("AGAINST", "against")):
        items = evidence.get(key) or []
        if items:
            lines.append(f"{label} ({evidence.get(key + '_score', '')}): "
                         + ", ".join(f"{i['label']} {int(i['points']):+d}" for i in items[:5]))
    lines.append(f"Silver: {evidence.get('silver', 'n/a')}")
    remaining = trace.get("remaining_vetoes") or []
    if remaining:
        lines.append("Still to clear once the zone is reached: " + ", ".join(str(x) for x in remaining))
    lines.append(trace.get("go_meaning", ""))
    return "\n".join(x for x in lines if x)


def fmt_clock(state: BotState) -> str:
    """v3.3.0: system UTC, broker time, detected offset, residual skew and the guard's verdict."""
    s = state.latest_snapshot() or {}
    clock = _get(s, "analysis", "broker_clock", default={}) or {}
    hb = state.heartbeat() or {}
    if not clock and hb.get("broker_utc_offset_hours") is not None:
        clock = {"broker_utc_offset_hours": hb.get("broker_utc_offset_hours"),
                 "residual_skew_seconds": hb.get("broker_clock_residual_seconds"),
                 "broker_clock_source": hb.get("broker_clock_source"), "clock_ok": hb.get("broker_clock_ok"),
                 "broker_server": hb.get("broker_server")}
    if not clock:
        return "No broker clock reading yet — start the trading bot (v3.3.0 or newer)."
    now = datetime.now(UTC)
    hours = float(clock.get("broker_utc_offset_hours") or 0)
    broker_now = now + timedelta(hours=hours)
    residual = clock.get("residual_skew_seconds", clock.get("broker_clock_residual_seconds"))
    limit = clock.get("max_clock_skew_seconds", 600)
    ok = clock.get("clock_ok")
    return "\n".join([
        f"**Broker clock** {'✅ orders allowed' if ok else '⛔ orders blocked by the clock guard'}",
        f"System UTC: {now.strftime('%Y-%m-%d %H:%M:%S')} · local {now.astimezone(state.tz).strftime('%H:%M:%S %Z')}",
        f"Broker time: {broker_now.strftime('%Y-%m-%d %H:%M:%S')} (server {clock.get('broker_server') or 'n/a'})",
        f"Detected offset: UTC{hours:+g}h ({clock.get('broker_clock_source', 'auto')})",
        f"Residual skew: {_f(residual, 1)}s · limit {limit}s",
        "The offset is applied to every tick and bar before analysis, so only the residual can block orders.",
    ])


def _age_seconds(iso: str | None) -> float | None:
    try:
        return (datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds()
    except Exception:
        return None


def _int_arg(args: list[str], default: int, lo: int = 1, hi: int = 20) -> int:
    try:
        return max(lo, min(hi, int(args[0])))
    except (IndexError, ValueError):
        return default


def dispatch(state: BotState, text: str, prefix: str = "!") -> str | dict[str, Any] | None:
    """Map a chat message to a reply: a string, an embed dict (card), or None when the message is not a command."""
    if not text.startswith(prefix):
        return None
    parts = text[len(prefix):].strip().split()
    if not parts:
        return None
    cmd, args = parts[0].lower(), parts[1:]
    try:
        if cmd == "status":
            snap = state.latest_snapshot()
            return status_card(snap, str(state.tz)) if snap else fmt_status(state)   # embed card (v3.2.0)
        if cmd in {"detected", "patterns", "seen"}:
            snap = state.latest_snapshot()
            full = bool(args) and args[0].lower() in {"full", "all", "everything"}      # v3.4.0: !detected full
            return detections_card(snap, str(state.tz), full=full) if snap else "No snapshot yet."
        if cmd in {"why", "decide", "explain"}: return fmt_why(state)
        if cmd in {"clock", "time", "offset"}: return fmt_clock(state)
        if cmd == "text": return fmt_status(state)
        if cmd == "plan": return fmt_plan(state)
        if cmd in {"silver", "xag", "smt"}: return fmt_silver(state)
        if cmd == "scouts": return fmt_scouts(state)
        if cmd in {"positions", "pos"}: return fmt_positions(state)
        if cmd == "day": return fmt_day(state)
        if cmd == "trades": return fmt_trades(state, _int_arg(args, 5))
        if cmd == "go": return fmt_go(state)
        if cmd == "events": return fmt_events(state, _int_arg(args, 8))
        if cmd == "reports": return fmt_reports(state, _int_arg(args, 3))
        if cmd in {"heartbeat", "hb", "health"}: return fmt_heartbeat(state)
        if cmd == "help": return HELP
        if cmd in {"buy", "sell", "close", "long", "short", "open", "modify"}:
            return "Order commands are not implemented by design: the trading loop's router is the only authorisation point."
    except Exception as exc:
        return f"Command failed: {type(exc).__name__}: {exc}"
    return None


def allowed_user_ids(raw: str) -> set[str]:
    return {part.strip() for part in raw.replace(";", ",").split(",") if part.strip()}


def authorised(channel: str, author: str, channel_id: str, allowed_users: set[str]) -> bool:
    """Channel filter (if set) AND user allow-list (if set). Empty settings mean no restriction."""
    if channel_id and channel != channel_id:
        return False
    if allowed_users and author not in allowed_users:
        return False
    return True


def chunk(text: str, limit: int = 1900) -> list[str]:
    """Split on line boundaries so no chunk exceeds Discord's 2000-character limit."""
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            out.append(cur); cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def main(project_dir: str | Path | None = None) -> None:
    try:
        import discord
    except ImportError as exc:                                   # pragma: no cover
        raise SystemExit("discord.py is not installed: pip install -e \".[discord]\"") from exc
    base = Path(project_dir or Path(__file__).resolve().parents[2])
    env_path = base / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1); os.environ[k.strip()] = v.strip()
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set in .env")
    channel_id = (os.environ.get("DISCORD_COMMAND_CHANNEL_ID") or os.environ.get("DISCORD_CHANNEL_ID") or "").strip()
    allowed_users = allowed_user_ids(os.environ.get("DISCORD_ALLOWED_USER_IDS", ""))
    prefix = os.environ.get("DISCORD_COMMAND_PREFIX", "!")
    state = BotState(base)
    intents = discord.Intents.default(); intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():                                        # pragma: no cover
        print(f"[discord_bot] logged in as {client.user}; watching {state.base}", flush=True)

    @client.event
    async def on_message(message):                               # pragma: no cover
        if message.author == client.user or message.author.bot:
            return
        if not authorised(str(message.channel.id), str(message.author.id), channel_id, allowed_users):
            return
        reply = dispatch(state, message.content, prefix)
        if reply is None:
            return
        if isinstance(reply, dict):
            await message.channel.send(embed=discord.Embed.from_dict(reply)); return
        for piece in chunk(reply):
            await message.channel.send(piece)

    client.run(token)


if __name__ == "__main__":                                       # pragma: no cover
    main()
