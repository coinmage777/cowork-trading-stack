"""
Polymarket/Predict 자율 최적화 (2026-04-18)

trades_v2.db에서 전략별 성과 분석 → Claude API → .env 파라미터 개선 제안/적용.

안전장치:
  - 화이트리스트 외 변경 금지
  - 변경폭 제한
  - 일일 1회
  - 24h 후 PnL -$10 이하면 자동 롤백
  - 모든 변경 Telegram 알림

사용법:
  python scripts/autonomous_optimizer.py --dry-run
  python scripts/autonomous_optimizer.py --apply
  python scripts/autonomous_optimizer.py --rollback-check
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

DB = ROOT / "trades_v2.db"
ENV_PATH = ROOT / ".env"
BACKUP_DIR = ROOT / "env_backups"
STATE = ROOT / "auto_optimizer_state.json"
BALANCE = ROOT / "balance_snapshots.json"

# 화이트리스트: .env 변수명 → {min, max, max_delta, type}
ALLOWED_PARAMS = {
    # Predict.fun sniper
    "PREDICT_SNIPE_MAX_MINUTES":      {"min": 2.0, "max": 10.0, "max_delta": 2.0, "type": "float"},
    "PREDICT_SNIPE_MIN_STRIKE_DIST":  {"min": 0.0003, "max": 0.002, "max_delta": 0.0005, "type": "float"},
    "PREDICT_SNIPE_MAX_ENTRY_PRICE":  {"min": 0.55, "max": 0.85, "max_delta": 0.1, "type": "float"},
    "PREDICT_SNIPE_MIN_EDGE":         {"min": 0.01, "max": 0.10, "max_delta": 0.03, "type": "float"},
    "PREDICT_SNIPE_BET_SIZE":         {"min": 1, "max": 10, "max_delta": 3, "type": "int"},
    "PREDICT_MAX_OPEN_POSITIONS":     {"min": 3, "max": 15, "max_delta": 3, "type": "int"},
    # Hedge arb
    "HEDGE_MAX_COMBINED_PRICE":       {"min": 0.85, "max": 0.99, "max_delta": 0.05, "type": "float"},
    "HEDGE_MIN_PROFIT_PCT":           {"min": 0.01, "max": 0.10, "max_delta": 0.03, "type": "float"},
    # Circuit breaker
    "DAILY_STOP_LOSS":                {"min": -100.0, "max": -10.0, "max_delta": 30.0, "type": "float"},
    # ML blend (if set)
    "ML_BLEND_WEIGHT":                {"min": 0.0, "max": 0.5, "max_delta": 0.1, "type": "float"},
}


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(d: dict) -> None:
    STATE.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")


def read_env() -> dict:
    """현재 .env 값들."""
    out = {}
    if not ENV_PATH.exists():
        return out
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def update_env(changes: list) -> None:
    """changes list를 .env에 적용. 기존 줄은 유지."""
    if not ENV_PATH.exists():
        raise RuntimeError(".env not found")
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    existing_keys = {}
    for i, line in enumerate(lines):
        s = line.strip()
        if "=" in s and not s.startswith("#"):
            k = s.split("=", 1)[0].strip()
            existing_keys[k] = i

    for ch in changes:
        key = ch["param"]
        new_val = str(ch["new"])
        if key in existing_keys:
            lines[existing_keys[key]] = f"{key}={new_val}"
        else:
            lines.append(f"{key}={new_val}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_stats(days: int = 7) -> dict:
    if not DB.exists():
        return {}
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()
    c = sqlite3.connect(str(DB))
    try:
        cur = c.cursor()
        # 전체
        r = cur.execute(
            "SELECT COUNT(*), SUM(pnl), "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)*100.0/COUNT(*), "
            "SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END), "
            "SUM(CASE WHEN pnl<0 THEN ABS(pnl) ELSE 0 END) "
            "FROM trades WHERE timestamp >= ?",
            (since,)
        ).fetchone()
        if not r or not r[0]:
            return {}
        n, pnl, wr, gw, gl = r
        pf = (gw / gl) if gl else 99.0
        # 전략별
        rows = cur.execute(
            "SELECT strategy_name, COUNT(*), SUM(pnl), "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)*100.0/COUNT(*), AVG(edge), AVG(model_prob - market_prob) "
            "FROM trades WHERE timestamp >= ? GROUP BY strategy_name",
            (since,)
        ).fetchall()
        by_strat = [{"strategy": r[0] or "unknown", "n": r[1], "pnl": round(r[2] or 0, 2),
                     "wr": round(r[3] or 0, 1), "avg_edge": round(r[4] or 0, 4),
                     "avg_edge_realized": round(r[5] or 0, 4)} for r in rows]
        # 자산별 (predict_snipe용)
        rows = cur.execute(
            "SELECT asset_symbol, COUNT(*), SUM(pnl), "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)*100.0/COUNT(*) "
            "FROM trades WHERE timestamp >= ? AND strategy_name='predict_snipe' "
            "GROUP BY asset_symbol HAVING COUNT(*) >= 3",
            (since,)
        ).fetchall()
        by_asset = [{"asset": r[0], "n": r[1], "pnl": round(r[2] or 0, 2), "wr": round(r[3] or 0, 1)} for r in rows]
        # 만료분(minutes_to_expiry) 버킷
        rows = cur.execute(
            "SELECT CAST(minutes_to_expiry AS INTEGER), COUNT(*), SUM(pnl), "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END)*100.0/COUNT(*) "
            "FROM trades WHERE timestamp >= ? AND strategy_name='predict_snipe' "
            "GROUP BY CAST(minutes_to_expiry AS INTEGER) HAVING COUNT(*) >= 3",
            (since,)
        ).fetchall()
        by_ttl = [{"ttl_min": r[0], "n": r[1], "pnl": round(r[2] or 0, 2), "wr": round(r[3] or 0, 1)} for r in rows]
    finally:
        c.close()
    return {
        "n": n, "pnl_usd": round(pnl, 2), "wr": round(wr, 1), "pf": round(pf, 2),
        "by_strategy": by_strat,
        "by_asset": by_asset,
        "by_ttl_min": by_ttl,
    }


def get_balance() -> float:
    if not BALANCE.exists():
        return 0.0
    try:
        d = json.loads(BALANCE.read_text(encoding="utf-8"))
        if isinstance(d, list) and d:
            last = d[-1]
            return float(last.get("usdc_balance") or last.get("balance") or 0)
    except Exception:
        pass
    return 0.0


def build_prompt(stats: dict, params: dict) -> str:
    return f"""너는 Polymarket / Predict.fun 예측시장 봇의 파라미터 최적화 전문가다.
주력 전략: Predict.fun snipe (만료 임박 시 가격 스냅샷). 보조: hedge_arb.

[최근 7일 데이터]
{json.dumps(stats, indent=2, ensure_ascii=False)}

[현재 .env 파라미터]
{json.dumps(params, indent=2, ensure_ascii=False)}

[수정 가능 + 허용 범위]
{json.dumps(ALLOWED_PARAMS, indent=2, ensure_ascii=False)}

[룰]
1. 최대 **3개 파라미터**만 수정 제안
2. max_delta 이내 변경
3. 표본 부족 (n<10) 구간은 건드리지 말 것
4. 과거 이력 반영:
   - expiry_snipe는 역선택으로 WR 29% 되어 한동안 비활성이었음 → 최근 WR 상승이면 다시 활성 고려
   - predict.fun은 현재 주력, 튜닝 대상
   - weather는 ghost position 문제로 비활성 유지 권장
   - hedge_arb는 기회 희소
5. WR > 60% 전략은 공격적 튜닝 가능, WR < 40%는 보수적/축소
6. 근거는 데이터 수치 인용

[출력 형식] 순수 JSON:
{{
  "analysis": "현재 상태 진단 1문장",
  "changes": [
    {{
      "param": "PREDICT_SNIPE_MIN_EDGE",
      "old": "0.02",
      "new": "0.04",
      "reason": "predict_snipe WR 47% → 진입 엣지 상향"
    }}
  ]
}}
changes 빈 배열이면 현상 유지.
"""


async def call_claude(prompt: str) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        return {"error": "ANTHROPIC_API_KEY 없음"}
    url = "https://api.anthropic.com/v1/messages"
    payload = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1500,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.post(url, json=payload, headers=headers) as r:
            d = await r.json()
            if r.status != 200:
                return {"error": d.get("error", {}).get("message", str(d)[:200])}
            try:
                content = d["content"][0]["text"].strip()
                if content.startswith("```"):
                    content = content.split("```", 2)[1]
                    if content.startswith("json"):
                        content = content[4:]
                return json.loads(content.strip())
            except Exception as e:
                return {"error": f"parse: {e}"}


def validate_change(change: dict, current: dict) -> tuple[bool, str]:
    p = change.get("param", "")
    if p not in ALLOWED_PARAMS:
        return False, f"화이트리스트 외: {p}"
    spec = ALLOWED_PARAMS[p]
    try:
        new = float(change.get("new"))
    except Exception:
        return False, "new numeric 변환 실패"
    if new < spec["min"] or new > spec["max"]:
        return False, f"범위 외 [{spec['min']}, {spec['max']}]"
    old_str = current.get(p, "")
    if old_str:
        try:
            old = float(old_str)
            delta = abs(new - old)
            if delta > spec["max_delta"]:
                return False, f"변경폭 {delta} > {spec['max_delta']}"
        except Exception:
            pass
    # int 타입 강제
    if spec.get("type") == "int":
        change["new"] = int(new)
    else:
        change["new"] = new
    return True, ""


def backup_env() -> Path:
    BACKUP_DIR.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    h = hashlib.sha256(ENV_PATH.read_bytes()).hexdigest()[:12]
    dst = BACKUP_DIR / f"env_auto_{ts}_{h}"
    shutil.copy(ENV_PATH, dst)
    return dst


async def send_telegram(msg: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        print(msg)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
        async with s.post(url, json={"chat_id": chat, "text": msg[:4000], "parse_mode": "HTML"}) as r:
            if r.status != 200:
                body = await r.text()
                if "can't parse entities" in body:
                    import re
                    plain = re.sub(r"<[^>]+>", "", msg)
                    await s.post(url, json={"chat_id": chat, "text": plain[:4000]})


def restart_polymarket():
    """서비스 재시작 (Polymarket은 hot reload 없음)."""
    import subprocess
    try:
        subprocess.run(["systemctl", "restart", "polymarket"], timeout=10, check=False)
    except Exception:
        pass


async def run_optimizer(apply: bool) -> None:
    state = load_state()
    last_opt = state.get("last_optimization")
    if last_opt:
        last_dt = datetime.fromisoformat(last_opt)
        if (datetime.now(timezone.utc) - last_dt).total_seconds() < 22 * 3600:
            msg = f"<b>🤖 Poly Auto-Opt 스킵</b>\n  마지막 {last_dt.strftime('%H:%M UTC')} · 22h 쿨다운"
            if apply:
                await send_telegram(msg)
            else:
                print(msg)
            return

    stats = collect_stats(7)
    if not stats or stats.get("n", 0) < 20:
        msg = f"<b>🤖 Poly Auto-Opt 스킵</b>\n  표본 부족 (7d n={stats.get('n', 0)}, 최소 20+)"
        if apply:
            await send_telegram(msg)
        else:
            print(msg)
        return

    env = read_env()
    params = {k: env.get(k, "") for k in ALLOWED_PARAMS}

    prompt = build_prompt(stats, params)
    result = await call_claude(prompt)
    if "error" in result:
        msg = f"<b>🤖 Poly Auto-Opt 실패</b>\n  {result['error'][:200]}"
        if apply:
            await send_telegram(msg)
        else:
            print(msg)
        return

    analysis = result.get("analysis", "")
    proposed = result.get("changes", []) or []

    valid_changes, rejected = [], []
    for ch in proposed:
        ok, reason = validate_change(ch, params)
        if ok:
            valid_changes.append(ch)
        else:
            rejected.append({**ch, "rejected": reason})

    lines = [f"<b>🤖 Poly 자율 최적화</b> ({datetime.now().strftime('%Y-%m-%d')})"]
    lines.append(f"  분석: {analysis}")
    lines.append(f"  표본: n={stats['n']} WR {stats['wr']}% PF {stats['pf']} PnL ${stats['pnl_usd']:+.2f}")
    if not valid_changes and not rejected:
        lines.append("\n✅ 현상 유지 권고")
        save_state({**state, "last_optimization": datetime.now(timezone.utc).isoformat(),
                    "last_changes": [], "last_baseline_pnl": stats["pnl_usd"],
                    "last_baseline_balance": get_balance()})
    else:
        if valid_changes:
            lines.append(f"\n<b>📝 적용{'할' if apply else ' 제안'} 변경 ({len(valid_changes)})</b>")
            for ch in valid_changes:
                lines.append(f"  • {ch['param']}: <code>{ch.get('old')}</code> → <code>{ch['new']}</code>")
                lines.append(f"    └ {ch.get('reason', '')[:90]}")
        if rejected:
            lines.append(f"\n<b>❌ 거부 ({len(rejected)})</b>")
            for ch in rejected[:3]:
                lines.append(f"  • {ch['param']}: {ch['rejected']}")

    if apply and valid_changes:
        backup_path = backup_env()
        update_env(valid_changes)
        restart_polymarket()
        lines.append(f"\n✅ 서비스 재시작 · backup: {backup_path.name}")
        lines.append("\n<i>24h 뒤 PnL 체크 → -$10 이하면 자동 롤백</i>")
        save_state({
            **state,
            "last_optimization": datetime.now(timezone.utc).isoformat(),
            "last_changes": valid_changes,
            "last_backup": str(backup_path),
            "last_baseline_pnl": stats["pnl_usd"],
            "last_baseline_balance": get_balance(),
        })

    msg = "\n".join(lines)
    if apply:
        await send_telegram(msg)
    print(msg)


async def run_rollback_check() -> None:
    state = load_state()
    if not state.get("last_optimization") or not state.get("last_changes"):
        return
    last_dt = datetime.fromisoformat(state["last_optimization"])
    age_h = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
    if age_h < 20:
        return
    baseline = float(state.get("last_baseline_pnl", 0))
    current_pnl = collect_stats(1).get("pnl_usd", 0)
    # 폴리마켓은 작은 자본이라 -$10 임계값
    if current_pnl < -10:
        backup = Path(state.get("last_backup", ""))
        if backup.exists():
            shutil.copy(backup, ENV_PATH)
            restart_polymarket()
            msg = (f"<b>🔙 Poly 자동 롤백</b>\n"
                   f"  24h PnL ${current_pnl:+.2f} (baseline ${baseline:+.2f})\n"
                   f"  복원: {backup.name}")
            await send_telegram(msg)
            save_state({**state, "last_changes": [], "rolled_back_at": datetime.now(timezone.utc).isoformat()})
    else:
        msg = f"<b>✅ Poly 24h 체크 OK</b>\n  PnL ${current_pnl:+.2f}"
        await send_telegram(msg)
        save_state({**state, "last_changes": []})


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback-check", action="store_true")
    args = ap.parse_args()
    if args.rollback_check:
        await run_rollback_check()
    else:
        await run_optimizer(apply=args.apply)


if __name__ == "__main__":
    asyncio.run(main())
