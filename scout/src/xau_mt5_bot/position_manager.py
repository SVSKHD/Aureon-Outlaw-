"""Tracks PA and scout positions across cycles and restarts (account-specific state), manages PA trades
(invalidation, break-even, trailing, partial exits), records complete results, and enforces daily/session locks."""
from __future__ import annotations

import json

from .statefile import atomic_write_json, load_json_state
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .config import BotConfig
from .execution import normalize_price, normalize_volume
from .mt5_client import TradingClient, broker_epoch_to_utc


class PositionManager:
    def __init__(self, client: TradingClient, config: BotConfig, record: Callable[[dict], None] | None = None,
                 audit: Callable[[str, dict], None] | None = None, state_dir: str | None = None, account_key: str = "unknown",
                 config_fingerprint: str = "") -> None:
        self.client = client; self.config = config; self.config_fingerprint = config_fingerprint
        self.record = record or (lambda _r: None); self.audit = audit or (lambda _k, _p: None)
        safe = "".join(c if c.isalnum() else "_" for c in f"{account_key}_{config.symbol}")
        base = Path(state_dir) if state_dir is not None else Path(config.project_dir) / "data"
        self.path = base / f"positions_{safe}.json"; self.path.parent.mkdir(parents=True, exist_ok=True)
        legacy = base / "positions.json"
        if not self.path.exists() and legacy.exists():
            try:
                legacy.replace(self.path)
                self.audit("legacy_state_migrated", {"from": str(legacy), "to": str(self.path)})
            except OSError:
                pass
        self.account_key = account_key
        state = self._load()
        self.tracked: dict[str, dict[str, Any]] = state.get("tracked", {})
        self.pending_finalize: dict[str, dict[str, Any]] = state.get("pending_finalize", {})
        self.setups: dict[str, dict[str, Any]] = state.get("setups", {})            # item 16: setup_id → entries, last_exit
        self.day: dict[str, Any] = state.get("day", {})                             # items 20-24: per trading date
        self.meta: dict[str, Any] = state.get("meta", {})                           # v2.1.0: engine durable tallies (GO per session, Friday-close GO)
        for rec in list(self.tracked.values()) + list(self.pending_finalize.values()):
            rec.setdefault("actual_entry", rec.get("open_price")); rec.setdefault("requested_entry", rec.get("open_price"))
            rec.setdefault("initial_sl", rec.get("plan", {}).get("stop_loss", rec.get("sl", 0)))
            rec.setdefault("final_sl", rec.get("sl", 0)); rec.setdefault("partial_exits", []); rec.setdefault("exit_deal_ids", [])
            rec.setdefault("tp1_done", False); rec.setdefault("tp2_done", False); rec.setdefault("tp2_lock_done", False)
            rec.setdefault("breakeven_done", False); rec.setdefault("realized", 0.0)

    # ---- persistence -------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        data, source = load_json_state(self.path)                                     # v2.0.0 item 8: atomic file + backup
        self.state_source = source
        if source == "backup" or (source == "none" and self.path.exists()):
            self.audit("state_file_recovered", {"file": str(self.path), "source": source,
                                                "message": "main state file unreadable; loaded backup" if source == "backup" else "state file unreadable and no backup; starting empty"})
        return data

    def _save(self) -> None:
        atomic_write_json(self.path, {"tracked": self.tracked, "pending_finalize": self.pending_finalize, "setups": self.setups, "day": self.day, "meta": self.meta})

    # ---- queries -----------------------------------------------------------
    def pa_positions(self) -> list[Any]:
        return self.client.positions(self.config.symbol, self.config.magic.pa)

    def scout_positions(self) -> list[Any]:
        magics = {self.config.magic.scout_asia, self.config.magic.scout_london, self.config.magic.scout_new_york}
        return [p for p in self.client.positions(self.config.symbol) if int(p.magic) in magics]

    def status_reports(self, bid: float, ask: float) -> list[dict[str, Any]]:
        reports = []
        for p in self.pa_positions():
            rec = self.tracked.get(str(int(p.ticket)))
            if not rec: continue
            price = bid if rec["side"] == "LONG" else ask; k = 1 if rec["side"] == "LONG" else -1
            risk = abs(rec["actual_entry"] - rec["initial_sl"]) if rec.get("initial_sl") else 0
            locked = 0.0
            if rec.get("sl") and (rec["sl"] - rec["actual_entry"]) * k > 0:
                calc = getattr(self.client, "calc_profit", None)
                locked = float(calc(self.config.symbol, rec["side"], float(p.volume), rec["actual_entry"], rec["sl"]) or 0) if calc else 0.0
            tps = rec.get("plan", {}).get("take_profits", [])
            next_target = tps[2] if rec.get("tp2_done") and len(tps) > 2 else tps[1] if rec.get("tp1_done") and len(tps) > 1 else tps[0] if tps else None
            reports.append({"ticket": rec["ticket"], "side": rec["side"], "bid": bid, "ask": ask, "pnl": float(p.profit) + rec.get("realized", 0),
                            "r": ((price - rec["actual_entry"]) * k / risk) if risk else None, "sl": rec.get("sl"), "locked_price": rec.get("sl"),
                            "locked_currency": locked, "tp1_done": rec.get("tp1_done"), "tp2_done": rec.get("tp2_done"),
                            "remaining_volume": float(p.volume), "next_target": next_target,
                            "trailing": "ACTIVE" if rec.get("tp2_done") else "WAITING FOR TP2", "invalidation": rec.get("invalidation_price"),
                            "pnl_reference_lot": (float(p.profit) + rec.get("realized", 0)) * self.config.reporting.research_reference_lot /
                                                 max(float(rec.get("original_volume", p.volume)), 1e-9)})
        return reports

    # ---- daily / session locks (items 20-24) -------------------------------
    def _day(self, tdate: str) -> dict[str, Any]:
        if self.day.get("date") != tdate:
            self.day = {"date": tdate, "pnl": 0.0, "pa_pnl": 0.0, "scout_pnl": 0.0,
                        "trades": 0, "per_session": {}, "consecutive_losses": 0, "locked": None}
        self.day.setdefault("pa_pnl", 0.0); self.day.setdefault("scout_pnl", 0.0)
        return self.day

    def entry_allowed(self, tdate: str, session: str, balance: float) -> tuple[bool, str]:
        allowed, why = self.risk_allowed(tdate, balance)
        if not allowed: return allowed, why
        d = self._day(tdate); r = self.config.risk
        if r.max_pa_trades_per_day and d["trades"] >= r.max_pa_trades_per_day: return False, "max PA trades per day reached"
        if r.max_pa_trades_per_session and d["per_session"].get(session, 0) >= r.max_pa_trades_per_session: return False, f"max PA trades in {session} reached"
        return True, "ok"

    def risk_allowed(self, tdate: str, balance: float) -> tuple[bool, str]:
        """Account-wide bot lock used by both scout and PA order paths."""
        d = self._day(tdate); r = self.config.risk
        if d.get("locked"): return False, f"day locked: {d['locked']}"
        if r.daily_max_loss_percent and d["pnl"] <= -balance * r.daily_max_loss_percent / 100:
            d["locked"] = f"daily max loss {d['pnl']:.2f}"; self._save(); return False, d["locked"]
        if r.daily_profit_lock_percent and d["pnl"] >= balance * r.daily_profit_lock_percent / 100:
            d["locked"] = f"daily profit locked {d['pnl']:.2f}"; self._save(); return False, d["locked"]
        if r.max_consecutive_losses and d["consecutive_losses"] >= r.max_consecutive_losses:
            d["locked"] = f"{d['consecutive_losses']} consecutive losses"; self._save(); return False, d["locked"]
        return True, "ok"

    def setup_allowed(self, setup_id: str, now: datetime) -> tuple[bool, str]:
        s = self.setups.get(setup_id)
        if not s: return True, "new setup"
        if s.get("open"): return False, "setup already has an open position"
        if s["entries"] >= self.config.management.max_entries_per_setup: return False, f"setup consumed ({s['entries']} entries)"
        last = s.get("last_exit")
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=self.config.management.setup_cooldown_minutes): return False, "setup cooldown"
        return True, "ok"

    # ---- tracking ----------------------------------------------------------
    def track(self, position: Any, kind: str, session: str, invalidation_price: float | None = None, side: str | None = None,
              setup_id: str | None = None, plan: dict[str, Any] | None = None, tdate: str | None = None) -> None:
        t = getattr(position, "time", None)
        # v3.3.0: position.time is a BROKER-server epoch — converted through the client's single conversion point.
        open_time = broker_epoch_to_utc(self.client, int(t)) if isinstance(t, (int, float)) else datetime.now(UTC)
        vol = float(position.volume)
        self.tracked[str(int(position.ticket))] = {
            "ticket": int(position.ticket), "kind": kind, "side": side or ("LONG" if int(getattr(position, "type", 0)) == 0 else "SHORT"),
            "session": session, "magic": int(position.magic), "volume": vol, "original_volume": vol,
            "open_time": open_time.isoformat(), "open_price": float(position.price_open),
            "sl": float(getattr(position, "sl", 0) or 0), "tp": float(getattr(position, "tp", 0) or 0),
            "invalidation_price": invalidation_price, "setup_id": setup_id, "plan": plan or {},
            "account": self.account_key, "symbol": self.config.symbol, "trading_date": tdate,
            "config_fingerprint": self.config_fingerprint,                                 # v2.0.0 item 11: bound at open, not at close
            "requested_entry": (plan or {}).get("requested_entry"), "actual_entry": float(position.price_open),
            "initial_sl": (plan or {}).get("initial_stop_loss") or float(getattr(position, "sl", 0) or 0),
            "final_sl": float(getattr(position, "sl", 0) or 0), "partial_exits": [], "exit_deal_ids": [],
            "mfe": 0.0, "mae": 0.0, "last_pnl": 0.0, "last_price": None, "breakeven_done": False,
            "tp1_done": False, "tp2_done": False, "tp2_lock_done": False, "realized": 0.0}
        if kind == "PA":
            s = self.setups.setdefault(setup_id or "unknown", {"entries": 0, "last_exit": None, "open": False})
            s["entries"] += 1; s["open"] = True
            if tdate:
                d = self._day(tdate); d["trades"] += 1; d["per_session"][session] = d["per_session"].get(session, 0) + 1
        self._save()

    def adopt_untracked(self, session: str, tdate: str | None = None) -> list[int]:
        adopted = []
        for p in self.pa_positions() + self.scout_positions():
            if str(int(p.ticket)) not in self.tracked:
                kind = "PA" if int(p.magic) == self.config.magic.pa else "SCOUT"
                self.track(p, kind, session, tdate=tdate); adopted.append(int(p.ticket))
        if adopted: self.audit("positions_adopted", {"tickets": adopted, "session": session})
        restored = [int(k) for k, v in self.tracked.items() if v["kind"] == "PA" and v.get("invalidation_price")]
        if restored: self.audit("pa_management_restored", {"tickets": restored})                       # item 26
        return adopted

    # ---- per-cycle ---------------------------------------------------------
    def update(self, m5_closed: pd.DataFrame, price: float | None = None, atr: float = 0.0, session_end: bool = False,
               bid: float | None = None, ask: float | None = None) -> list[dict[str, Any]]:
        closed_records: list[dict[str, Any]] = []
        live = {int(p.ticket): p for p in self.pa_positions() + self.scout_positions()}
        last_close = float(m5_closed.close.iloc[-1]) if len(m5_closed) else None
        for key, rec in list(self.tracked.items()):
            ticket = int(key); p = live.get(ticket)
            if p is None:
                self.pending_finalize[key] = self.tracked.pop(key); continue                          # item 10: confirm via deal history
            executable = (bid if rec["side"] == "LONG" else ask)
            if executable is None: executable = price
            if executable is None: executable = float(getattr(p, "price_current", p.price_open))
            self._sync_deals(rec)
            pnl = float(p.profit) + rec.get("realized", 0.0)
            rec["mfe"] = max(rec["mfe"], pnl); rec["mae"] = min(rec["mae"], pnl); rec["last_pnl"] = pnl; rec["last_price"] = executable
            rec["volume"] = float(p.volume); rec["sl"] = float(getattr(p, "sl", 0) or 0); rec["tp"] = float(getattr(p, "tp", 0) or 0)
            if rec["kind"] != "PA":
                continue
            inv = rec.get("invalidation_price")
            if inv and last_close is not None and ((rec["side"] == "LONG" and last_close < inv) or (rec["side"] == "SHORT" and last_close > inv)):
                self._close_full(rec, "INVALIDATION", executable, {"m5_close": last_close, "invalidation": inv}); continue
            if session_end and self.config.management.close_pa_at_session_end:                            # item 25
                self._close_full(rec, "SESSION_END", executable, {}); continue
            self._manage(rec, p, executable, atr, m5_closed)
        closed_records += self._finalize_pending(price or bid or ask or 0.0)
        self._save()
        return closed_records

    def _close_full(self, rec: dict[str, Any], reason: str, price: float, extra: dict[str, Any]) -> None:
        result = self.client.close_position(rec["ticket"], self.config.magic.pa)
        absent = self._position(rec["ticket"]) is None
        confirmed = bool(result.success and absent)
        self.audit("pa_close", {"ticket": rec["ticket"], "reason": reason, "success": confirmed, "retcode": result.retcode,
                                "message": result.message, **extra})
        if confirmed:
            rec["exit_reason_hint"] = reason
            self.pending_finalize[str(rec["ticket"])] = self.tracked.pop(str(rec["ticket"]))

    def _position(self, ticket: int) -> Any | None:
        return next((p for p in self.client.positions(self.config.symbol, self.config.magic.pa) if int(p.ticket) == int(ticket)), None)

    def _sync_deals(self, rec: dict[str, Any]) -> list[dict[str, Any]]:
        getter = getattr(self.client, "closed_deals", None)
        deals = []
        if getter:
            opened = datetime.fromisoformat(rec["open_time"])
            try:
                deals = list(getter(rec["ticket"], opened) or [])
            except TypeError:  # backward-compatible adapters
                deals = list(getter(rec["ticket"]) or [])
        rec["exit_deal_ids"] = [int(d.get("deal_ticket", i)) for i, d in enumerate(deals)]
        rec["realized"] = sum(float(d.get("net", float(d.get("profit", 0)) + float(d.get("commission", 0)) + float(d.get("swap", 0)))) for d in deals)
        return deals

    def _partial_volume(self, original: float, remaining: float, percent: float, info: Any) -> float:
        step = float(info.volume_step); minimum = float(info.volume_min)
        wanted = math.floor((original * percent / 100) / step + 1e-9) * step
        maximum = math.floor(max(0.0, remaining - minimum) / step + 1e-9) * step
        return round(min(wanted, maximum), 8) if wanted >= minimum and maximum >= minimum else 0.0

    def _modify_verified(self, rec: dict[str, Any], sl: float, tp: float, kind: str) -> bool:
        p = self._position(rec["ticket"])
        if p is None or p.symbol != self.config.symbol or int(p.magic) != self.config.magic.pa:
            self.audit(kind, {"ticket": rec["ticket"], "success": False, "message": "exact position verification failed"}); return False
        info = self.client.symbol_info(self.config.symbol); sl = normalize_price(sl, info); tp = normalize_price(tp, info) if tp else 0.0
        tick = self.client.get_tick(self.config.symbol); executable = tick.bid if rec["side"] == "LONG" else tick.ask
        distance = max(float(getattr(info, "trade_stops_level", 0)), float(getattr(info, "trade_freeze_level", 0))) * float(getattr(info, "point", 0.01))
        if sl and (abs(executable - sl) + 1e-9 < distance or (rec["side"] == "LONG" and sl >= executable) or (rec["side"] == "SHORT" and sl <= executable)):
            self.audit(kind, {"ticket": rec["ticket"], "success": False, "message": "SL violates stop/freeze or side", "sl": sl}); return False
        result = self.client.modify_sltp(rec["ticket"], sl, tp, self.config.magic.pa)
        after = self._position(rec["ticket"]); ok = bool(result.success and after and abs(float(after.sl) - sl) <= max(float(getattr(info, "point", .01)), 1e-9))
        self.audit(kind, {"ticket": rec["ticket"], "sl": sl, "tp": tp, "success": ok, "retcode": result.retcode, "message": result.message})
        if ok: rec["sl"] = sl; rec["final_sl"] = sl; rec["tp"] = tp
        return ok

    def _partial_verified(self, rec: dict[str, Any], volume: float, label: str) -> bool:
        before = self._position(rec["ticket"])
        if before is None or before.symbol != self.config.symbol or int(before.magic) != self.config.magic.pa: return False
        before_volume = float(before.volume)
        result = self.client.close_partial(rec["ticket"], volume, self.config.magic.pa, label)
        after = self._position(rec["ticket"]); after_volume = float(after.volume) if after else 0.0
        closed = before_volume - after_volume
        ok = bool(result.success and closed + 1e-9 >= volume and closed <= volume + float(self.client.symbol_info(self.config.symbol).volume_step) / 2)
        deals = self._sync_deals(rec)
        self.audit("pa_partial", {"ticket": rec["ticket"], "label": label, "requested_volume": volume, "confirmed_volume": closed,
                                  "success": ok, "retcode": result.retcode, "message": result.message})
        if ok:
            matching = deals[-1] if deals else {}
            rec["partial_exits"].append({"label": label, "volume": closed, "price": matching.get("price"), "net": matching.get("net")})
        return ok

    def _manage(self, rec: dict[str, Any], p: Any, price: float, atr: float, m5_closed: pd.DataFrame) -> None:
        m = self.config.management; side = rec["side"]; k = 1 if side == "LONG" else -1
        entry, sl0 = rec["open_price"], rec["plan"].get("stop_loss", rec["sl"]) or rec["sl"]
        risk = abs(entry - sl0) if sl0 else 0.0
        if risk <= 0: return
        rr_now = (price - entry) * k / risk
        info = self.client.symbol_info(self.config.symbol)
        tps = rec["plan"].get("take_profits", [])
        if rec.get("tp1_done") and not rec.get("breakeven_done"):
            allowance = max(m.breakeven_offset_price, self.client.get_tick(self.config.symbol).spread)
            rec["breakeven_done"] = self._modify_verified(rec, entry + k * allowance, 0.0, "pa_breakeven_retry")
            if not rec["breakeven_done"]: return
        if rec.get("tp2_done") and not rec.get("tp2_lock_done") and tps:
            rec["tp2_lock_done"] = self._modify_verified(rec, tps[0], 0.0, "pa_tp2_lock_retry")
            if not rec["tp2_lock_done"]: return
        # Partial targets are sequential and only a confirmed close unlocks the SL move.
        for idx, flag, pct in ((0, "tp1_done", m.partial_tp1_percent), (1, "tp2_done", m.partial_tp2_percent)):
            if pct and not rec[flag] and (idx == 0 or rec["tp1_done"]) and len(tps) > idx and (price - tps[idx]) * k >= 0:
                current = self._position(rec["ticket"]); remaining = float(current.volume) if current else 0.0
                vol = self._partial_volume(rec["original_volume"], remaining, pct, info)
                if vol and self._partial_verified(rec, vol, f"PA_TP{idx + 1}"):
                    rec[flag] = True
                    if idx == 0:
                        allowance = max(m.breakeven_offset_price, self.client.get_tick(self.config.symbol).spread)
                        rec["breakeven_done"] = self._modify_verified(rec, entry + k * allowance, 0.0, "pa_breakeven")
                    else:
                        rec["tp2_lock_done"] = self._modify_verified(rec, tps[0], 0.0, "pa_tp2_lock")
                    return
        if len(tps) >= 3 and rec["tp2_done"] and (price - tps[2]) * k >= 0:
            self._close_full(rec, "TP3", price, {"target": tps[2]}); return
        # Runner trails a confirmed M5 swing; ATR is a fallback.
        if rec["tp2_done"] and len(m5_closed) >= 5:
            from .structure import detect_pivots
            wanted = "LOW" if side == "LONG" else "HIGH"
            pivots = [x for x in detect_pivots(m5_closed) if x.kind == wanted]
            structural = pivots[-1].price - k * max(atr * .1, .01) if pivots else None
            trail = structural if structural is not None else (price - k * max(m.trailing_atr, 1.0) * atr if atr > 0 else None)
            if trail is not None and (trail - rec["sl"]) * k > max(.01, .05 * atr):
                self._modify_verified(rec, trail, 0.0, "pa_trail")

    def _finalize_pending(self, fallback_price: float) -> list[dict[str, Any]]:
        out = []
        for key, rec in list(self.pending_finalize.items()):
            deals = self._sync_deals(rec)
            rec["finalize_attempts"] = rec.get("finalize_attempts", 0) + 1
            total_volume = sum(float(d.get("volume", 0)) for d in deals)
            if not deals or total_volume + 1e-9 < float(rec.get("original_volume", rec.get("volume", 0))):
                continue
            deal = deals[-1]; close_time = deal["time"]
            open_time = datetime.fromisoformat(rec["open_time"])
            record = {**rec, "close_time": close_time, "close_price": deal["price"] if deal else fallback_price,
                      "pnl": rec.get("realized", 0.0), "total_realized_pnl": rec.get("realized", 0.0),
                      "commission": sum(float(d.get("commission", 0)) for d in deals), "swap": sum(float(d.get("swap", 0)) for d in deals),
                      "exit_deals": deals, "final_sl": rec.get("final_sl", rec.get("sl")),
                      "duration_s": int((close_time - open_time).total_seconds()),
                      "exit_reason": rec.get("exit_reason_hint") or (deal["reason"] if deal else "UNKNOWN_EXTERNAL"),
                      "result_confirmed": True}
            self.pending_finalize.pop(key)
            self.record(record)
            self.audit("trade_closed", {k: record.get(k) for k in ("ticket", "kind", "side", "session", "setup_id", "pnl", "mfe", "mae", "duration_s", "exit_reason", "result_confirmed")})
            if rec["kind"] == "PA":
                s = self.setups.get(rec.get("setup_id") or "unknown")
                if s: s["open"] = False; s["last_exit"] = close_time.isoformat()
            # Risk locks cover every bot-owned realized result, including both scout legs.
            from .sessions import SessionEngine
            tdate = SessionEngine.broker_trading_date(close_time).isoformat()           # v2.1.0 item 5: charge the day it CLOSED
            record["close_trading_date"] = tdate
            d = self._day(tdate)
            d["pnl"] += record["pnl"]
            bucket = "pa_pnl" if rec["kind"] == "PA" else "scout_pnl"
            d[bucket] = d.get(bucket, 0.0) + record["pnl"]
            d["consecutive_losses"] = d["consecutive_losses"] + 1 if record["pnl"] < 0 else 0
            out.append(record)
        return out
