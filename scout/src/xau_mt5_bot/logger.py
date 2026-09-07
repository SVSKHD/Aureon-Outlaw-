from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from .models import AnalysisSnapshot


def json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot JSON serialize {type(value).__name__}")


def as_utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class AuditLogger:
    def __init__(self, sqlite_path: str, jsonl_path: str, retention_days: int = 30, rotate_daily: bool = True) -> None:
        self.sqlite_path = Path(sqlite_path)
        self.jsonl_path = Path(jsonl_path)
        self.retention_days, self.rotate_daily = retention_days, rotate_daily
        self.context: dict[str, Any] = {}          # account/server/symbol/setup added to every order + trade record (item 38)
        self._last_prune = 0.0
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_schema()
        self._recover_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _recover_database(self) -> None:
        """Checkpoint a surviving WAL and fail loudly if an unclean supervisor kill damaged SQLite."""
        with self._connect() as database:
            database.execute("PRAGMA wal_checkpoint(PASSIVE)")
            status = database.execute("PRAGMA quick_check").fetchone()
        if not status or status[0] != "ok":
            raise RuntimeError(f"SQLite recovery check failed: {status[0] if status else 'no result'}")

    def _create_schema(self) -> None:
        with self._connect() as database:
            database.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analysis_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    session TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    side TEXT, session TEXT, magic INTEGER, volume REAL,
                    open_time TEXT, open_price REAL, close_time TEXT, close_price REAL,
                    sl REAL, tp REAL, pnl REAL, mfe REAL, mae REAL, duration_s INTEGER, exit_reason TEXT,
                    account TEXT NOT NULL DEFAULT 'unknown', symbol TEXT NOT NULL DEFAULT 'unknown',
                    setup_id TEXT, result_confirmed INTEGER,
                    requested_entry REAL, actual_entry REAL, initial_sl REAL, final_sl REAL,
                    tp1 REAL, tp2 REAL, tp3 REAL, partial_exits_json TEXT,
                    total_realized_pnl REAL, commission REAL, swap REAL,
                    config_fingerprint TEXT,
                    payload_json TEXT,
                    UNIQUE(account, symbol, ticket)
                );
                CREATE TABLE IF NOT EXISTS event_keys (
                    event_type TEXT NOT NULL, event_key TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(event_type, event_key)
                );
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    order_kind TEXT NOT NULL,
                    ticket INTEGER,
                    success INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generated_reports (
                    report_type TEXT NOT NULL,
                    report_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(report_type, report_key)
                );
                """
            )
            # v1.1/v1.2 databases already have `trades`; add newer audit fields in place.
            wanted = {
                "side": "TEXT", "session": "TEXT", "magic": "INTEGER", "volume": "REAL", "open_time": "TEXT",
                "open_price": "REAL", "close_time": "TEXT", "close_price": "REAL", "sl": "REAL", "tp": "REAL",
                "pnl": "REAL", "mfe": "REAL", "mae": "REAL", "duration_s": "INTEGER", "exit_reason": "TEXT",
                "account": "TEXT", "symbol": "TEXT", "setup_id": "TEXT", "result_confirmed": "INTEGER",
                "requested_entry": "REAL", "actual_entry": "REAL", "initial_sl": "REAL", "final_sl": "REAL",
                "tp1": "REAL", "tp2": "REAL", "tp3": "REAL", "partial_exits_json": "TEXT",
                "total_realized_pnl": "REAL", "commission": "REAL", "swap": "REAL", "config_fingerprint": "TEXT", "payload_json": "TEXT",
            }
            existing = {row[1] for row in database.execute("PRAGMA table_info(trades)")}
            for name, sql_type in wanted.items():
                if name not in existing:
                    database.execute(f"ALTER TABLE trades ADD COLUMN {name} {sql_type}")
            self._migrate_trade_identity(database)
            self._backfill_event_keys(database)

    TRADES_DDL_V3 = """
        CREATE TABLE {name} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket INTEGER NOT NULL, kind TEXT NOT NULL,
            side TEXT, session TEXT, magic INTEGER, volume REAL,
            open_time TEXT, open_price REAL, close_time TEXT, close_price REAL,
            sl REAL, tp REAL, pnl REAL, mfe REAL, mae REAL, duration_s INTEGER, exit_reason TEXT,
            account TEXT NOT NULL DEFAULT 'unknown', symbol TEXT NOT NULL DEFAULT 'unknown',
            setup_id TEXT, result_confirmed INTEGER,
            requested_entry REAL, actual_entry REAL, initial_sl REAL, final_sl REAL,
            tp1 REAL, tp2 REAL, tp3 REAL, partial_exits_json TEXT,
            total_realized_pnl REAL, commission REAL, swap REAL,
            config_fingerprint TEXT, payload_json TEXT,
            UNIQUE(account, symbol, ticket)
        )"""

    @staticmethod
    def _trades_schema_is_current(database) -> bool:
        info = list(database.execute("PRAGMA table_info(trades)"))
        cols = {row[1]: row for row in info}
        if [row[1] for row in info if row[5]] != ["id"]: return False
        for name in ("account", "symbol"):
            row = cols.get(name)
            if row is None or int(row[3]) != 1: return False                          # NOT NULL required
        for idx in database.execute("PRAGMA index_list(trades)"):
            if int(idx[2]) == 1:                                                       # unique index
                members = [r[2] for r in database.execute(f"PRAGMA index_info('{idx[1]}')")]
                if members == ["account", "symbol", "ticket"]: return True
        return False

    @classmethod
    def _migrate_trade_identity(cls, database) -> None:
        """v3.0.0 §5.4: trades keyed by (account, symbol, ticket) with NOT NULL identity. Handles pre-v2.3 (ticket PK),
        authentic v2.3 (id PK, nullable identity) and current schemas; transactional, restart-safe, idempotent."""
        if cls._trades_schema_is_current(database):
            database.execute("DROP TABLE IF EXISTS trades_v3")                          # leftover from a crash after the rename
            return
        cols = [row[1] for row in database.execute("PRAGMA table_info(trades)")]
        database.execute("BEGIN IMMEDIATE")
        try:
            database.execute("DROP TABLE IF EXISTS trades_v3")
            database.execute(cls.TRADES_DDL_V3.format(name="trades_v3"))
            common = [c for c in cols if c != "id"]
            select = ",".join("COALESCE(account,'unknown')" if c == "account" else "COALESCE(symbol,'unknown')" if c == "symbol" else c for c in common)
            database.execute(f"INSERT OR IGNORE INTO trades_v3({','.join(common)}) SELECT {select} FROM trades")
            database.execute("DROP TABLE trades")
            database.execute("ALTER TABLE trades_v3 RENAME TO trades")
            database.execute("COMMIT")
        except Exception:
            database.execute("ROLLBACK")
            raise

    @staticmethod
    def _backfill_event_keys(database) -> None:
        """§5.3: register a scoped key (account:symbol:fingerprint:session_id) for every existing scout_session_stats and
        drop only exact same-scope duplicates (earliest kept). Legacy rows without a session_id are left untouched."""
        scope = ("COALESCE(json_extract(payload_json,'$.account'),'unknown') || ':' || COALESCE(json_extract(payload_json,'$.symbol'),'unknown') "
                 "|| ':' || COALESCE(json_extract(payload_json,'$.config_fingerprint'),'') || ':' || json_extract(payload_json,'$.session_id')")
        database.execute(f"""
            INSERT OR IGNORE INTO event_keys(event_type, event_key, created_at)
            SELECT 'scout_session_stats', {scope}, MIN(timestamp) FROM events
            WHERE event_type='scout_session_stats' AND json_extract(payload_json,'$.session_id') IS NOT NULL
            GROUP BY {scope}""")
        database.execute(f"""
            DELETE FROM events WHERE event_type='scout_session_stats' AND json_extract(payload_json,'$.session_id') IS NOT NULL
            AND id NOT IN (SELECT MIN(id) FROM events WHERE event_type='scout_session_stats'
                           AND json_extract(payload_json,'$.session_id') IS NOT NULL GROUP BY {scope})""")

    def outcome_store(self):
        from .outcomes import OutcomeStore
        return OutcomeStore(self._connect)

    def event_once(self, event_type: str, event_key: str, payload: dict[str, Any]) -> bool:
        """Idempotent event: written at most once per (event_type, event_key), atomically with its key (v2.3.0 item 4).
        Returns True when written now, False when it already existed. Raises on storage failure so the caller keeps its
        pending state."""
        timestamp = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload, default=json_default, separators=(",", ":"))
        with self._connect() as database:
            cursor = database.execute("INSERT OR IGNORE INTO event_keys(event_type,event_key,created_at) VALUES(?,?,?)",
                                      (event_type, event_key, timestamp))
            if cursor.rowcount != 1:
                return False
            database.execute("INSERT INTO events(timestamp,event_type,payload_json) VALUES(?,?,?)", (timestamp, event_type, encoded))
        return True

    def event(self, event_type: str, payload: dict[str, Any]) -> None:
        timestamp = datetime.now(UTC).isoformat()
        encoded = json.dumps(payload, default=json_default, separators=(",", ":"))
        with self._connect() as database:
            database.execute(
                "INSERT INTO events(timestamp,event_type,payload_json) VALUES(?,?,?)",
                (timestamp, event_type, encoded),
            )

    def snapshot(self, value: AnalysisSnapshot) -> None:
        encoded = json.dumps(value, default=json_default, separators=(",", ":"))
        with self._connect() as database:
            database.execute(
                "INSERT INTO analysis_snapshots(timestamp,symbol,session,action,payload_json) VALUES(?,?,?,?,?)",
                (value.timestamp.isoformat(), value.symbol, value.session.value, value.decision.action.value, encoded),
            )
        path = self.jsonl_path
        if self.rotate_daily:                                                                          # item 39
            path = self.jsonl_path.with_name(f"{self.jsonl_path.stem}_{value.timestamp.strftime('%Y%m%d')}{self.jsonl_path.suffix}")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
        self.prune()

    def prune(self) -> None:
        """Delete snapshots/events older than retention_days and rotated JSONL files past retention (item 39). Runs hourly."""
        import time
        if time.time() - self._last_prune < 3600:
            return
        self._last_prune = time.time()
        cutoff = datetime.now(UTC) - timedelta(days=self.retention_days)
        with self._connect() as database:
            database.execute("DELETE FROM analysis_snapshots WHERE julianday(timestamp) < julianday(?)", (cutoff.isoformat(),))
            database.execute("DELETE FROM events WHERE julianday(timestamp) < julianday(?)", (cutoff.isoformat(),))
        try:
            connection = self._connect(); connection.isolation_level = None; connection.execute("VACUUM"); connection.close()
        except Exception:
            pass
        for f in self.jsonl_path.parent.glob(f"{self.jsonl_path.stem}_*{self.jsonl_path.suffix}"):
            try:
                stamp = datetime.strptime(f.stem.rsplit("_", 1)[1], "%Y%m%d").replace(tzinfo=UTC)
                if stamp < cutoff: f.unlink()
            except Exception:
                pass

    def order(self, order_kind: str, result: Any, payload: dict[str, Any]) -> None:
        encoded = json.dumps({**self.context, **payload}, default=json_default, separators=(",", ":"))
        with self._connect() as database:
            database.execute(
                "INSERT INTO orders(timestamp,order_kind,ticket,success,payload_json) VALUES(?,?,?,?,?)",
                (
                    datetime.now(UTC).isoformat(), order_kind,
                    getattr(result, "ticket", None), int(bool(getattr(result, "success", False))), encoded,
                ),
            )

    def performance_summary(self, start: datetime, end: datetime, session: str | None = None,
                            reference_lot: float = 1.0, daily_target: float = 5.0,
                            monthly_target: float = 1500.0, config_fingerprint: str | None = None,
                            account: str | None = None, symbol: str | None = None) -> dict[str, Any]:
        sql = "SELECT pnl,mfe,mae,duration_s,side,session,volume FROM trades WHERE kind='PA' AND result_confirmed=1 AND julianday(close_time)>=julianday(?) AND julianday(close_time)<julianday(?)"
        args: list[Any] = [start.isoformat(), end.isoformat()]
        if session is not None:
            sql += " AND session=?"; args.append(session)
        if config_fingerprint is not None:                                        # v1.9.0: reports never mix parameter sets
            sql += " AND config_fingerprint=?"; args.append(config_fingerprint)
        if account is not None: sql += " AND account=?"; args.append(account)      # v2.1.0 item 7
        if symbol is not None: sql += " AND symbol=?"; args.append(symbol)
        with self._connect() as database:
            rows = list(database.execute(sql, args))
        pnls = [float(row[0] or 0) for row in rows]
        scaled = [float(row[0] or 0) * reference_lot / max(float(row[6] or reference_lot), 1e-9) for row in rows]
        return {
            "period_start": start.astimezone(UTC).isoformat(), "period_end": end.astimezone(UTC).isoformat(),
            "session": session, "pa_trades": len(rows), "wins": sum(p > 0 for p in pnls),
            "losses": sum(p < 0 for p in pnls), "breakeven": sum(p == 0 for p in pnls),
            "net_pnl": round(sum(pnls), 2), "average_pnl": round(sum(pnls) / len(pnls), 2) if pnls else 0.0,
            "best_trade": round(max(pnls), 2) if pnls else None, "worst_trade": round(min(pnls), 2) if pnls else None,
            "average_duration_s": int(sum(int(row[3] or 0) for row in rows) / len(rows)) if rows else 0,
            "reference_lot": reference_lot, "net_pnl_reference_lot": round(sum(scaled), 2),
            "research_daily_usd": daily_target, "research_daily_progress_pct": round(sum(scaled) / daily_target * 100, 1),
            "research_monthly_usd": monthly_target, "research_monthly_progress_pct": round(sum(scaled) / monthly_target * 100, 1),
            "research_scale": "NORMALISED_RESEARCH_VALUES_NOT_A_TRADING_PERMISSION",
            "config_fingerprint": config_fingerprint, "account": account, "symbol": symbol,
        }

    def scout_performance_summary(self, start: datetime, end: datetime, reference_lot: float = 1.0,
                                  account: str | None = None, config_fingerprint: str | None = None,
                                  symbol: str | None = None) -> dict[str, Any]:
        """Scout statistics; scoped to one account and strategy fingerprint when given (v2.0.0 item 12)."""
        sql = "SELECT timestamp,payload_json FROM events WHERE event_type='scout_session_stats'"; args: list[Any] = []
        if account is not None:
            sql += " AND json_extract(payload_json,'$.account')=?"; args.append(account)
        if symbol is not None:
            sql += " AND json_extract(payload_json,'$.symbol')=?"; args.append(symbol)
        if config_fingerprint is not None:
            sql += " AND json_extract(payload_json,'$.config_fingerprint')=?"; args.append(config_fingerprint)
        sql += " ORDER BY julianday(timestamp)"
        with self._connect() as database:
            events = list(database.execute(sql, args))
            by_session: dict[str, dict[str, Any]] = {}
            correct = compared = 0
            seen_ids: set[str] = set()
            for event_time, encoded in events:
                payload = json.loads(encoded); session = str(payload.get("session", "UNKNOWN"))
                sid = payload.get("session_id")
                if sid:                                                                  # v2.4.0 item 5: one session, one sample
                    if sid in seen_ids: continue
                    seen_ids.add(sid)
                close_marker = as_utc(payload.get("close_time") or event_time)
                if close_marker < start or close_marker >= end:
                    continue
                bucket = by_session.setdefault(session, {"sessions": 0, "buy_mfe": [], "buy_mae": [], "sell_mfe": [], "sell_mae": [], "leaders": {"BUY": 0, "SELL": 0, "NONE": 0}})
                bucket["sessions"] += 1
                for key in ("buy_mfe", "buy_mae", "sell_mfe", "sell_mae"): bucket[key].append(float(payload.get(key, 0) or 0))
                leader = str(payload.get("leader", "NONE")); bucket["leaders"][leader] = bucket["leaders"].get(leader, 0) + 1
                opened = payload.get("open_time") or (as_utc(event_time) - timedelta(hours=12)).isoformat()
                closed = payload.get("close_time") or event_time
                trade_sql = "SELECT side,pnl FROM trades WHERE kind='PA' AND result_confirmed=1 AND session=? AND close_time>=? AND close_time<=?"
                trade_args: list[Any] = [session, opened, closed]
                if account is not None: trade_sql += " AND account=?"; trade_args.append(account)
                if symbol is not None: trade_sql += " AND symbol=?"; trade_args.append(symbol)
                if config_fingerprint is not None: trade_sql += " AND config_fingerprint=?"; trade_args.append(config_fingerprint)
                trades = list(database.execute(trade_sql, trade_args))
                for side, pnl in trades:
                    if leader not in {"BUY", "SELL"}: continue
                    compared += 1
                    leader_side = "LONG" if leader == "BUY" else "SHORT"
                    if (str(side) == leader_side and float(pnl or 0) > 0) or (str(side) != leader_side and float(pnl or 0) < 0): correct += 1
            scout_sql = ("SELECT pnl,volume FROM trades WHERE kind='SCOUT' AND result_confirmed=1 "
                         "AND julianday(close_time)>=julianday(?) AND julianday(close_time)<julianday(?)")
            scout_args: list[Any] = [start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()]
            if account is not None: scout_sql += " AND account=?"; scout_args.append(account)
            if symbol is not None: scout_sql += " AND symbol=?"; scout_args.append(symbol)
            if config_fingerprint is not None: scout_sql += " AND config_fingerprint=?"; scout_args.append(config_fingerprint)
            scout_rows = list(database.execute(scout_sql, scout_args))                     # v2.1.0 item 2
        cleaned = {}
        for session, values in by_session.items():
            cleaned[session] = {"sessions": values["sessions"], "leaders": values["leaders"]}
            for key in ("buy_mfe", "buy_mae", "sell_mfe", "sell_mae"):
                cleaned[session][f"average_{key}"] = round(sum(values[key]) / len(values[key]), 2) if values[key] else 0.0
        scout_pnls = [float(row[0] or 0) for row in scout_rows]
        scout_scaled = [float(row[0] or 0) * reference_lot / max(float(row[1] or reference_lot), 1e-9) for row in scout_rows]
        return {"sessions": cleaned, "leader_compared_trades": compared, "leader_correct_trades": correct,
                "leader_accuracy_pct": round(correct / compared * 100, 1) if compared else None,
                "scout_legs": len(scout_rows), "scout_net_pnl": round(sum(scout_pnls), 2),
                "scout_net_pnl_reference_lot": round(sum(scout_scaled), 2),
                "mirroring_policy": "PROHIBITED_EVIDENCE_ONLY"}

    def report_once(self, report_type: str, report_key: str, payload: dict[str, Any]) -> bool:
        encoded = json.dumps(payload, default=json_default, separators=(",", ":"))
        with self._connect() as database:
            cursor = database.execute(
                "INSERT OR IGNORE INTO generated_reports(report_type,report_key,created_at,payload_json) VALUES(?,?,?,?)",
                (report_type, report_key, datetime.now(UTC).isoformat(), encoded),
            )
            created = cursor.rowcount == 1
        if created:
            self.event(report_type, payload)
        return created

    def reports_between(self, report_type: str, start: datetime, end: datetime, key_field: str = "period_end") -> list[dict[str, Any]]:
        """Payloads of generated reports whose `key_field` timestamp falls in [start, end) (v2.0.0 item 13)."""
        with self._connect() as database:
            rows = list(database.execute("SELECT payload_json FROM generated_reports WHERE report_type=?", (report_type,)))
        result = []
        for (encoded,) in rows:
            payload = json.loads(encoded); marker = payload.get(key_field)
            if not marker: continue
            when = as_utc(marker)
            if start <= when < end: result.append(payload)
        return result

    def report_exists(self, report_type: str, report_key: str) -> bool:
        with self._connect() as database:
            return database.execute(
                "SELECT 1 FROM generated_reports WHERE report_type=? AND report_key=?", (report_type, report_key)
            ).fetchone() is not None

    def event_count(self, event_type: str, start: datetime | None = None, end: datetime | None = None) -> int:
        sql = "SELECT COUNT(*) FROM events WHERE event_type=?"; args: list[Any] = [event_type]
        if start is not None:
            sql += " AND julianday(timestamp)>=julianday(?)"; args.append(start.astimezone(UTC).isoformat())
        if end is not None:
            sql += " AND julianday(timestamp)<julianday(?)"; args.append(end.astimezone(UTC).isoformat())
        with self._connect() as database:
            return int(database.execute(sql, args).fetchone()[0])

    def scoped_event_count(self, event_type: str, account: str, symbol: str, config_fingerprint: str) -> int:
        """Count DISTINCT sessions (by payload.session_id, falling back to row id) whose payload carries exactly this
        account/symbol/fingerprint — a duplicate emission after a crash counts once (v2.2.0 minor 1)."""
        sql = ("SELECT COUNT(DISTINCT COALESCE(json_extract(payload_json,'$.session_id'), 'row:' || id)) FROM events "
               "WHERE event_type=? AND json_extract(payload_json,'$.account')=? "
               "AND json_extract(payload_json,'$.symbol')=? AND json_extract(payload_json,'$.config_fingerprint')=?")
        with self._connect() as database:
            return int(database.execute(sql, (event_type, account, symbol, config_fingerprint)).fetchone()[0])

    def confirmed_trade_count(self, config_fingerprint: str | None = None, account: str | None = None,
                              symbol: str | None = None) -> int:
        """Confirmed PA trades; scoped to fingerprint / account / symbol when given (v2.2.0 item 1)."""
        sql = "SELECT COUNT(*) FROM trades WHERE kind='PA' AND result_confirmed=1"; args: list[Any] = []
        if config_fingerprint is not None:
            sql += " AND config_fingerprint=?"; args.append(config_fingerprint)
        if account is not None:
            sql += " AND account=?"; args.append(account)
        if symbol is not None:
            sql += " AND symbol=?"; args.append(symbol)
        with self._connect() as database:
            return int(database.execute(sql, args).fetchone()[0])


    def trade(self, record: dict[str, Any]) -> None:
        cols = ["ticket", "kind", "side", "session", "magic", "volume", "open_time", "open_price", "close_time", "close_price",
                "sl", "tp", "pnl", "mfe", "mae", "duration_s", "exit_reason", "account", "symbol", "setup_id", "result_confirmed",
                "requested_entry", "actual_entry", "initial_sl", "final_sl", "tp1", "tp2", "tp3", "partial_exits_json",
                "total_realized_pnl", "commission", "swap", "config_fingerprint"]
        record = {**self.context, **record}
        tps = record.get("plan", {}).get("take_profits", [])
        for index in range(3): record.setdefault(f"tp{index + 1}", tps[index] if len(tps) > index else None)
        record.setdefault("partial_exits_json", json.dumps(record.get("partial_exits", []), default=json_default))
        values = [record.get(c) for c in cols]
        values = [v.isoformat() if isinstance(v, datetime) else (v.value if isinstance(v, Enum) else v) for v in values]
        encoded = json.dumps(record, default=json_default, separators=(",", ":"))
        values[cols.index("account")] = values[cols.index("account")] or "unknown"       # v2.3.0 item 1: composite identity never NULL
        values[cols.index("symbol")] = values[cols.index("symbol")] or "unknown"
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("ticket", "account", "symbol")) + ", payload_json=excluded.payload_json"
        with self._connect() as database:
            database.execute(
                f"INSERT INTO trades({','.join(cols)},payload_json) VALUES({','.join('?' * len(cols))},?) "
                f"ON CONFLICT(account,symbol,ticket) DO UPDATE SET {updates}", (*values, encoded))
