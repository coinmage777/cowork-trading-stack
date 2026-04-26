"""
Equity Tracker — 실시간 거래소별 잔고 추적
봇 시작 시 초기 잔고 기록, 이후 주기적으로 현재 잔고 스냅샷.
equity_tracker.json에 시계열 저장.
"""
import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 같은 HL 지갑을 공유해서 equity_tracker에 중복 계산되는 거래소.
# hyena_2(0xWALLC) + hl_wallet_c(0xWALLC) 둘 다 HL wallet의 accountValue를 쿼리해서
# 합산 시 2배 집계됨. hyena_2만 유효 기록, hl_wallet_c는 0으로.
DUPLICATE_WALLET_EXCHANGES = {"hl_wallet_c"}


class EquityTracker:
    def __init__(
        self,
        data_path: str = "equity_tracker.json",
        interval_minutes: float = 10.0,
        max_entries: int = 5000,
    ):
        self.data_path = Path(data_path)
        self.interval_minutes = interval_minutes
        self.max_entries = max_entries
        self.running = False

        self._wrappers: dict[str, object] = {}
        self._manual_equity: dict[str, float] = {}
        self._data: list[dict] = self._load()
        # 2026-04-22: API 실패 시 이전 유효값 fallback (kill_switch false alarm 방지)
        # 2026-04-22 Codex HIGH 1: TTL 추가 (value, ts)
        self._last_valid: dict[str, tuple[float, float]] = {}
        import time as _time
        if self._data:
            seen: set[str] = set()
            for snap in reversed(self._data):
                snap_ts = _time.time()
                try:
                    from datetime import datetime as _dt
                    snap_ts = _dt.fromisoformat(snap["timestamp"]).timestamp()
                except Exception:
                    pass
                for ex, v in (snap.get("exchanges") or {}).items():
                    if ex in seen:
                        continue
                    if isinstance(v, (int, float)) and v > 0:
                        self._last_valid[ex] = (float(v), snap_ts)
                        seen.add(ex)

    def _load(self) -> list:
        if self.data_path.exists():
            try:
                return json.loads(self.data_path.read_text(encoding="utf-8"))
            except Exception:
                return []
        return []

    def _save(self):
        try:
            if len(self._data) > self.max_entries:
                self._data = self._data[-self.max_entries:]
            self.data_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[EquityTracker] 저장 실패: {e}")

    def register(self, exchange_name: str, wrapper, manual_equity: float = 0):
        self._wrappers[exchange_name] = wrapper
        if manual_equity > 0:
            self._manual_equity[exchange_name] = manual_equity

    async def snapshot_all(self) -> dict:
        record = {
            "timestamp": datetime.now().isoformat(),
            "exchanges": {},
        }
        total = 0.0
        # dict 복사로 iteration 중 변경 방지
        wrappers = dict(self._wrappers)
        failures: list[str] = []
        # 2026-04-22 Codex HIGH: orphan 거래소 식별 (최근 200 스냅샷 중 70%+ zero)
        orphans: set[str] = set()
        if len(self._data) >= 20:
            recent = self._data[-200:]
            zero_cnt: dict[str, int] = {}
            total_cnt: dict[str, int] = {}
            for snap in recent:
                for ex, v in (snap.get("exchanges") or {}).items():
                    total_cnt[ex] = total_cnt.get(ex, 0) + 1
                    if v == 0:
                        zero_cnt[ex] = zero_cnt.get(ex, 0) + 1
            for ex, tot in total_cnt.items():
                if tot >= 20 and zero_cnt.get(ex, 0) / tot >= 0.70:
                    orphans.add(ex)

        # 2026-04-22 HIP-3 venues (hyena/hyena_2)은 API가 mark-to-market 노이즈 심함 → total_clean에서 영구 제외
        HIP3_VOLATILE = {"hyena", "hyena_2"}
        total_excluding_orphan = 0.0
        total_excluding_hip3 = 0.0
        for name, wrapper in wrappers.items():
            equity, is_fresh = await self._get_equity(name, wrapper)
            record["exchanges"][name] = round(equity, 2)
            total += equity
            if name not in orphans:
                total_excluding_orphan += equity
            if name not in orphans and name not in HIP3_VOLATILE:
                total_excluding_hip3 += equity
            if not is_fresh:
                failures.append(name)
        # total은 기존 호환 위해 전체 합, total_clean은 orphan 제외 (defensive 판단용)
        record["total"] = round(total, 2)
        record["total_clean"] = round(total_excluding_orphan, 2)
        record["total_stable"] = round(total_excluding_hip3, 2)  # HIP-3 제외 (mark 노이즈 없는 안정 잔고)
        if orphans:
            record["_orphans"] = sorted(orphans)
        if failures:
            record["_failures"] = failures

        # Anomaly detection: total이 이전 스냅샷의 50% 미만이면 이상치 → 재조회 1회
        if self._data:
            prev_total = self._data[-1].get("total", 0)
            if prev_total > 0 and total < prev_total * 0.5:
                logger.warning(
                    f"[EquityTracker] 이상치 감지: ${total:.2f} < 이전(${prev_total:.2f})의 50% │ 재조회"
                )
                # Retry once
                total = 0.0
                record["exchanges"] = {}
                failures = []
                for name, wrapper in wrappers.items():
                    await asyncio.sleep(1)  # API cooldown
                    equity, is_fresh = await self._get_equity(name, wrapper)
                    record["exchanges"][name] = round(equity, 2)
                    total += equity
                    if not is_fresh:
                        failures.append(name)
                record["total"] = round(total, 2)
                if failures:
                    record["_failures"] = failures

                # Still anomalous after retry? Flag it
                if total < prev_total * 0.5:
                    record["anomaly"] = True
                    logger.warning(
                        f"[EquityTracker] 재조회 후에도 이상: ${total:.2f} │ anomaly 플래그"
                    )

        self._data.append(record)
        self._save()
        return record

    async def _get_equity(self, name: str, wrapper) -> tuple[float, bool]:
        """(value, is_fresh) 반환. is_fresh=False면 API 실패로 이전값 fallback."""
        if name in DUPLICATE_WALLET_EXCHANGES:
            return 0.0, True  # 정책적 0, 성공
        for method_name in ["get_balance", "get_collateral"]:
            method = getattr(wrapper, method_name, None)
            if not method:
                continue
            try:
                result = await method()
                if isinstance(result, (int, float)) and result > 0:
                    v = float(result)
                    self._last_valid[name] = (v, time.time())
                    return v, True
                if isinstance(result, dict):
                    perp_val = 0.0
                    for key in ["total_collateral", "available_collateral",
                                "equity", "totalEquity", "balance"]:
                        if key in result and result[key]:
                            try:
                                v = float(result[key])
                                if v > 0:
                                    perp_val = v
                                    break
                            except Exception:
                                pass
                    # HIP-3 (HyENA): spot USDE는 사용 가능한 마진으로 HIP-3 margin과 합산
                    # 이전: perp_val < 1.0일 때만 spot 사용 → 포지션 오픈 시 spot이 빠지면 잔고 오류
                    # 수정: 항상 perp_val + spot USDE 합산 (실제 총 margin)
                    if name in ("hyena", "hyena_2"):
                        spot = result.get("spot") or {}
                        spot_usde = 0.0
                        if isinstance(spot, dict):
                            try:
                                spot_usde = float(spot.get("USDE", 0) or 0)
                            except Exception:
                                pass
                        total_val = max(perp_val, 0.0) + max(spot_usde, 0.0)
                        if total_val > 0:
                            self._last_valid[name] = (total_val, time.time())
                            return total_val, True
                    if perp_val > 0:
                        self._last_valid[name] = (perp_val, time.time())
                        return perp_val, True
            except Exception:
                continue
        # 2026-04-22 TTL 15분→2시간 (API 장애 긴 경우 방어)
        last = self._last_valid.get(name)
        if last is not None:
            val, ts = last
            if val > 0 and time.time() - ts <= 7200:
                return val, False
            self._last_valid.pop(name, None)
        return self._manual_equity.get(name, 0), False

    async def run(self):
        self.running = True
        tag = "EQUITY"
        logger.info(f"  {tag} | Equity Tracker 시작 | 주기={self.interval_minutes}분")

        # 거래소 초기화 완료 대기 (30초)
        await asyncio.sleep(30)

        try:
            initial = await self.snapshot_all()
            parts = [f"{k}=${v:.0f}" for k, v in initial["exchanges"].items() if v > 0]
            logger.info(f"  {tag} | 초기 잔고: ${initial['total']:,.2f} ({', '.join(parts)})")
        except Exception as e:
            logger.error(f"  {tag} | 초기 스냅샷 실패: {e}")

        while self.running:
            await asyncio.sleep(self.interval_minutes * 60)
            try:
                snap = await self.snapshot_all()
                logger.info(f"  {tag} | 잔고: ${snap['total']:,.2f}")
            except Exception as e:
                logger.error(f"  {tag} | 스냅샷 실패: {e}")

    def stop(self):
        self.running = False

    def get_history(self, hours: int = 24) -> list:
        cutoff = time.time() - hours * 3600
        result = []
        for entry in self._data:
            try:
                ts = datetime.fromisoformat(entry["timestamp"]).timestamp()
                if ts >= cutoff:
                    result.append(entry)
            except Exception:
                continue
        return result

    def get_latest(self) -> Optional[dict]:
        return self._data[-1] if self._data else None

    def get_pnl_since_start(self) -> Optional[dict]:
        if len(self._data) < 2:
            return None
        first = self._data[0]
        last = self._data[-1]
        pnl = {}
        for name in last.get("exchanges", {}):
            start_val = first.get("exchanges", {}).get(name, 0)
            end_val = last["exchanges"].get(name, 0)
            if start_val > 0:
                pnl[name] = {
                    "start": start_val,
                    "current": end_val,
                    "pnl_usd": round(end_val - start_val, 2),
                    "pnl_pct": round((end_val - start_val) / start_val * 100, 2),
                }
        return pnl
