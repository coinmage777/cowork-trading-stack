"""Read-only web dashboard server for paper/shadow monitoring."""

import json
import mimetypes
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from config import Config


@dataclass
class DashboardRepository:
    db_path: Path
    log_path: Path
    optimizer_state_path: Path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _minutes_ago(ts: datetime | None, now: datetime) -> int | None:
    if ts is None:
        return None
    diff = now - ts.astimezone(timezone.utc)
    return max(0, int(diff.total_seconds() // 60))


def _profit_factor(pnls: list[float]) -> float:
    gross_profit = sum(x for x in pnls if x > 0)
    gross_loss = abs(sum(x for x in pnls if x < 0))
    if gross_loss > 0:
        return gross_profit / gross_loss
    return 999.0 if gross_profit > 0 else 0.0


def _parse_guard_log_timestamp(line: str, base_time: datetime) -> datetime | None:
    try:
        prefix = line[:8]
        hh, mm, ss = [int(x) for x in prefix.split(":")]
    except Exception:
        return None
    candidate = base_time.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    # Handle midnight rollover for date-less log lines.
    if candidate - base_time > timedelta(hours=4):
        candidate -= timedelta(days=1)
    elif base_time - candidate > timedelta(days=1, hours=20):
        candidate += timedelta(days=1)
    return candidate


def _status_by_age(minutes: int | None, warning_min: int, critical_min: int, ok: str, warning: str, critical: str) -> dict:
    if minutes is None:
        return {"status": "critical", "message": "No timestamp available.", "age_minutes": None}
    if minutes >= critical_min:
        return {"status": "critical", "message": critical.format(minutes=minutes), "age_minutes": minutes}
    if minutes >= warning_min:
        return {"status": "warning", "message": warning.format(minutes=minutes), "age_minutes": minutes}
    return {"status": "ok", "message": ok.format(minutes=minutes), "age_minutes": minutes}


def _status_rank(status: str) -> int:
    if status == "critical":
        return 2
    if status == "warning":
        return 1
    return 0


def _append_unique(messages: list[str], message: str) -> None:
    clean = (message or "").strip()
    if clean and clean not in messages:
        messages.append(clean)


def _default_asset_for_group(group: str) -> str:
    text = (group or "").strip().lower()
    if text.startswith("btc_"):
        return "BTC"
    if text.startswith("eth_"):
        return "ETH"
    return ""


def _normalize_group_rows(rows: list[sqlite3.Row], active_groups: list[str]) -> list[dict]:
    normalized: dict[str, dict] = {}
    for row in rows:
        group = (row["market_group"] or "").strip()
        if not group:
            continue
        normalized[group] = {
            "market_group": group,
            "asset_symbol": (row["asset_symbol"] or "").strip() or _default_asset_for_group(group),
            "trade_count": _safe_int(row["trade_count"]),
            "win_rate": _safe_float(row["win_rate"]),
            "avg_pnl": _safe_float(row["avg_pnl"]),
            "total_pnl": _safe_float(row["total_pnl"]),
        }
    for group in active_groups:
        if group not in normalized:
            normalized[group] = {
                "market_group": group,
                "asset_symbol": _default_asset_for_group(group),
                "trade_count": 0,
                "win_rate": 0.0,
                "avg_pnl": 0.0,
                "total_pnl": 0.0,
            }
    return [normalized[group] for group in active_groups if group in normalized]


def build_payload(cfg: Config, repo: DashboardRepository) -> dict:
    now = _utc_now()
    today_utc = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_key = today_utc.strftime("%Y-%m-%d")

    with repo.connect() as conn:
        overall_paper = conn.execute(
            """
            SELECT COUNT(*) AS trade_count,
                   COALESCE(SUM(pnl), 0) AS total_pnl,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate
            FROM trades
            WHERE status='closed' AND mode='paper'
            """
        ).fetchone()

        today_paper = conn.execute(
            """
            SELECT COUNT(*) AS trade_count,
                   COALESCE(SUM(pnl), 0) AS total_pnl,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate
            FROM trades
            WHERE status='closed' AND mode='paper' AND timestamp >= ?
            """,
            (today_utc.isoformat(),),
        ).fetchone()
        reference_today_paper = conn.execute(
            """
            SELECT COUNT(*) AS trade_count,
                   COALESCE(SUM(pnl), 0) AS total_pnl,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate
            FROM trades
            WHERE status='closed' AND mode='paper'
              AND COALESCE(market_group,'') = ?
              AND timestamp >= ?
            """,
            (cfg.performance_reference_group, today_utc.isoformat()),
        ).fetchone()

        non_shadow_today = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) AS total_pnl
            FROM trades
            WHERE status='closed' AND mode <> 'shadow' AND timestamp >= ?
            """,
            (today_utc.isoformat(),),
        ).fetchone()

        recent_ref = conn.execute(
            """
            SELECT pnl, timestamp
            FROM trades
            WHERE status='closed' AND mode='paper' AND COALESCE(market_group,'') = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (cfg.performance_reference_group, cfg.paper_live_gate_min_trades),
        ).fetchall()

        recent_paper_rows = conn.execute(
            """
            SELECT id, timestamp, market_question, strategy_name, side, entry_price, exit_price, pnl, market_group, mode
            FROM trades
            WHERE status='closed' AND mode='paper'
            ORDER BY timestamp DESC
            LIMIT 40
            """
        ).fetchall()

        open_rows = conn.execute(
            """
            SELECT id, timestamp, market_question, side, size, entry_price, expiry_time, mode, market_group
            FROM trades
            WHERE status='open'
            ORDER BY timestamp DESC
            LIMIT 40
            """
        ).fetchall()

        mode_breakdown = conn.execute(
            """
            SELECT mode, COUNT(*) AS count, COALESCE(SUM(pnl),0) AS pnl
            FROM trades
            WHERE status='closed'
            GROUP BY mode
            ORDER BY mode
            """
        ).fetchall()

        strategy_rows = conn.execute(
            """
            SELECT COALESCE(strategy_name, '') AS strategy_name,
                   COUNT(*) AS trade_count,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate,
                   COALESCE(AVG(pnl), 0) AS avg_pnl,
                   COALESCE(SUM(pnl), 0) AS total_pnl
            FROM trades
            WHERE status='closed' AND mode='paper'
            GROUP BY COALESCE(strategy_name, '')
            ORDER BY total_pnl DESC, trade_count DESC
            LIMIT 12
            """
        ).fetchall()

        profile_rows = conn.execute(
            """
            SELECT COALESCE(profile_name, '') AS profile_name,
                   COUNT(*) AS trade_count,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate,
                   COALESCE(AVG(pnl), 0) AS avg_pnl,
                   COALESCE(SUM(pnl), 0) AS total_pnl
            FROM trades
            WHERE status='closed' AND mode='paper'
            GROUP BY COALESCE(profile_name, '')
            ORDER BY total_pnl DESC, trade_count DESC
            LIMIT 12
            """
        ).fetchall()

        group_rows = conn.execute(
            """
            SELECT COALESCE(market_group, '') AS market_group,
                   COALESCE(asset_symbol, '') AS asset_symbol,
                   COUNT(*) AS trade_count,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate,
                   COALESCE(AVG(pnl), 0) AS avg_pnl,
                   COALESCE(SUM(pnl), 0) AS total_pnl
            FROM trades
            WHERE status='closed' AND mode='paper'
            GROUP BY COALESCE(market_group, ''), COALESCE(asset_symbol, '')
            ORDER BY total_pnl DESC, trade_count DESC
            """
        ).fetchall()

        shadow_group_rows = conn.execute(
            """
            SELECT COALESCE(market_group, '') AS market_group,
                   COALESCE(asset_symbol, '') AS asset_symbol,
                   COUNT(*) AS trade_count,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END), 0) AS win_rate,
                   COALESCE(AVG(pnl), 0) AS avg_pnl,
                   COALESCE(SUM(pnl), 0) AS total_pnl
            FROM trades
            WHERE status='closed' AND mode='shadow'
            GROUP BY COALESCE(market_group, ''), COALESCE(asset_symbol, '')
            ORDER BY total_pnl DESC, trade_count DESC
            """
        ).fetchall()

        daily_rows = conn.execute(
            """
            SELECT substr(timestamp, 1, 10) AS date,
                   COALESCE(SUM(pnl), 0) AS realized_pnl,
                   COUNT(*) AS trade_count,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END),0) AS win_rate
            FROM trades
            WHERE status='closed' AND mode='paper'
            GROUP BY substr(timestamp, 1, 10)
            ORDER BY date DESC
            LIMIT 14
            """
        ).fetchall()

        hourly_rows_raw = conn.execute(
            """
            SELECT strftime('%H:00', timestamp) AS label,
                   COUNT(*) AS trade_count,
                   COALESCE(SUM(pnl),0) AS pnl,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END),0) AS win_rate
            FROM trades
            WHERE status='closed' AND mode='paper'
            GROUP BY strftime('%H', timestamp)
            ORDER BY label ASC
            """
        ).fetchall()

        all_paper = conn.execute(
            """
            SELECT timestamp, pnl, COALESCE(strategy_name, '') AS strategy_name
            FROM trades
            WHERE status='closed' AND mode='paper'
            ORDER BY timestamp ASC
            """
        ).fetchall()

        optimizer_events = conn.execute(
            """
            SELECT timestamp, profile_name, sample_size, sample_pnl, sample_win_rate, notes
            FROM optimizer_events
            WHERE mode='paper'
            ORDER BY timestamp DESC
            LIMIT 8
            """
        ).fetchall()

        blocked_scope = conn.execute(
            """
            SELECT COUNT(*) AS closed_count, COALESCE(SUM(pnl),0) AS closed_pnl
            FROM trades
            WHERE status='closed' AND mode='paper'
              AND strategy_name='deviation' AND COALESCE(market_group,'') <> ?
            """,
            (cfg.performance_reference_group,),
        ).fetchone()
        blocked_scope_open = conn.execute(
            """
            SELECT COUNT(*) AS open_count
            FROM trades
            WHERE status='open' AND mode='paper'
              AND strategy_name='deviation' AND COALESCE(market_group,'') <> ?
            """,
            (cfg.performance_reference_group,),
        ).fetchone()
        blocked_scope_recent = conn.execute(
            """
            SELECT COUNT(*) AS recent_closed_count, COALESCE(SUM(pnl),0) AS recent_closed_pnl
            FROM trades
            WHERE status='closed' AND mode='paper'
              AND strategy_name='deviation' AND COALESCE(market_group,'') <> ?
              AND datetime(timestamp) >= datetime('now', '-6 hours')
            """,
            (cfg.performance_reference_group,),
        ).fetchone()

        shadow_open = conn.execute(
            """
            SELECT COUNT(*) AS open_count,
                   COALESCE(SUM(size),0) AS open_size,
                   MIN(timestamp) AS oldest_open_ts
            FROM trades
            WHERE status='open' AND mode='shadow'
            """
        ).fetchone()

        shadow_closed = conn.execute(
            """
            SELECT COUNT(*) AS closed_count,
                   COALESCE(SUM(pnl),0) AS pnl,
                   COALESCE(AVG(CASE WHEN pnl > 0 THEN 1.0 ELSE 0.0 END),0) AS win_rate,
                   MAX(timestamp) AS last_closed_ts
            FROM trades
            WHERE status='closed' AND mode='shadow'
            """
        ).fetchone()

        last_paper_close = conn.execute(
            "SELECT MAX(timestamp) AS ts FROM trades WHERE status='closed' AND mode='paper'"
        ).fetchone()
        last_trade_activity = conn.execute("SELECT MAX(timestamp) AS ts FROM trades").fetchone()
    recent_ref_pnls = [_safe_float(row["pnl"]) for row in recent_ref]
    sample_last_trade_ts = _parse_iso(recent_ref[0]["timestamp"]) if recent_ref else None
    sample_age_minutes = _minutes_ago(sample_last_trade_ts, now)
    gate_sample_monitor = _status_by_age(
        sample_age_minutes,
        warning_min=180,
        critical_min=720,
        ok="Paper-gate sample refreshed {minutes}m ago.",
        warning="Paper-gate sample is {minutes}m old.",
        critical="Paper-gate sample is {minutes}m old (stale historical sample).",
    )
    gate_sample_monitor["timestamp_utc"] = sample_last_trade_ts.isoformat() if sample_last_trade_ts else None
    sample_size = len(recent_ref_pnls)
    sample_pnl = sum(recent_ref_pnls)
    sample_win_rate = (sum(1 for x in recent_ref_pnls if x > 0) / sample_size) if sample_size else 0.0
    sample_avg_pnl = (sample_pnl / sample_size) if sample_size else 0.0
    sample_pf = _profit_factor(recent_ref_pnls)
    passes_win_rate = sample_win_rate >= cfg.paper_live_gate_min_win_rate
    passes_profit_factor = sample_pf >= cfg.paper_live_gate_min_profit_factor and sample_avg_pnl > 0
    paper_gate_ready = (
        sample_size >= cfg.paper_live_gate_min_trades
        and sample_pnl >= cfg.paper_live_gate_min_pnl
        and (passes_win_rate or passes_profit_factor)
    )

    today_non_shadow_pnl = _safe_float(non_shadow_today["total_pnl"])
    effective_pnl = cfg.recovery_start_pnl + today_non_shadow_pnl
    recovery_locked = cfg.recovery_mode_enabled and effective_pnl < cfg.live_resume_profit_target
    remaining_unlock = max(0.0, cfg.live_resume_profit_target - effective_pnl)
    baseline = cfg.live_resume_profit_target - cfg.recovery_start_pnl
    progress = 1.0 if baseline <= 0 else max(0.0, min(1.0, (effective_pnl - cfg.recovery_start_pnl) / baseline))

    live_ready = (not recovery_locked) and (not cfg.paper_live_gate_enabled or paper_gate_ready)

    optimizer_state = {}
    if repo.optimizer_state_path.exists():
        try:
            optimizer_state = json.loads(repo.optimizer_state_path.read_text(encoding="utf-8"))
        except Exception:
            optimizer_state = {}

    optimizer_status = optimizer_state.get("status") if isinstance(optimizer_state, dict) else {}
    if not isinstance(optimizer_status, dict):
        optimizer_status = {}
    optimizer_updated = _parse_iso(optimizer_state.get("updated_at_utc")) if isinstance(optimizer_state, dict) else None
    latest_optimizer_event_ts = _parse_iso(optimizer_events[0]["timestamp"]) if optimizer_events else None

    last_paper_ts = _parse_iso(last_paper_close["ts"] if last_paper_close else None)
    last_trade_activity_ts = _parse_iso(last_trade_activity["ts"] if last_trade_activity else None)
    last_shadow_closed_ts = _parse_iso(shadow_closed["last_closed_ts"] if shadow_closed else None)
    oldest_shadow_open_ts = _parse_iso(shadow_open["oldest_open_ts"] if shadow_open else None)

    optimizer_state_monitor = _status_by_age(
        _minutes_ago(optimizer_updated, now),
        warning_min=30,
        critical_min=120,
        ok="Optimizer state refreshed {minutes}m ago.",
        warning="Optimizer state age is {minutes}m (stale).",
        critical="Optimizer state age is {minutes}m (critical stale).",
    )
    optimizer_event_monitor = _status_by_age(
        _minutes_ago(latest_optimizer_event_ts, now),
        warning_min=30,
        critical_min=120,
        ok="Optimizer events updated {minutes}m ago.",
        warning="Optimizer events are {minutes}m old (stale).",
        critical="Optimizer events are {minutes}m old (optimizer loop likely stalled).",
    )
    optimizer_event_monitor["timestamp_utc"] = latest_optimizer_event_ts.isoformat() if latest_optimizer_event_ts else None
    optimizer_today_pnl = _safe_float(optimizer_status.get("today_paper_pnl", 0.0))
    optimizer_today_trades = _safe_int(optimizer_status.get("today_closed_trades", 0))
    reference_today_pnl = _safe_float(reference_today_paper["total_pnl"] if reference_today_paper else 0.0)
    reference_today_trades = _safe_int(reference_today_paper["trade_count"] if reference_today_paper else 0)
    optimizer_today_pnl_delta = optimizer_today_pnl - reference_today_pnl
    optimizer_today_trade_delta = optimizer_today_trades - reference_today_trades
    optimizer_reference_consistency_monitor = {
        "status": "ok",
        "reference_group": cfg.performance_reference_group,
        "optimizer_today_pnl": optimizer_today_pnl,
        "db_reference_today_pnl": reference_today_pnl,
        "optimizer_today_closed_trades": optimizer_today_trades,
        "db_reference_today_closed_trades": reference_today_trades,
        "pnl_delta": optimizer_today_pnl_delta,
        "trade_delta": optimizer_today_trade_delta,
        "message": "Optimizer today metrics align with DB reference-group today metrics.",
    }
    if abs(optimizer_today_pnl_delta) >= 1.0 or abs(optimizer_today_trade_delta) >= 2:
        optimizer_reference_consistency_monitor["status"] = "critical"
        optimizer_reference_consistency_monitor["message"] = (
            f"Optimizer/DB reference-day drift is high: pnl_delta={optimizer_today_pnl_delta:+.2f}, "
            f"trade_delta={optimizer_today_trade_delta:+d}."
        )
    elif abs(optimizer_today_pnl_delta) >= 0.25 or abs(optimizer_today_trade_delta) >= 1:
        optimizer_reference_consistency_monitor["status"] = "warning"
        optimizer_reference_consistency_monitor["message"] = (
            f"Optimizer/DB reference-day drift detected: pnl_delta={optimizer_today_pnl_delta:+.2f}, "
            f"trade_delta={optimizer_today_trade_delta:+d}."
        )

    bot_log_mtime = datetime.fromtimestamp(repo.log_path.stat().st_mtime, tz=timezone.utc) if repo.log_path.exists() else None
    bot_log_monitor = _status_by_age(
        _minutes_ago(bot_log_mtime, now),
        warning_min=30,
        critical_min=120,
        ok="bot.log heartbeat updated {minutes}m ago.",
        warning="bot.log heartbeat is {minutes}m old.",
        critical="bot.log heartbeat is {minutes}m old (runtime likely stalled).",
    )
    db_mtime = datetime.fromtimestamp(repo.db_path.stat().st_mtime, tz=timezone.utc) if repo.db_path.exists() else None
    db_heartbeat_ts = last_trade_activity_ts or db_mtime
    db_heartbeat_source = "trades.max_timestamp" if last_trade_activity_ts else "file_mtime"
    db_monitor = _status_by_age(
        _minutes_ago(db_heartbeat_ts, now),
        warning_min=30,
        critical_min=120,
        ok="trades.db trade-data heartbeat updated {minutes}m ago.",
        warning="trades.db trade-data heartbeat is {minutes}m old.",
        critical="trades.db trade-data heartbeat is {minutes}m old (trade persistence may be stalled).",
    )
    db_monitor["source"] = db_heartbeat_source
    db_monitor["timestamp_utc"] = db_heartbeat_ts.isoformat() if db_heartbeat_ts else None

    latest_guard_line = ""
    latest_guard_ts = None
    runtime_restart_monitor = {
        "status": "ok",
        "restart_count_recent_window": 0,
        "recent_window_lines": 0,
        "last_restart_log_time": None,
        "message": "No unusual bot restart churn in recent logs.",
    }
    if repo.log_path.exists():
        try:
            lines = repo.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            recent_log_lines = lines[-4000:]
            runtime_restart_monitor["recent_window_lines"] = len(recent_log_lines)
            for line in reversed(recent_log_lines):
                if "[GUARD]" in line:
                    latest_guard_line = line
                    latest_guard_ts = _parse_guard_log_timestamp(line, now)
                    break
            restart_lines = [line for line in recent_log_lines if "Starting bot in PAPER mode" in line]
            restart_count = len(restart_lines)
            runtime_restart_monitor["restart_count_recent_window"] = restart_count
            if restart_lines:
                runtime_restart_monitor["last_restart_log_time"] = restart_lines[-1][:8]
            if restart_count >= 4:
                runtime_restart_monitor["status"] = "critical"
                runtime_restart_monitor["message"] = (
                    f"Bot restarted {restart_count} times in the recent {len(recent_log_lines)} log lines."
                )
            elif restart_count >= 2:
                runtime_restart_monitor["status"] = "warning"
                runtime_restart_monitor["message"] = (
                    f"Bot restarted {restart_count} times in the recent {len(recent_log_lines)} log lines."
                )
        except Exception:
            latest_guard_line = ""
            latest_guard_ts = None

    guard_ts_clamped = False
    if latest_guard_ts and bot_log_mtime and latest_guard_ts > bot_log_mtime:
        latest_guard_ts = bot_log_mtime
        guard_ts_clamped = True

    runtime_guard_monitor = _status_by_age(
        _minutes_ago(latest_guard_ts, now),
        warning_min=30,
        critical_min=120,
        ok="[GUARD] heartbeat seen {minutes}m ago.",
        warning="[GUARD] heartbeat is {minutes}m old.",
        critical="[GUARD] heartbeat is {minutes}m old (runtime stale).",
    )
    runtime_guard_monitor["timestamp_clamped_to_log_mtime"] = guard_ts_clamped
    freshness_inputs = [optimizer_state_monitor, optimizer_event_monitor, bot_log_monitor, db_monitor, runtime_guard_monitor]
    critical_count = sum(1 for m in freshness_inputs if _status_rank(m.get("status", "ok")) >= 2)
    warning_count = sum(1 for m in freshness_inputs if _status_rank(m.get("status", "ok")) == 1)
    if critical_count >= 2:
        system_staleness_monitor = {
            "status": "critical",
            "critical_count": critical_count,
            "warning_count": warning_count,
            "message": f"System freshness is critical: {critical_count} core heartbeats are stale.",
        }
    elif critical_count == 1 or warning_count >= 2:
        system_staleness_monitor = {
            "status": "warning",
            "critical_count": critical_count,
            "warning_count": warning_count,
            "message": f"System freshness warning: {critical_count} critical and {warning_count} warning heartbeat checks.",
        }
    else:
        system_staleness_monitor = {
            "status": "ok",
            "critical_count": critical_count,
            "warning_count": warning_count,
            "message": "Core runtime/data heartbeats are fresh.",
        }

    runtime_stall_monitor = {
        "status": "ok",
        "critical_count": critical_count,
        "core_checks": len(freshness_inputs),
        "message": "Runtime heartbeat coverage looks healthy.",
    }
    if critical_count >= len(freshness_inputs):
        runtime_stall_monitor["status"] = "critical"
        runtime_stall_monitor["message"] = (
            "All core heartbeats are critically stale; trading runtime is likely stopped."
        )
    elif critical_count >= 3:
        runtime_stall_monitor["status"] = "warning"
        runtime_stall_monitor["message"] = (
            f"{critical_count} core heartbeats are critically stale; runtime may be partially stalled."
        )

    blocked_count = _safe_int(blocked_scope["closed_count"] if blocked_scope else 0)
    blocked_pnl = _safe_float(blocked_scope["closed_pnl"] if blocked_scope else 0.0)
    blocked_open_count = _safe_int(blocked_scope_open["open_count"] if blocked_scope_open else 0)
    blocked_recent_count = _safe_int(blocked_scope_recent["recent_closed_count"] if blocked_scope_recent else 0)
    blocked_recent_pnl = _safe_float(blocked_scope_recent["recent_closed_pnl"] if blocked_scope_recent else 0.0)
    blocked_status = "ok"
    blocked_message = "No non-reference deviation paper trades found."
    if blocked_open_count > 0:
        blocked_status = "critical"
        blocked_message = (
            f"Active reference-scope leakage: {blocked_open_count} open non-reference deviation trade(s)."
        )
    elif blocked_count > 0:
        blocked_status = "warning"
        blocked_message = (
            f"Reference-scope leakage detected: {blocked_count} closed non-reference deviation trades ({blocked_pnl:+.2f})."
        )
    blocked_scope_monitor = {
        "status": blocked_status,
        "lifetime_closed_count": blocked_count,
        "lifetime_closed_pnl": blocked_pnl,
        "lifetime_open_count": blocked_open_count,
        "recent_6h_closed_count": blocked_recent_count,
        "recent_6h_closed_pnl": blocked_recent_pnl,
        "message": blocked_message,
    }

    shadow_open_count = _safe_int(shadow_open["open_count"] if shadow_open else 0)
    shadow_open_size = _safe_float(shadow_open["open_size"] if shadow_open else 0.0)
    shadow_open_age = _minutes_ago(oldest_shadow_open_ts, now)
    shadow_open_monitor = {
        "status": "ok",
        "open_count": shadow_open_count,
        "total_open_size": shadow_open_size,
        "oldest_open_minutes": shadow_open_age,
        "message": "Shadow open exposure is within expected range.",
    }
    if shadow_open_count > 0 and shadow_open_age is not None and shadow_open_age >= 240:
        shadow_open_monitor["status"] = "warning"
        shadow_open_monitor["message"] = f"Oldest shadow position has been open for {shadow_open_age}m."
    shadow_last_closed_minutes = _minutes_ago(last_shadow_closed_ts, now)
    shadow_activity_monitor = {
        "status": "ok",
        "minutes_since_last_closed": shadow_last_closed_minutes,
        "last_closed_ts": shadow_closed["last_closed_ts"] if shadow_closed else None,
        "warning_threshold_minutes": 6 * 60,
        "critical_threshold_minutes": 24 * 60,
        "message": "Recent shadow activity is within expected range.",
    }
    if shadow_last_closed_minutes is None:
        shadow_activity_monitor["status"] = "warning"
        shadow_activity_monitor["message"] = "No closed shadow trades found yet."
    elif shadow_last_closed_minutes >= 24 * 60:
        shadow_activity_monitor["status"] = "critical"
        shadow_activity_monitor["message"] = (
            f"No closed shadow trades for {shadow_last_closed_minutes}m (over 24h)."
        )
    elif shadow_last_closed_minutes >= 6 * 60:
        shadow_activity_monitor["status"] = "warning"
        shadow_activity_monitor["message"] = (
            f"No closed shadow trades for {shadow_last_closed_minutes}m (over 6h)."
        )

    paper_last_closed_minutes = _minutes_ago(last_paper_ts, now)
    paper_activity_monitor = {
        "status": "ok",
        "today_closed_trades": _safe_int(today_paper["trade_count"]),
        "minutes_since_last_closed": paper_last_closed_minutes,
        "last_closed_ts": last_paper_close["ts"] if last_paper_close else None,
        "message": "Recent paper activity is within expected range.",
    }
    if paper_last_closed_minutes is None:
        paper_activity_monitor["status"] = "critical"
        paper_activity_monitor["message"] = "No closed paper trades found yet."
    elif paper_last_closed_minutes >= 48 * 60:
        paper_activity_monitor["status"] = "critical"
        paper_activity_monitor["message"] = (
            f"No closed paper trades for {paper_last_closed_minutes}m (over 48h)."
        )
    elif paper_last_closed_minutes >= 24 * 60:
        paper_activity_monitor["status"] = "warning"
        paper_activity_monitor["message"] = (
            f"No closed paper trades for {paper_last_closed_minutes}m (over 24h)."
        )

    optimizer_risk_scale = _safe_float(optimizer_status.get("risk_scale", 1.0))
    expected_risk_cap = 0.35
    paper_risk_monitor = {
        "status": "ok",
        "risk_scale": optimizer_risk_scale,
        "expected_cap": expected_risk_cap,
        "inactivity_minutes": paper_last_closed_minutes,
        "warning_threshold_minutes": 24 * 60,
        "critical_threshold_minutes": 48 * 60,
        "message": "Optimizer risk scale is aligned with inactivity-aware paper cap.",
    }
    if paper_last_closed_minutes is not None and optimizer_risk_scale > expected_risk_cap:
        if paper_last_closed_minutes >= 48 * 60:
            paper_risk_monitor["status"] = "critical"
            paper_risk_monitor["message"] = (
                f"Paper inactivity is {paper_last_closed_minutes}m (over 48h) and optimizer state risk_scale={optimizer_risk_scale:.2f} "
                f"exceeds expected cap {expected_risk_cap:.2f}; verify runtime cap enforcement immediately."
            )
        elif paper_last_closed_minutes >= 24 * 60:
            paper_risk_monitor["status"] = "warning"
            paper_risk_monitor["message"] = (
                f"Paper inactivity is {paper_last_closed_minutes}m but optimizer state risk_scale={optimizer_risk_scale:.2f} "
                f"exceeds expected cap {expected_risk_cap:.2f}; verify runtime cap enforcement."
            )

    equity_curve = []
    running = 0.0
    for idx, row in enumerate(all_paper[-120:]):
        running += _safe_float(row["pnl"])
        equity_curve.append({"x": idx + 1, "timestamp": row["timestamp"], "equity": running})

    strategy_series_map: dict[str, list[dict]] = {}
    strategy_running: dict[str, float] = {}
    strategy_index: dict[str, int] = {}
    for row in all_paper[-180:]:
        name = (row["strategy_name"] or "-").strip() or "-"
        strategy_running[name] = strategy_running.get(name, 0.0) + _safe_float(row["pnl"])
        strategy_index[name] = strategy_index.get(name, 0) + 1
        strategy_series_map.setdefault(name, []).append({"x": strategy_index[name], "equity": strategy_running[name]})
    strategy_curves = [{"strategy": key, "points": pts} for key, pts in strategy_series_map.items()]

    hourly_rows = [dict(r) for r in hourly_rows_raw]
    hourly_active = [r for r in hourly_rows if _safe_int(r.get("trade_count")) >= 2]
    hourly_top_active = sorted(hourly_active, key=lambda r: _safe_float(r.get("pnl")), reverse=True)[:3]
    hourly_bottom_active = sorted(hourly_active, key=lambda r: _safe_float(r.get("pnl")))[:3]

    pnl_values = [_safe_float(row["pnl"]) for row in recent_paper_rows]
    bins = [(-999, -1.0, "<=-1.0"), (-1.0, -0.2, "-1.0~-0.2"), (-0.2, 0.0, "-0.2~0.0"), (0.0, 0.2, "0.0~0.2"), (0.2, 1.0, "0.2~1.0"), (1.0, 999, ">=1.0")]
    histogram = []
    for lo, hi, label in bins:
        count = sum(1 for v in pnl_values if (v >= lo and v < hi) or (label == ">=1.0" and v >= lo))
        histogram.append({"label": label, "count": count})

    recommendations = []
    if recovery_locked:
        _append_unique(recommendations, "Recovery lock is active; keep live trading disabled.")
    if optimizer_state_monitor["status"] != "ok":
        _append_unique(recommendations, optimizer_state_monitor["message"])
    if optimizer_event_monitor["status"] != "ok":
        _append_unique(recommendations, optimizer_event_monitor["message"])
    if optimizer_reference_consistency_monitor["status"] != "ok":
        _append_unique(recommendations, optimizer_reference_consistency_monitor["message"])
    if runtime_guard_monitor["status"] != "ok":
        _append_unique(recommendations, runtime_guard_monitor["message"])
    if db_monitor["status"] != "ok":
        _append_unique(recommendations, db_monitor["message"])
    if system_staleness_monitor["status"] != "ok":
        _append_unique(recommendations, system_staleness_monitor["message"])
    if runtime_stall_monitor["status"] != "ok":
        _append_unique(recommendations, runtime_stall_monitor["message"])
    if runtime_restart_monitor["status"] != "ok":
        _append_unique(recommendations, runtime_restart_monitor["message"])
    if blocked_scope_monitor["status"] != "ok":
        _append_unique(recommendations, blocked_scope_monitor["message"])
    if shadow_open_monitor["status"] != "ok":
        _append_unique(recommendations, shadow_open_monitor["message"])
    if shadow_activity_monitor["status"] != "ok":
        _append_unique(recommendations, shadow_activity_monitor["message"])
    if paper_activity_monitor["status"] != "ok":
        _append_unique(recommendations, paper_activity_monitor["message"])
    if paper_risk_monitor["status"] != "ok":
        _append_unique(recommendations, paper_risk_monitor["message"])
    if gate_sample_monitor["status"] != "ok":
        _append_unique(recommendations, gate_sample_monitor["message"])
    if not recommendations:
        _append_unique(recommendations, "No acute monitor alerts; continue paper-only observation.")

    intervention_monitors = [
        optimizer_state_monitor,
        optimizer_event_monitor,
        optimizer_reference_consistency_monitor,
        runtime_guard_monitor,
        db_monitor,
        bot_log_monitor,
        system_staleness_monitor,
        runtime_stall_monitor,
        runtime_restart_monitor,
        blocked_scope_monitor,
        shadow_open_monitor,
        shadow_activity_monitor,
        paper_activity_monitor,
        paper_risk_monitor,
        gate_sample_monitor,
    ]
    critical_alerts = sum(1 for m in intervention_monitors if _status_rank(m.get("status", "ok")) == 2)
    warning_alerts = sum(1 for m in intervention_monitors if _status_rank(m.get("status", "ok")) == 1)
    intervention_needed = recovery_locked or critical_alerts > 0 or warning_alerts > 0
    paper_gate_freshness_qualified_ready = (
        paper_gate_ready
        and _status_rank(system_staleness_monitor.get("status", "ok")) < 2
        and _status_rank(gate_sample_monitor.get("status", "ok")) < 2
    )
    paper_gate_message = "Paper gate passed" if paper_gate_ready else "Paper gate locked"
    if paper_gate_ready and not paper_gate_freshness_qualified_ready:
        paper_gate_message = "Paper gate passed on historical sample, but freshness checks are critical."
        _append_unique(
            recommendations,
            "Paper gate is ready on historical sample only; treat it as non-actionable until core heartbeats recover.",
        )
    active_groups = cfg.active_market_groups()
    group_rows_normalized = _normalize_group_rows(group_rows, active_groups)
    shadow_group_rows_normalized = _normalize_group_rows(shadow_group_rows, active_groups)

    payload = {
        "generated_at": now.isoformat(),
        "headline": {
            "paper_today_pnl": _safe_float(today_paper["total_pnl"]),
            "paper_today_win_rate": _safe_float(today_paper["win_rate"]),
            "paper_today_trades": _safe_int(today_paper["trade_count"]),
            "recent20_pnl": sample_pnl,
            "recent20_win_rate": sample_win_rate,
            "paper_goal_hit": _safe_int(today_paper["trade_count"]) >= cfg.optimizer_day_profit_min_trades and _safe_float(today_paper["total_pnl"]) > 0,
            "live_ready": live_ready,
        },
        "paper_scoreboard": {
            "today_pnl": _safe_float(today_paper["total_pnl"]),
            "today_trades": _safe_int(today_paper["trade_count"]),
            "today_win_rate": _safe_float(today_paper["win_rate"]),
            "recent20_pnl": sample_pnl,
            "recent20_win_rate": sample_win_rate,
            "goal_hit": _safe_int(today_paper["trade_count"]) >= cfg.optimizer_day_profit_min_trades and _safe_float(today_paper["total_pnl"]) > 0,
        },
        "recovery": {
            "locked": recovery_locked,
            "start_pnl": cfg.recovery_start_pnl,
            "today_total_pnl": today_non_shadow_pnl,
            "effective_pnl": effective_pnl,
            "target_pnl": cfg.live_resume_profit_target,
            "remaining": remaining_unlock,
            "progress": progress,
        },
        "paper_gate": {
            "ready": paper_gate_ready,
            "freshness_qualified_ready": paper_gate_freshness_qualified_ready,
            "reference_group": cfg.performance_reference_group,
            "sample_size": sample_size,
            "sample_pnl": sample_pnl,
            "sample_win_rate": sample_win_rate,
            "sample_profit_factor": sample_pf,
            "required_trades": cfg.paper_live_gate_min_trades,
            "required_pnl": cfg.paper_live_gate_min_pnl,
            "required_win_rate": cfg.paper_live_gate_min_win_rate,
            "required_profit_factor": cfg.paper_live_gate_min_profit_factor,
            "passes_win_rate": passes_win_rate,
            "passes_profit_factor": passes_profit_factor,
            "remaining_trades": max(0, cfg.paper_live_gate_min_trades - sample_size),
            "remaining_pnl": max(0.0, cfg.paper_live_gate_min_pnl - sample_pnl),
            "remaining_win_rate": max(0.0, cfg.paper_live_gate_min_win_rate - sample_win_rate),
            "sample_last_trade_ts": sample_last_trade_ts.isoformat() if sample_last_trade_ts else None,
            "sample_age_minutes": sample_age_minutes,
            "sample_freshness_monitor": gate_sample_monitor,
            "message": paper_gate_message,
        },
        "optimizer": {
            "profile_name": optimizer_status.get("profile_name", "-"),
            "phase": optimizer_status.get("phase", "idle"),
            "regime": optimizer_status.get("regime", "warming_up"),
            "reference_group": optimizer_status.get("reference_group", cfg.performance_reference_group),
            "sample_pnl": _safe_float(optimizer_status.get("sample_pnl", 0.0)),
            "sample_size": _safe_int(optimizer_status.get("sample_size", 0)),
            "sample_win_rate": _safe_float(optimizer_status.get("sample_win_rate", 0.0)),
            "today_paper_pnl": _safe_float(optimizer_status.get("today_paper_pnl", 0.0)),
            "today_closed_trades": _safe_int(optimizer_status.get("today_closed_trades", 0)),
            "risk_scale": _safe_float(optimizer_status.get("risk_scale", 1.0)),
            "blocked_strategies": optimizer_status.get("blocked_strategies", []),
            "blocked_strategy_sides": optimizer_status.get("blocked_strategy_sides", []),
            "active_config": optimizer_status.get("active_config", {}),
            "message": optimizer_status.get("message", "Optimizer is evaluating market regime and profile selection."),
            "events": [dict(r) for r in optimizer_events],
            "state_monitor": optimizer_state_monitor,
            "event_monitor": optimizer_event_monitor,
            "reference_consistency_monitor": optimizer_reference_consistency_monitor,
        },
        "recommendation": {
            "confidence": "high" if recovery_locked else "medium",
            "headline": "Keep live disabled" if recovery_locked else "Paper-only observation",
            "summary": recommendations[0],
            "actions": recommendations,
            "intervention_needed": intervention_needed,
            "alert_counts": {
                "critical": critical_alerts,
                "warning": warning_alerts,
            },
        },
        "translations": {
            "paper_goal": "Validate paper profitability and sample size before live transition.",
            "profit_notice": "Both recovery lock and paper gate must pass for live_ready=true.",
            "optimizer_note": "Check optimizer state/log drift in dedicated monitors.",
        },
        "metrics": {
            "overall_paper": {
                "closed_count": _safe_int(overall_paper["trade_count"]),
                "pnl": _safe_float(overall_paper["total_pnl"]),
                "win_rate": _safe_float(overall_paper["win_rate"]),
            },
            "recent_paper": [dict(r) for r in recent_paper_rows],
            "open_positions": [dict(r) for r in open_rows],
            "mode_breakdown": [dict(r) for r in mode_breakdown],
            "strategy_rows": [dict(r) for r in strategy_rows],
            "profile_rows": [dict(r) for r in profile_rows],
            "group_rows": group_rows_normalized,
            "shadow_group_rows": shadow_group_rows_normalized,
            "daily_rows": [dict(r) for r in daily_rows],
            "hourly_rows": hourly_rows,
            "hourly_top_active": hourly_top_active,
            "hourly_bottom_active": hourly_bottom_active,
            "pnl_histogram": histogram,
            "equity_curve": equity_curve,
            "strategy_curves": strategy_curves,
            "blocked_scope_monitor": blocked_scope_monitor,
            "shadow_open_monitor": shadow_open_monitor,
            "shadow_activity_monitor": shadow_activity_monitor,
            "paper_activity_monitor": paper_activity_monitor,
            "paper_risk_monitor": paper_risk_monitor,
            "runtime_guard_monitor": {
                **runtime_guard_monitor,
                "last_guard_line": latest_guard_line,
            },
            "bot_log_monitor": bot_log_monitor,
            "db_monitor": db_monitor,
            "optimizer_state_monitor": optimizer_state_monitor,
            "optimizer_event_monitor": optimizer_event_monitor,
            "optimizer_reference_consistency_monitor": optimizer_reference_consistency_monitor,
            "system_staleness_monitor": system_staleness_monitor,
            "runtime_stall_monitor": runtime_stall_monitor,
            "runtime_restart_monitor": runtime_restart_monitor,
            "paper_gate_sample_monitor": gate_sample_monitor,
            "reference_today_paper": {
                "reference_group": cfg.performance_reference_group,
                "closed_count": reference_today_trades,
                "pnl": reference_today_pnl,
                "win_rate": _safe_float(reference_today_paper["win_rate"] if reference_today_paper else 0.0),
            },
            "shadow_balance": {
                "closed_count": _safe_int(shadow_closed["closed_count"] if shadow_closed else 0),
                "closed_pnl": _safe_float(shadow_closed["pnl"] if shadow_closed else 0.0),
                "closed_win_rate": _safe_float(shadow_closed["win_rate"] if shadow_closed else 0.0),
                "open_count": shadow_open_count,
                "open_size": shadow_open_size,
                "last_closed_ts": shadow_closed["last_closed_ts"] if shadow_closed else None,
                "oldest_open_ts": shadow_open["oldest_open_ts"] if shadow_open else None,
            },
            "paper_last_closed_ts": last_paper_close["ts"] if last_paper_close else None,
            "today_utc": today_key,
            "shadow_minutes_since_last_closed": shadow_last_closed_minutes,
            "paper_minutes_since_last_closed": _minutes_ago(last_paper_ts, now),
            "intervention_needed": intervention_needed,
            "monitor_alert_counts": {
                "critical": critical_alerts,
                "warning": warning_alerts,
            },
        },
        "paper_optimizer_state": optimizer_state,
    }
    return payload


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/dashboard":
            self._serve_api()
            return
        self._serve_static(path)

    def _serve_api(self):
        try:
            payload = build_payload(self.server.cfg, self.server.repo)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            error = json.dumps({"error": str(exc), "generated_at": _utc_now().isoformat()}).encode("utf-8")
            self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(error)))
            self.end_headers()
            self.wfile.write(error)

    def _serve_static(self, raw_path: str):
        rel = raw_path.lstrip("/") or "index.html"
        safe_root = self.server.web_dir.resolve()
        target = (safe_root / rel).resolve()
        if not str(target).startswith(str(safe_root)) or not target.exists() or target.is_dir():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type, _ = mimetypes.guess_type(str(target))
        content = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format: str, *args):
        return


class DashboardServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_cls, cfg: Config, repo: DashboardRepository, web_dir: Path):
        super().__init__(server_address, handler_cls)
        self.cfg = cfg
        self.repo = repo
        self.web_dir = web_dir


def main():
    cfg = Config()
    base_dir = Path(cfg.base_dir)
    web_dir = base_dir / "web"
    if not web_dir.exists():
        raise RuntimeError(f"web directory not found: {web_dir}")

    repo = DashboardRepository(
        db_path=Path(cfg.db_path),
        log_path=Path(cfg.log_path),
        optimizer_state_path=base_dir / "paper_optimizer_state.json",
    )

    host = os.getenv("WEB_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_DASHBOARD_PORT", "8765"))
    server = DashboardServer((host, port), DashboardHandler, cfg=cfg, repo=repo, web_dir=web_dir)
    print(f"Dashboard running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()



















