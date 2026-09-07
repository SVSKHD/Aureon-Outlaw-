from __future__ import annotations

from zoneinfo import ZoneInfo

from .models import AnalysisSnapshot


def _price(value: float | None) -> str:
    return "NONE" if value is None else f"{value:.2f}"


def format_report(snapshot: AnalysisSnapshot, display_timezone: str = "Asia/Kolkata") -> str:
    structures = "\n".join(
        f"{timeframe}: {result.state.value}" for timeframe, result in snapshot.structures.items()
    )
    pattern_names = ", ".join(str(item.get("name", item.get("event", ""))) for item in snapshot.patterns[-12:]) or "None"
    scout = snapshot.scout
    zone = snapshot.zones[0] if snapshot.zones else None
    plan = snapshot.trade_plan
    tps = "NONE" if plan is None else ", ".join(_price(value) for value in plan.take_profits)
    rrs = "NONE" if plan is None else ", ".join(f"{value:.2f}" for value in plan.actual_rr)
    local_time = snapshot.timestamp.astimezone(ZoneInfo(display_timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
    protection = ""
    if plan:
        pcts = ""
        if len(plan.take_profits) >= 1: pcts += f"\nTP1: {plan.take_profits[0]:.2f} — close configured first partial"
        if len(plan.take_profits) >= 2: pcts += f"\nTP2: {plan.take_profits[1]:.2f} — close configured second partial"
        if len(plan.take_profits) >= 3: pcts += f"\nTP3: {plan.take_profits[2]:.2f} — runner target"
        protection = f"""\nPROFIT PROTECTION{pcts}
At TP1 -> confirm partial close, then move SL above/below entry with spread allowance
At TP2 -> confirm partial close, then lock SL at TP1
After TP2 -> trail behind confirmed M5 structure (ATR fallback)
"""
    active = ""
    if snapshot.active_positions:
        rows = [f"Ticket {p['ticket']} {p['side']} | Bid/Ask {p['bid']:.2f}/{p['ask']:.2f} | P/L {p['pnl']:+.2f} | R {p['r']:.2f}" if p['r'] is not None else f"Ticket {p['ticket']} {p['side']} | Bid/Ask {p['bid']:.2f}/{p['ask']:.2f} | P/L {p['pnl']:+.2f} | R n/a" for p in snapshot.active_positions]
        active = "\nACTIVE PA POSITION\n" + "\n".join(rows) + "\n" + "\n".join(
            f"SL {p['sl']:.2f} | locked {p['locked_currency']:+.2f} | TP1 {'done' if p['tp1_done'] else 'pending'} | TP2 {'done' if p['tp2_done'] else 'pending'} | remaining {p['remaining_volume']:.2f} | next {_price(p['next_target'])} | trail {p['trailing']}"
            for p in snapshot.active_positions)
    return f"""XAUUSD LIVE ANALYSIS
Time: {local_time}
Session: {snapshot.session.value}
Bid / Ask: {_price(snapshot.bid)} / {_price(snapshot.ask)}
Spread: {snapshot.spread:.2f}
Data: {snapshot.freshness.value}

HIGHER TIMEFRAME
{structures}

DETECTED PATTERNS
{pattern_names}

PRICE ACTION BIAS
{snapshot.pa_side.value if snapshot.pa_side else 'NEUTRAL'}
Confluence: {snapshot.confluence}/100 (not a win probability)

SCOUT STATUS
BUY: entry {_price(scout.buy_entry)}, P&L {scout.buy_pnl:.2f}, MFE {scout.buy_mfe:.2f}, MAE {scout.buy_mae:.2f}
SELL: entry {_price(scout.sell_entry)}, P&L {scout.sell_pnl:.2f}, MFE {scout.sell_mfe:.2f}, MAE {scout.sell_mae:.2f}
Research {snapshot.reporting.get('research_reference_lot', 1):.2f}-lot scale (normalised, not a permission): BUY {scout.buy_pnl_reference_lot:+.2f}, SELL {scout.sell_pnl_reference_lot:+.2f}
Scout role: SECONDARY EVIDENCE ONLY — never a direction source, never positions to copy; both spreads are in demo P/L
Leader: {scout.leader}
Scout evidence: {scout.verdict.value} ({scout.strength}/10)
Market speed: {scout.market_speed}
Guidance: {scout.guidance}

ENTRY
Zone: {'NONE' if zone is None else f'{zone.low:.2f}-{zone.high:.2f} ({zone.kind})'}
Status: {snapshot.entry_state.value}
Touch: {snapshot.trigger.touch_time.isoformat() if snapshot.trigger.touch_time else 'NONE'}
Trigger: {'CONFIRMED' if snapshot.trigger.confirmed else 'WAITING'} — {snapshot.trigger.reason}

EXECUTION
Entry: {_price(plan.entry if plan else None)}
SL: {_price(plan.stop_loss if plan else None)}
SL reason: {plan.sl_reason if plan else 'NONE'}
TPs: {tps}
Actual RR: {rrs}
Target realism: {plan.target_realism.value if plan else 'NONE'}
Invalidation: {plan.invalidation if plan else 'NONE'}
{protection}{active}

MULTI-TIMEFRAME
{_mtf_line(snapshot)}
Treatment: {snapshot.analysis.get('treatment', {}).get('treatment', 'n/a')} — {snapshot.analysis.get('treatment', {}).get('reason', '')}

HISTORICAL RELIABILITY (comparable setups decided before now; not a guarantee)
{_reliability_line(snapshot)}

SESSION TARGET (${snapshot.analysis.get('session_target', {}).get('session_target_price_move', 10):.0f} XAUUSD price move)
{_target_line(snapshot)}

FINAL DECISION (DEMO ACCOUNT ONLY)
signal_go: {snapshot.go_status} | trade_action: {snapshot.decision.action.value} | scout_verdict: {scout.verdict.value} | target_verdict: {snapshot.analysis.get('session_target', {}).get('target_verdict', 'DISABLED')} | calibration: {snapshot.reporting.get('calibration_status', 'COLLECTING')}
Reason: {snapshot.decision.reason}
Remaining session: {snapshot.analysis.get('remaining_session_minutes', 0):.0f} min
Research scale ({snapshot.reporting.get('research_reference_lot', 1):.2f} lot, normalised): active PA {snapshot.reporting.get('active_pa_pnl_reference_lot', 0):+.2f}
"""


def _mtf_line(snapshot) -> str:
    mtf = snapshot.analysis.get("multi_timeframe", {})
    tfs = mtf.get("timeframes", {})
    parts = [f"{tf} {v.get('state')} ({v.get('role')})" for tf, v in tfs.items()]
    return f"{mtf.get('label', 'n/a')} score {mtf.get('score', 0)}/{mtf.get('max_score', 10)}: " + "; ".join(parts)


def _reliability_line(snapshot) -> str:
    r = snapshot.analysis.get("historical_pattern_reliability", {})
    if not r or r.get("samples", 0) == 0: return "No resolved comparable setups yet (COLLECTING)."
    return (f"{r.get('status')} · level {r.get('comparison_level')} · n={r.get('samples')} · +3 {r.get('plus_3_hit_rate')} · +5 {r.get('plus_5_hit_rate')} "
            f"· +10 {r.get('plus_10_hit_rate')} CI {r.get('confidence_interval_plus_10')} · fakeout {r.get('fakeout_rate')} · median MFE {r.get('median_mfe')} MAE {r.get('median_mae')} "
            f"· scout agreement {r.get('scout_agreement_rate')}")


def _target_line(snapshot) -> str:
    t = snapshot.analysis.get("session_target", {})
    if not t or t.get("target_verdict") in (None, "DISABLED"): return "disabled"
    return (f"{t.get('target_verdict')} (structural {t.get('structural_verdict')}, p≈{t.get('estimated_probability')}) · target {t.get('target_price')} · required {t.get('required_price_move')} "
            f"· {t.get('remaining_session_minutes')} min left · pace {t.get('current_velocity')} vs required {t.get('required_velocity')} /min · session range {t.get('session_range')} "
            f"(consumed {t.get('session_range_consumed')}) · est. remaining {t.get('estimated_remaining_range')} · blocker {t.get('nearest_blocking_liquidity')} at {t.get('distance_to_blocking_liquidity')}\n"
            f"Why: {t.get('target_reason')}")
