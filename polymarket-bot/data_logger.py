"""SQLite trade logging with full audit trail and optimizer state."""

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


class DataLogger:
    def __init__(self, db_path: str = "trades.db", busy_timeout_ms: int = 5000):
        db_file = Path(db_path).expanduser().resolve()
        db_file.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_file)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._configure_connection(busy_timeout_ms)
        self._create_tables()

    def _configure_connection(self, busy_timeout_ms: int):
        with self._lock:
            cursor = self.conn.cursor()
            safe_timeout = max(1000, int(busy_timeout_ms))
            cursor.execute(f"PRAGMA busy_timeout = {safe_timeout}")
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA synchronous = NORMAL")
            cursor.execute("PRAGMA temp_store = MEMORY")
            self.conn.commit()

    def wal_checkpoint(self):
        """WAL 체크포인트 실행 — WAL 파일이 무한정 커지는 것을 방지"""
        with self._lock:
            try:
                self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            except Exception as e:
                logging.warning(f"WAL checkpoint failed: {e}")

    def _execute(self, query: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self.conn.cursor()
            cursor.execute(query, params)
            return cursor

    def _executescript(self, script: str):
        with self._lock:
            self.conn.executescript(script)
            self.conn.commit()

    def _fetchall(self, query: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def _fetchone(self, query: str, params: tuple | list = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(query, params).fetchone()
        return dict(row) if row else None

    def _commit(self):
        with self._lock:
            self.conn.commit()

    def _create_tables(self):
        self._executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_question TEXT,
                side TEXT NOT NULL,
                size REAL NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                pnl REAL,
                signal_values TEXT,
                model_prob REAL,
                market_prob REAL,
                edge REAL,
                kelly_fraction REAL,
                expiry_time TEXT,
                status TEXT DEFAULT 'open',
                order_id TEXT,
                mode TEXT DEFAULT 'paper'
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                date TEXT PRIMARY KEY,
                realized_pnl REAL DEFAULT 0.0,
                num_trades INTEGER DEFAULT 0,
                num_wins INTEGER DEFAULT 0,
                max_drawdown REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS price_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                btc_price REAL NOT NULL,
                rsi REAL,
                bb_upper REAL,
                bb_lower REAL,
                vwap REAL,
                momentum_score REAL
            );

            CREATE TABLE IF NOT EXISTS optimizer_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                mode TEXT NOT NULL,
                profile_name TEXT NOT NULL,
                sample_size INTEGER NOT NULL,
                sample_pnl REAL NOT NULL,
                sample_win_rate REAL NOT NULL,
                active_config TEXT NOT NULL,
                notes TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS idx_trades_status_mode ON trades(status, mode);
            CREATE INDEX IF NOT EXISTS idx_optimizer_timestamp ON optimizer_events(timestamp);
            """
        )
        self._ensure_column("trades", "strategy_name", "TEXT")
        self._ensure_column("trades", "profile_name", "TEXT")
        self._ensure_column("trades", "optimizer_phase", "TEXT")
        self._ensure_column("trades", "market_liquidity", "REAL")
        self._ensure_column("trades", "minutes_to_expiry", "REAL")
        self._ensure_column("trades", "asset_symbol", "TEXT")
        self._ensure_column("trades", "market_group", "TEXT")
        self._ensure_column("trades", "market_duration_min", "REAL")
        self._ensure_column("trades", "mae", "REAL")
        self._ensure_column("trades", "mfe", "REAL")
        self._ensure_column("trades", "ml_prob", "REAL")
        self._ensure_column("trades", "ml_confidence", "REAL")
        self._ensure_column("trades", "exit_target", "REAL")
        self._ensure_column("trades", "token_id", "TEXT")
        self._execute("CREATE INDEX IF NOT EXISTS idx_trades_status_group_mode ON trades(status, market_group, mode)")
        self._execute("UPDATE trades SET asset_symbol = 'BTC' WHERE COALESCE(asset_symbol, '') = ''")
        self._execute("UPDATE trades SET market_group = 'btc_15m' WHERE COALESCE(market_group, '') = ''")
        self._execute("UPDATE trades SET market_duration_min = 15.0 WHERE market_duration_min IS NULL OR market_duration_min <= 0")
        self._commit()

    def _ensure_column(self, table: str, column: str, column_type: str):
        columns = {row[1] for row in self._execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            self._execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            self._commit()

    def log_trade(
        self,
        market_id: str,
        side: str,
        size: float,
        entry_price: float,
        signal_values: dict,
        model_prob: float,
        market_prob: float,
        edge: float,
        kelly_fraction: float,
        expiry_time: str,
        market_question: str = "",
        order_id: str = "",
        mode: str = "paper",
        strategy_name: str = "",
        profile_name: str = "",
        optimizer_phase: str = "",
        market_liquidity: float = 0.0,
        minutes_to_expiry: float = 0.0,
        asset_symbol: str = "BTC",
        market_group: str = "btc_15m",
        market_duration_min: float = 15.0,
        token_id: str = "",
    ) -> int:
        try:
            cursor = self._execute(
                """
                INSERT INTO trades (
                    timestamp, market_id, market_question, side, size,
                    entry_price, signal_values, model_prob, market_prob,
                    edge, kelly_fraction, expiry_time, status, order_id, mode,
                    strategy_name, profile_name, optimizer_phase, market_liquidity,
                    minutes_to_expiry, asset_symbol, market_group, market_duration_min,
                    token_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    market_id,
                    market_question,
                    side,
                    size,
                    entry_price,
                    json.dumps(signal_values, sort_keys=True),
                    model_prob,
                    market_prob,
                    edge,
                    kelly_fraction,
                    expiry_time,
                    order_id,
                    mode,
                    strategy_name,
                    profile_name,
                    optimizer_phase,
                    market_liquidity,
                    minutes_to_expiry,
                    asset_symbol,
                    market_group,
                    market_duration_min,
                    token_id,
                ),
            )
            self._commit()
            return int(cursor.lastrowid)
        except Exception as exc:
            logging.error(f"[DB] log_trade failed: {exc}", exc_info=True)
            return -1

    def update_trade_ml_data(self, trade_id: int, ml_prob: float, ml_confidence: float, exit_target: float):
        self._execute(
            "UPDATE trades SET ml_prob = ?, ml_confidence = ?, exit_target = ? WHERE id = ?",
            (ml_prob, ml_confidence, exit_target, trade_id),
        )
        self._commit()

    def update_trade_mae_mfe(self, trade_id: int, mae: float, mfe: float):
        self._execute(
            "UPDATE trades SET mae = ?, mfe = ? WHERE id = ?",
            (mae, mfe, trade_id),
        )
        self._commit()

    def close_trade(self, trade_id: int, exit_price: float, pnl: float, update_daily: bool = True):
        try:
            self._execute(
                "UPDATE trades SET exit_price = ?, pnl = ?, status = 'closed' WHERE id = ?",
                (exit_price, pnl, trade_id),
            )
            self._commit()
            if update_daily:
                self._update_daily_pnl(pnl)
        except Exception as exc:
            logging.error(f"[DB] close_trade #{trade_id} failed: {exc}", exc_info=True)

    def _update_daily_pnl(self, pnl: float):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._fetchone("SELECT * FROM daily_pnl WHERE date = ?", (today,))
        if row:
            new_pnl = float(row["realized_pnl"] or 0.0) + pnl
            new_trades = int(row["num_trades"] or 0) + 1
            new_wins = int(row["num_wins"] or 0) + (1 if pnl > 0 else 0)
            new_dd = min(float(row["max_drawdown"] or 0.0), new_pnl)
            self._execute(
                "UPDATE daily_pnl SET realized_pnl = ?, num_trades = ?, num_wins = ?, max_drawdown = ? WHERE date = ?",
                (new_pnl, new_trades, new_wins, new_dd, today),
            )
        else:
            self._execute(
                "INSERT INTO daily_pnl (date, realized_pnl, num_trades, num_wins, max_drawdown) VALUES (?, ?, 1, ?, ?)",
                (today, pnl, 1 if pnl > 0 else 0, min(0, pnl)),
            )
        self._commit()

    def _apply_trade_filters(
        self,
        query: str,
        params: list,
        mode: Optional[str],
        market_group: Optional[str],
        asset_symbol: Optional[str],
        include_shadow: bool,
    ) -> tuple[str, list]:
        if not include_shadow:
            query += " AND mode <> 'shadow'"
        if mode is not None:
            query += " AND mode = ?"
            params.append(mode)
        if market_group is not None:
            query += " AND COALESCE(market_group, '') = ?"
            params.append(market_group)
        if asset_symbol is not None:
            query += " AND COALESCE(asset_symbol, '') = ?"
            params.append(asset_symbol)
        return query, params

    def get_open_trades(self, mode: Optional[str] = None, market_group: Optional[str] = None, asset_symbol: Optional[str] = None, strategy_name: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM trades WHERE status = 'open'"
        params: list = []
        query, params = self._apply_trade_filters(query, params, mode, market_group, asset_symbol, include_shadow=True)
        if strategy_name:
            query += " AND strategy_name = ?"
            params.append(strategy_name)
        query += " ORDER BY timestamp DESC"
        return self._fetchall(query, params)

    def count_open_trades(self, mode: Optional[str] = None, market_group: Optional[str] = None, asset_symbol: Optional[str] = None) -> int:
        query = "SELECT COUNT(*) AS c FROM trades WHERE status = 'open'"
        params: list = []
        query, params = self._apply_trade_filters(query, params, mode, market_group, asset_symbol, include_shadow=True)
        row = self._fetchone(query, params)
        return int((row or {}).get("c") or 0)

    def get_daily_pnl(self, date: Optional[str] = None) -> dict:
        if date is None:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._fetchone("SELECT * FROM daily_pnl WHERE date = ?", (date,))
        if row:
            return row
        return {"date": date, "realized_pnl": 0.0, "num_trades": 0, "num_wins": 0, "max_drawdown": 0.0}

    def get_today_realized_pnl(self, mode: Optional[str] = None, market_group: Optional[str] = None) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        query = "SELECT SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) AS total_pnl FROM trades WHERE status = 'closed' AND substr(timestamp, 1, 10) = ?"
        params: list = [today]
        query, params = self._apply_trade_filters(query, params, mode, market_group, None, include_shadow=(mode == 'shadow'))
        row = self._fetchone(query, params)
        return float((row or {}).get("total_pnl") or 0.0)

    def get_cumulative_realized_pnl(self, mode: Optional[str] = None) -> float:
        """Get total realized PnL across all time (not just today)."""
        query = "SELECT SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) AS total_pnl FROM trades WHERE status = 'closed'"
        params: list = []
        query, params = self._apply_trade_filters(query, params, mode, None, None, include_shadow=(mode == 'shadow'))
        row = self._fetchone(query, params)
        return float((row or {}).get("total_pnl") or 0.0)

    def get_closed_trades(
        self,
        mode: Optional[str] = None,
        limit: Optional[int] = None,
        market_group: Optional[str] = None,
        asset_symbol: Optional[str] = None,
        include_shadow: bool = True,
    ) -> list[dict]:
        query = "SELECT * FROM trades WHERE status = 'closed'"
        params: list = []
        query, params = self._apply_trade_filters(query, params, mode, market_group, asset_symbol, include_shadow=include_shadow)
        query += " ORDER BY timestamp DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        return self._fetchall(query, params)

    def get_closed_trades_for_today(
        self,
        mode: Optional[str] = None,
        market_group: Optional[str] = None,
        asset_symbol: Optional[str] = None,
        include_shadow: bool = True,
    ) -> list[dict]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        query = "SELECT * FROM trades WHERE status = 'closed' AND substr(timestamp, 1, 10) = ?"
        params: list = [today]
        query, params = self._apply_trade_filters(query, params, mode, market_group, asset_symbol, include_shadow=include_shadow)
        query += " ORDER BY timestamp DESC"
        return self._fetchall(query, params)

    def get_session_stats(self, mode: Optional[str] = None, include_shadow: bool = False, market_group: Optional[str] = None) -> dict:
        query = """
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) as total_pnl,
                   AVG(CASE WHEN pnl IS NOT NULL THEN pnl END) as avg_pnl
            FROM trades WHERE status = 'closed'
        """
        params: list = []
        query, params = self._apply_trade_filters(query, params, mode, market_group, None, include_shadow=include_shadow)
        row = self._fetchone(query, params) or {}
        total = int(row.get("total") or 0)
        wins = int(row.get("wins") or 0)
        return {
            "total_trades": total,
            "wins": wins,
            "win_rate": wins / total if total > 0 else 0.0,
            "total_pnl": float(row.get("total_pnl") or 0.0),
            "avg_pnl": float(row.get("avg_pnl") or 0.0),
        }

    def log_price_snapshot(self, btc_price: float, rsi: float, bb_upper: float, bb_lower: float, vwap: float, momentum_score: float):
        self._execute(
            "INSERT INTO price_snapshots (timestamp, btc_price, rsi, bb_upper, bb_lower, vwap, momentum_score) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), btc_price, rsi, bb_upper, bb_lower, vwap, momentum_score),
        )
        self._commit()

    def log_optimizer_event(self, mode: str, profile_name: str, sample_size: int, sample_pnl: float, sample_win_rate: float, active_config: dict, notes: str = ""):
        self._execute(
            "INSERT INTO optimizer_events (timestamp, mode, profile_name, sample_size, sample_pnl, sample_win_rate, active_config, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(),
                mode,
                profile_name,
                sample_size,
                sample_pnl,
                sample_win_rate,
                json.dumps(active_config, sort_keys=True),
                notes,
            ),
        )
        self._commit()

    def get_recent_optimizer_events(self, limit: int = 10) -> list[dict]:
        rows = self._fetchall("SELECT * FROM optimizer_events ORDER BY timestamp DESC LIMIT ?", (limit,))
        for row in rows:
            try:
                row["active_config"] = json.loads(row["active_config"])
            except Exception:
                row["active_config"] = {}
        return rows

    def _grouped_performance_query(self, select_prefix: str, group_by: str, mode: str, market_group: Optional[str] = None) -> list[dict]:
        query = f"""
            SELECT {select_prefix},
                   COUNT(*) AS trade_count,
                   SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) AS total_pnl,
                   AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   AVG(CASE WHEN pnl IS NOT NULL THEN pnl END) AS avg_pnl
            FROM trades
            WHERE status = 'closed' AND mode = ?
        """
        params: list = [mode]
        if market_group is not None:
            query += " AND COALESCE(market_group, '') = ?"
            params.append(market_group)
        query += f" GROUP BY {group_by} ORDER BY total_pnl DESC, trade_count DESC"
        return self._fetchall(query, params)

    def get_profile_performance(self, mode: str = "paper", market_group: Optional[str] = None) -> list[dict]:
        return self._grouped_performance_query("COALESCE(profile_name, '') AS profile_name", "COALESCE(profile_name, '')", mode, market_group)

    def get_strategy_performance(self, mode: str = "paper", market_group: Optional[str] = None) -> list[dict]:
        return self._grouped_performance_query("COALESCE(strategy_name, '') AS strategy_name", "COALESCE(strategy_name, '')", mode, market_group)

    def get_strategy_side_performance(self, mode: str = "paper", market_group: Optional[str] = None) -> list[dict]:
        query = """
            SELECT COALESCE(strategy_name, '') AS strategy_name,
                   COALESCE(side, '') AS side,
                   COUNT(*) AS trade_count,
                   SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) AS total_pnl,
                   AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   AVG(CASE WHEN pnl IS NOT NULL THEN pnl END) AS avg_pnl
            FROM trades
            WHERE status = 'closed' AND mode = ?
        """
        params: list = [mode]
        if market_group is not None:
            query += " AND COALESCE(market_group, '') = ?"
            params.append(market_group)
        query += " GROUP BY COALESCE(strategy_name, ''), COALESCE(side, '') ORDER BY total_pnl DESC, trade_count DESC"
        return self._fetchall(query, params)

    def get_market_group_performance(self, mode: str = "paper") -> list[dict]:
        return self._fetchall(
            """
            SELECT COALESCE(market_group, '') AS market_group,
                   COALESCE(asset_symbol, '') AS asset_symbol,
                   COUNT(*) AS trade_count,
                   SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) AS total_pnl,
                   AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   AVG(CASE WHEN pnl IS NOT NULL THEN pnl END) AS avg_pnl
            FROM trades
            WHERE status = 'closed' AND mode = ?
            GROUP BY COALESCE(market_group, ''), COALESCE(asset_symbol, '')
            ORDER BY total_pnl DESC, trade_count DESC
            """,
            (mode,),
        )

    def get_market_group_side_performance(self, mode: str = "shadow") -> list[dict]:
        return self._fetchall(
            """
            SELECT COALESCE(market_group, '') AS market_group,
                   COALESCE(asset_symbol, '') AS asset_symbol,
                   COALESCE(side, '') AS side,
                   COUNT(*) AS trade_count,
                   SUM(CASE WHEN pnl IS NOT NULL THEN pnl ELSE 0 END) AS total_pnl,
                   AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
                   AVG(CASE WHEN pnl IS NOT NULL THEN pnl END) AS avg_pnl
            FROM trades
            WHERE status = 'closed' AND mode = ?
            GROUP BY COALESCE(market_group, ''), COALESCE(asset_symbol, ''), COALESCE(side, '')
            ORDER BY total_pnl DESC, trade_count DESC
            """,
            (mode,),
        )
    def get_all_closed_trades(self, include_shadow: bool = False, market_group: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM trades WHERE status = 'closed'"
        params: list = []
        if not include_shadow:
            query += " AND mode <> 'shadow'"
        if market_group is not None:
            query += " AND COALESCE(market_group, '') = ?"
            params.append(market_group)
        query += " ORDER BY timestamp"
        return self._fetchall(query, params)

    def close(self):
        with self._lock:
            self.conn.close()

