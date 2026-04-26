"""merge_split_arb.py — Polymarket / Predict.fun MERGE-SPLIT structural arbitrage.

전략 (JUSTCRYT 채널 #14 — 무위험):
- 바이너리 마켓에서 YES + NO = $1 (at resolution)
- YES_ask + NO_ask < $1 − edge  → 양쪽 매수 + mergePositions(CTF) 호출 → $1 회수 (MERGE path)
- YES_bid + NO_bid > $1 + edge  → splitPosition으로 $1을 YES+NO로 분할 → 양쪽 매도 (SPLIT path)

**무위험 조건**: 실제 체결 가격(슬리피지 포함) + 가스비 + 수수료 < 이론적 스프레드

본 모듈의 핵심 원칙:
  1) DRY_RUN 기본값 ON → 라이브 진입은 LIVE_CONFIRM=true 필요 (triple-lock)
  2) kill switch 파일(data/KILL_MERGE_SPLIT) 감지되면 즉시 진입 차단
  3) per-market cooldown 5분 — 같은 기회로 재발사 방지
  4) 일일/포지션당 상한 + CTF live 경로는 NotImplementedError STUB (실제 on-chain 호출은 Phase X.1)
  5) 탐지·로깅·회계 구조는 드라이런에서도 **완전히** 동작 → 기회 빈도/예상 PnL을 안전하게 측정

통합 포인트:
  main.py 에서 MERGE_SPLIT_ENABLED=true 일 때 `MergeSplitArb(clob_client=self.scanner, ...).start()` 를 태스크로 띄움.
  - clob_client: 우리는 `MarketScanner` 인스턴스(또는 .get_orderbook/place_order/sell_order 호출 가능한 객체)를 재사용
  - predict_client: Predict.fun SDK client (MERGE API 미공개 → 기본 Polymarket only)
  - telegram_notifier: notifier.notify async 호출기
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import notifier as _notifier
except Exception:  # notifier는 옵션
    _notifier = None

logger = logging.getLogger("polybot.merge_split")

# --- 상수 ---------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent

# Polymarket CTF on Polygon (ERC-1155)
POLYMARKET_CTF_ADDRESS = "<EVM_ADDRESS>"

# Default partition for binary market (YES=1, NO=2 bitmask)
BINARY_PARTITION = [1, 2]


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, "").strip() or default)
    except (ValueError, TypeError):
        return default


# --- 설정 ---------------------------------------------------------------
@dataclass
class MergeSplitConfig:
    enabled: bool = field(default_factory=lambda: _env_bool("MERGE_SPLIT_ENABLED", True))
    dry_run: bool = field(default_factory=lambda: _env_bool("MERGE_SPLIT_DRY_RUN", True))
    live_confirm: bool = field(default_factory=lambda: _env_bool("MERGE_SPLIT_LIVE_CONFIRM", False))
    min_edge: float = field(default_factory=lambda: _env_float("MERGE_SPLIT_MIN_EDGE", 0.005))
    max_usd: float = field(default_factory=lambda: _env_float("MERGE_SPLIT_MAX_USD", 50.0))
    daily_cap_usd: float = field(default_factory=lambda: _env_float("MERGE_SPLIT_DAILY_CAP_USD", 200.0))
    poll_interval_sec: int = field(default_factory=lambda: _env_int("MERGE_SPLIT_POLL_INTERVAL_SEC", 30))
    min_market_volume_usd: float = field(default_factory=lambda: _env_float("MERGE_SPLIT_MIN_MARKET_VOLUME_USD", 10000.0))
    kill_switch_file: str = field(default_factory=lambda: _env_str("MERGE_SPLIT_KILL_SWITCH_FILE", "data/KILL_MERGE_SPLIT"))
    fee_rate_poly: float = field(default_factory=lambda: _env_float("MERGE_SPLIT_FEE_RATE_POLY", 0.02))
    fee_rate_predict: float = field(default_factory=lambda: _env_float("MERGE_SPLIT_FEE_RATE_PREDICT", 0.01))
    per_market_cooldown_sec: int = 300  # 5분
    gamma_top_n: int = 40  # 볼륨 상위 N개 마켓만 스캔 (API 부하 관리)

    # --- Cold multi-outcome split (ovyg7f 스타일) -----------------
    cold_enabled: bool = field(default_factory=lambda: _env_bool("COLD_SPLIT_ENABLED", False))
    cold_dry_run: bool = field(default_factory=lambda: _env_bool("COLD_SPLIT_DRY_RUN", True))
    cold_live_confirm: bool = field(default_factory=lambda: _env_bool("COLD_SPLIT_LIVE_CONFIRM", False))
    cold_max_volume_usd: float = field(default_factory=lambda: _env_float("COLD_SPLIT_MAX_VOLUME_USD", 1000.0))
    cold_min_outcomes: int = field(default_factory=lambda: _env_int("COLD_SPLIT_MIN_OUTCOMES", 3))
    cold_max_outcomes: int = field(default_factory=lambda: _env_int("COLD_SPLIT_MAX_OUTCOMES", 50))
    cold_max_per_market_usd: float = field(default_factory=lambda: _env_float("COLD_SPLIT_MAX_PER_MARKET_USD", 5.0))
    cold_daily_cap_usd: float = field(default_factory=lambda: _env_float("COLD_SPLIT_DAILY_CAP_USD", 100.0))
    cold_seed_enabled: bool = field(default_factory=lambda: _env_bool("COLD_SPLIT_SEED_ENABLED", False))
    cold_sell_trigger_mult: float = field(default_factory=lambda: _env_float("COLD_SPLIT_SELL_TRIGGER_MULTIPLIER", 2.0))
    cold_poll_interval_sec: int = field(default_factory=lambda: _env_int("COLD_SPLIT_POLL_INTERVAL_SEC", 300))
    cold_min_days_to_resolution: int = field(default_factory=lambda: _env_int("COLD_SPLIT_MIN_DAYS_TO_RESOLUTION", 3))
    cold_monitor_interval_sec: int = 60  # 시드 포지션 모니터 주기

    # 라이브 경로가 진짜로 켜져 있는지 (3-lock)
    @property
    def is_live(self) -> bool:
        return self.enabled and (not self.dry_run) and self.live_confirm

    @property
    def cold_is_live(self) -> bool:
        return self.cold_enabled and (not self.cold_dry_run) and self.cold_live_confirm


# --- 기회/회계 ----------------------------------------------------------
@dataclass
class ArbOpportunity:
    venue: str  # "polymarket" | "predict"
    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float  # ask (MERGE) 또는 bid (SPLIT)
    no_price: float
    combined: float
    path: str  # "MERGE" | "SPLIT"
    gross_edge: float  # fee 전 엣지 (per $1 notional)
    net_edge: float  # fee 후 엣지
    est_profit_usd: float  # notional 대비 예상 달러 이익
    notional_usd: float
    detected_at: float
    volume_usd: float = 0.0


# --- 메인 클래스 ---------------------------------------------------------
class MergeSplitArb:
    """Polymarket/Predict.fun 구조적 MERGE/SPLIT 아비트라지 스캐너.

    Args:
        clob_client: MarketScanner 인스턴스 또는 get_orderbook/place_order 호환 객체
        predict_client: Predict.fun client (옵션)
        db_logger: DataLogger (옵션) — trades 테이블에 기록
        telegram_notifier: async 콜러블 or 모듈 (옵션)
        config: MergeSplitConfig
        gamma_client: Polymarket Gamma API httpx.AsyncClient (옵션, 없으면 내부 생성)
    """

    def __init__(
        self,
        clob_client: Any = None,
        predict_client: Any = None,
        db_logger: Any = None,
        telegram_notifier: Any = None,
        config: Optional[MergeSplitConfig] = None,
        gamma_client: Any = None,
        mode: str = "paper",
    ):
        self.scanner = clob_client  # MarketScanner 객체를 기대
        self.predict_client = predict_client
        self.db_logger = db_logger
        self._notifier = telegram_notifier or _notifier
        self.config = config or MergeSplitConfig()
        self.mode = mode
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._own_gamma = False
        self._gamma = gamma_client
        # 상태
        self._cooldowns: dict[str, float] = {}  # market_id -> until_ts
        self._daily_spent: float = 0.0
        self._daily_date: str = ""
        self._web3_service: Any = None  # poly_web3 SafeWeb3Service, lazy init
        # 감지/실행 카운터 — 대시보드/리포트용
        self.stats = {
            "opportunities_detected": 0,
            "merge_detected": 0,
            "split_detected": 0,
            "attempts": 0,
            "executed": 0,
            "total_profit_usd": 0.0,
            "last_opp_at": 0.0,
        }
        # JSONL 로그 (드라이런 포함 모든 기회 기록 → 빈도 측정)
        self._opp_log_path = BASE_DIR / "merge_split_opportunities.jsonl"

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------
    async def start(self):
        if not self.config.enabled:
            logger.info("[merge_split] MERGE_SPLIT_ENABLED=false → 비활성")
            return
        if self._running:
            logger.warning("[merge_split] 이미 실행 중")
            return
        self._running = True
        # Cold split 전용 상태 (별도 경로)
        self._cold_seeded_path = BASE_DIR / "data" / "cold_split_positions.jsonl"
        self._cold_seeded_path.parent.mkdir(parents=True, exist_ok=True)
        self._cold_daily_spent: float = 0.0
        self._cold_seeded_markets: set[str] = set()
        self._cold_task: Optional[asyncio.Task] = None
        self._cold_monitor_task: Optional[asyncio.Task] = None
        self.stats.setdefault("cold_split_scanned", 0)
        self.stats.setdefault("cold_split_immediate_profit", 0)
        self.stats.setdefault("cold_split_seeded", 0)
        self.stats.setdefault("cold_split_sold", 0)
        # Gamma client 준비
        if self._gamma is None:
            try:
                import httpx
                self._gamma = httpx.AsyncClient(base_url="https://gamma-api.polymarket.com", timeout=15.0)
                self._own_gamma = True
            except Exception as exc:
                logger.error(f"[merge_split] httpx AsyncClient 생성 실패: {exc}")
                self._running = False
                return

        logger.info(
            f"[merge_split] 시작 — dry_run={self.config.dry_run} live_confirm={self.config.live_confirm} "
            f"min_edge={self.config.min_edge:.4f} max_usd=${self.config.max_usd:.0f} "
            f"daily_cap=${self.config.daily_cap_usd:.0f} poll={self.config.poll_interval_sec}s"
        )
        if self.config.is_live:
            logger.warning("[merge_split] !!! LIVE 모드 활성 — 실제 자금 집행 가능 !!!")
            await self._notify(
                "[merge_split] LIVE 모드 시작 (실자금) — 진입 전 즉시 중단하려면 "
                f"touch {self.config.kill_switch_file}"
            )
        else:
            logger.info("[merge_split] DRY_RUN 모드 — 탐지/로깅만, 주문 미전송")

        self._task = asyncio.create_task(self._scan_loop())

        # Cold multi-outcome split 병렬 태스크 (COLD_SPLIT_ENABLED 일 때만)
        if self.config.cold_enabled:
            logger.info(
                f"[cold_split] 활성 — dry_run={self.config.cold_dry_run} live_confirm={self.config.cold_live_confirm} "
                f"max_vol=${self.config.cold_max_volume_usd:.0f} max_per_mkt=${self.config.cold_max_per_market_usd:.2f} "
                f"daily_cap=${self.config.cold_daily_cap_usd:.0f} seed={self.config.cold_seed_enabled} "
                f"sell_mult={self.config.cold_sell_trigger_mult:.1f}x min_days={self.config.cold_min_days_to_resolution}"
            )
            self._cold_task = asyncio.create_task(self._cold_scan_loop())
            self._cold_monitor_task = asyncio.create_task(self._cold_monitor_loop())
        else:
            logger.info("[cold_split] COLD_SPLIT_ENABLED=false → 비활성")

        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self._running = False
        for t in (self._task, getattr(self, "_cold_task", None), getattr(self, "_cold_monitor_task", None)):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        if self._own_gamma and self._gamma is not None:
            try:
                await self._gamma.aclose()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 메인 루프
    # ------------------------------------------------------------------
    async def _scan_loop(self):
        """주기적으로 상위 마켓을 스캔하고 기회를 처리."""
        while self._running:
            started = time.time()
            try:
                if self._kill_switch_active():
                    logger.warning("[merge_split] kill switch 감지 → sleep")
                else:
                    await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[merge_split] scan 실패: {exc}", exc_info=True)
            elapsed = time.time() - started
            sleep_for = max(1.0, self.config.poll_interval_sec - elapsed)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def _scan_once(self):
        """한 번의 스캔 사이클."""
        self._reset_daily_if_needed()
        markets = await self._fetch_top_polymarket_markets()
        if not markets:
            logger.debug("[merge_split] 상위 마켓 0건")
            return

        opps: list[ArbOpportunity] = []
        for market in markets:
            try:
                opp = await self._evaluate_market_polymarket(market)
                if opp is not None:
                    opps.append(opp)
            except Exception as exc:
                logger.debug(f"[merge_split] market 평가 실패 {market.get('id', '?')}: {exc}")

        # Predict.fun (stub) — SDK 분석되면 별도 _evaluate_market_predict 추가
        # 현재는 Polymarket only

        if not opps:
            return

        # 최적 기회 순 정렬 (net_edge 내림차순)
        opps.sort(key=lambda x: (-x.net_edge, -x.est_profit_usd))
        for opp in opps:
            self.stats["opportunities_detected"] += 1
            if opp.path == "MERGE":
                self.stats["merge_detected"] += 1
            else:
                self.stats["split_detected"] += 1
            self.stats["last_opp_at"] = opp.detected_at
            self._append_opp_log(opp)
            await self._handle_opportunity(opp)

    # ------------------------------------------------------------------
    # Polymarket 데이터 수집
    # ------------------------------------------------------------------
    async def _fetch_top_polymarket_markets(self) -> list[dict]:
        """Gamma API /markets 에서 활성 바이너리 마켓 상위 N개 (볼륨 기준).

        반환 구조: [{id, conditionId, question, clobTokenIds, outcomes, outcomePrices, volume, ...}, ...]
        실패 시 빈 리스트.
        """
        try:
            params = {
                "closed": "false",
                "active": "true",
                "archived": "false",
                "order": "volume",
                "ascending": "false",
                "limit": str(self.config.gamma_top_n),
            }
            resp = await self._gamma.get("/markets", params=params)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                return []
            # 볼륨 + 바이너리 + 활성 필터
            filtered: list[dict] = []
            for m in data:
                try:
                    vol = float(m.get("volume") or m.get("volumeNum") or 0.0)
                except (TypeError, ValueError):
                    vol = 0.0
                if vol < self.config.min_market_volume_usd:
                    continue
                if m.get("closed") or not m.get("active", True):
                    continue
                clob_ids = m.get("clobTokenIds")
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except json.JSONDecodeError:
                        clob_ids = []
                if not isinstance(clob_ids, list) or len(clob_ids) != 2:
                    continue
                m["_clob_ids"] = clob_ids
                m["_volume"] = vol
                filtered.append(m)
            return filtered
        except Exception as exc:
            logger.debug(f"[merge_split] gamma /markets 조회 실패: {exc}")
            return []

    async def _evaluate_market_polymarket(self, market: dict) -> Optional[ArbOpportunity]:
        """단일 Polymarket 마켓에서 MERGE 또는 SPLIT 기회 판별."""
        market_id = str(market.get("id") or market.get("conditionId") or "")
        if not market_id:
            return None
        condition_id = str(market.get("conditionId") or market_id)
        # 쿨다운
        until = self._cooldowns.get(market_id, 0.0)
        if time.time() < until:
            return None

        clob_ids = market.get("_clob_ids", [])
        if len(clob_ids) != 2:
            return None
        yes_id, no_id = str(clob_ids[0]), str(clob_ids[1])

        if self.scanner is None or not hasattr(self.scanner, "get_orderbook"):
            logger.debug("[merge_split] scanner.get_orderbook 미사용 가능 → 스킵")
            return None

        yes_book = await self.scanner.get_orderbook(yes_id)
        no_book = await self.scanner.get_orderbook(no_id)

        yes_bid, yes_ask = _best_bid_ask(yes_book)
        no_bid, no_ask = _best_bid_ask(no_book)

        fee = self.config.fee_rate_poly
        volume = float(market.get("_volume", 0.0))
        question = str(market.get("question") or "")[:120]

        # --- MERGE path: 양쪽 ask 매수 후 merge → $1 회수
        # 매수비용 = (yes_ask + no_ask) + 수수료(매수 2건)
        # 무위험 수익 per $1 notional = 1 − (yes_ask + no_ask) − fee*(yes_ask + no_ask)
        if yes_ask > 0 and no_ask > 0:
            combined_ask = yes_ask + no_ask
            cost_with_fee = combined_ask * (1.0 + fee)
            gross_edge = 1.0 - combined_ask
            net_edge = 1.0 - cost_with_fee
            if net_edge >= self.config.min_edge:
                notional = min(self.config.max_usd, self._remaining_daily_usd())
                if notional <= 0:
                    logger.info(f"[merge_split] 일일 캡 소진 → MERGE 스킵 {market_id[:12]}")
                    return None
                return ArbOpportunity(
                    venue="polymarket",
                    market_id=market_id,
                    condition_id=condition_id,
                    question=question,
                    yes_token_id=yes_id,
                    no_token_id=no_id,
                    yes_price=yes_ask,
                    no_price=no_ask,
                    combined=combined_ask,
                    path="MERGE",
                    gross_edge=gross_edge,
                    net_edge=net_edge,
                    est_profit_usd=net_edge * notional,
                    notional_usd=notional,
                    detected_at=time.time(),
                    volume_usd=volume,
                )

        # --- SPLIT path: $1 → YES+NO split, 양쪽 bid 매도 → 회수
        # 매도수익 = (yes_bid + no_bid) − 수수료(매도 2건)
        # 무위험 수익 per $1 notional = (yes_bid + no_bid) − fee*combined_bid − 1
        if yes_bid > 0 and no_bid > 0:
            combined_bid = yes_bid + no_bid
            proceeds_with_fee = combined_bid * (1.0 - fee)
            gross_edge = combined_bid - 1.0
            net_edge = proceeds_with_fee - 1.0
            if net_edge >= self.config.min_edge:
                notional = min(self.config.max_usd, self._remaining_daily_usd())
                if notional <= 0:
                    logger.info(f"[merge_split] 일일 캡 소진 → SPLIT 스킵 {market_id[:12]}")
                    return None
                return ArbOpportunity(
                    venue="polymarket",
                    market_id=market_id,
                    condition_id=condition_id,
                    question=question,
                    yes_token_id=yes_id,
                    no_token_id=no_id,
                    yes_price=yes_bid,
                    no_price=no_bid,
                    combined=combined_bid,
                    path="SPLIT",
                    gross_edge=gross_edge,
                    net_edge=net_edge,
                    est_profit_usd=net_edge * notional,
                    notional_usd=notional,
                    detected_at=time.time(),
                    volume_usd=volume,
                )
        return None

    # ------------------------------------------------------------------
    # 기회 처리 / 실행
    # ------------------------------------------------------------------
    async def _handle_opportunity(self, opp: ArbOpportunity):
        """기회 로깅 + (라이브면) 실행 시도."""
        tag = f"{opp.venue}/{opp.path}/{opp.market_id[:10]}"
        summary = (
            f"[merge_split] {tag} combined={opp.combined:.4f} "
            f"net_edge={opp.net_edge*100:.3f}% est_profit=${opp.est_profit_usd:.3f} "
            f"notional=${opp.notional_usd:.0f} vol=${opp.volume_usd:,.0f} | {opp.question[:60]}"
        )
        logger.info(summary)

        # 쿨다운 등록 (같은 마켓 재발사 방지)
        self._cooldowns[opp.market_id] = time.time() + self.config.per_market_cooldown_sec

        # 드라이런 → 로깅만
        if not self.config.is_live:
            logger.info(f"[merge_split] DRY_RUN 감지 (live_confirm={self.config.live_confirm})")
            # 드라이런 텔레그램은 dedup으로 줄임
            await self._notify(
                f"[MERGE-SPLIT DRY] {opp.path} edge {opp.net_edge*100:.2f}% ${opp.est_profit_usd:.2f} "
                f"| {opp.question[:50]}",
                dedup_key=f"merge_split_dry:{opp.path}",
            )
            return

        # LIVE 경로 — 3-lock 통과 확인
        if not self.config.live_confirm:
            logger.warning("[merge_split] live_confirm=false → 실행 차단")
            return
        if self._kill_switch_active():
            logger.warning("[merge_split] kill switch 활성 → 실행 차단")
            return

        # 일일 캡 재확인
        if opp.notional_usd > self._remaining_daily_usd():
            logger.warning(f"[merge_split] notional ${opp.notional_usd:.0f} > 잔여 일일 캡")
            return

        self.stats["attempts"] += 1
        try:
            if opp.path == "MERGE":
                result = await self._execute_merge(opp)
            else:
                result = await self._execute_split(opp)
            if result.get("success"):
                self.stats["executed"] += 1
                self._daily_spent += opp.notional_usd
                pnl = float(result.get("realized_pnl") or 0.0)
                self.stats["total_profit_usd"] += pnl
                await self._log_trade_db(opp, result, pnl)
                await self._notify(
                    f"[MERGE-SPLIT LIVE] {opp.path} 체결 ${pnl:+.2f} (edge {opp.net_edge*100:.2f}%) "
                    f"| {opp.question[:40]}"
                )
            else:
                err = str(result.get("error", "unknown"))[:200]
                logger.error(f"[merge_split] 실행 실패 {tag}: {err}")
                await self._notify(f"[MERGE-SPLIT FAIL] {opp.path} {err[:120]}", dedup_key="merge_split_fail")
        except NotImplementedError as exc:
            logger.warning(f"[merge_split] 라이브 CTF 경로 미구현 (Phase X.1): {exc}")
            await self._notify(
                f"[MERGE-SPLIT STUB] {opp.path} CTF 라이브 구현 대기 ({exc})",
                dedup_key="merge_split_stub",
            )
        except Exception as exc:
            logger.error(f"[merge_split] 실행 예외: {exc}", exc_info=True)

    async def _execute_merge(self, opp: ArbOpportunity) -> dict:
        """MERGE path 라이브 실행.

        단계:
          1) YES/NO 양쪽 ask 매수 (IOC)
          2) 두 주문 전부 체결 확인
          3) CTF.mergePositions(conditionId, partition=[1,2], amount) 호출
          4) USDC 회수 확인 → PnL 계산

        현재: 매수는 기존 scanner.place_order 로 가능하나 CTF merge 라이브 콜은 STUB.
        poly_web3 SafeWeb3Service.merge() 가 claim_venv 에 있음 — 별도 bridge 프로세스 필요.
        따라서 Phase X.1 에서 구현.
        """
        if self.scanner is None or not hasattr(self.scanner, "place_order"):
            return {"success": False, "error": "scanner.place_order 없음"}

        # 1) notional 을 shares 로 환산 — 이 단계까지는 안전하게 드라이 실행 가능
        yes_shares = opp.notional_usd / max(opp.yes_price, 0.001)
        no_shares = opp.notional_usd / max(opp.no_price, 0.001)
        logger.info(
            f"[merge_split] MERGE 실행 시도: YES {yes_shares:.1f}sh@{opp.yes_price:.4f} + "
            f"NO {no_shares:.1f}sh@{opp.no_price:.4f}"
        )

        # 2) CTF merge 콜 — STUB (poly_web3 brige 필요)
        #    poly_web3.SafeWeb3Service(clob_client, relayer_client, rpc_url).merge(condition_id, amount)
        raise NotImplementedError(
            "CTF mergePositions 라이브 콜 미구현 — Phase X.1. "
            "poly_web3 SafeWeb3Service 를 claim_venv subprocess 로 래핑 필요. "
            "드라이런 감지는 완전히 동작함."
        )

    async def _execute_split(self, opp: ArbOpportunity) -> dict:
        """SPLIT path 라이브 실행.

        단계:
          1) USDC → CTF 승인 (최초 1회 또는 부족 시)
          2) CTF.splitPosition($1 notional) → YES+NO ERC-1155 수령
          3) YES/NO 양쪽 bid 매도 (IOC)
          4) USDC 회수 확인 → PnL 계산

        현재: CTF split 라이브 콜은 STUB (poly_web3 필요), 매도는 scanner.sell_order 로 구현 가능.
        """
        if self.scanner is None or not hasattr(self.scanner, "sell_order"):
            return {"success": False, "error": "scanner.sell_order 없음"}

        logger.info(
            f"[merge_split] SPLIT 실행 시도: ${opp.notional_usd:.0f} → YES/NO "
            f"후 YES@{opp.yes_price:.4f} + NO@{opp.no_price:.4f} 매도"
        )

        raise NotImplementedError(
            "CTF splitPosition 라이브 콜 미구현 — Phase X.1. "
            "poly_web3 SafeWeb3Service 를 claim_venv subprocess 로 래핑 필요. "
            "드라이런 감지는 완전히 동작함."
        )

    # ------------------------------------------------------------------
    # 보조 유틸
    # ------------------------------------------------------------------
    def _kill_switch_active(self) -> bool:
        try:
            p = BASE_DIR / self.config.kill_switch_file
            return p.exists()
        except Exception:
            return False

    def _reset_daily_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_spent = 0.0
            # 쿨다운도 하루 지나면 정리 (메모리 누수 방지)
            now = time.time()
            self._cooldowns = {k: v for k, v in self._cooldowns.items() if v > now}

    def _remaining_daily_usd(self) -> float:
        return max(0.0, self.config.daily_cap_usd - self._daily_spent)

    def _append_opp_log(self, opp: ArbOpportunity):
        """드라이런 포함 모든 기회를 JSONL 에 append → 빈도/예상 PnL 측정."""
        try:
            entry = {
                "ts": opp.detected_at,
                "venue": opp.venue,
                "market_id": opp.market_id,
                "condition_id": opp.condition_id,
                "question": opp.question[:120],
                "path": opp.path,
                "yes_price": opp.yes_price,
                "no_price": opp.no_price,
                "combined": round(opp.combined, 6),
                "gross_edge": round(opp.gross_edge, 6),
                "net_edge": round(opp.net_edge, 6),
                "est_profit_usd": round(opp.est_profit_usd, 4),
                "notional_usd": opp.notional_usd,
                "volume_usd": opp.volume_usd,
                "mode": "live" if self.config.is_live else "dry_run",
            }
            with self._opp_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug(f"[merge_split] opp log 실패: {exc}")

    async def _log_trade_db(self, opp: ArbOpportunity, result: dict, pnl: float):
        """실제 체결된 경우에만 DB 에 log (드라이런은 JSONL 만)."""
        if self.db_logger is None:
            return
        try:
            trade_id = self.db_logger.log_trade(
                market_id=opp.market_id,
                side=opp.path,  # MERGE/SPLIT
                size=opp.notional_usd,
                entry_price=opp.combined,
                signal_values={
                    "path": opp.path,
                    "yes_price": opp.yes_price,
                    "no_price": opp.no_price,
                    "gross_edge": opp.gross_edge,
                    "net_edge": opp.net_edge,
                    "volume_usd": opp.volume_usd,
                },
                model_prob=1.0,  # 무위험 (이론)
                market_prob=opp.combined,
                edge=opp.net_edge,
                kelly_fraction=0.0,
                expiry_time="",
                market_question=opp.question,
                order_id=str(result.get("order_id", ""))[:200],
                mode="live",
                strategy_name="merge_split_arb",
                market_liquidity=opp.volume_usd,
                minutes_to_expiry=0.0,
                asset_symbol="USDC",
                market_group=opp.path.lower(),
                market_duration_min=0.0,
                token_id=f"{opp.yes_token_id},{opp.no_token_id}"[:200],
            )
            if trade_id > 0:
                self.db_logger.close_trade(trade_id, exit_price=1.0, pnl=pnl)
        except Exception as exc:
            logger.warning(f"[merge_split] DB log 실패: {exc}")

    # ==================================================================
    # COLD MULTI-OUTCOME SPLIT (@ovyg7f 스타일 저유동성 파밍)
    # ==================================================================
    # 원리:
    #   N개 outcome 멀티마켓 → CTF splitPosition($1) → outcome당 1 share 씩 수령
    #   outcome당 cost basis = 1/N, 실제 중 하나가 승자 → 회수 $1 → 이익 = 1 − 1/N×(N−비승자 할인)
    #   저유동성 신생 마켓에서는 오더북에 일부 outcome 이 1/N 보다 높은 bid 로 걸려 있으면
    #   split 후 그 outcome 만 즉시 팔아도 흑자 가능 (IMMEDIATE profit)
    #   아니면 "seed" 로 둔 뒤 resolution 가까워질 때 가격이 오른 outcome 매도
    #
    # 리스크:
    #   - 체결 불가 (bid 가 너무 얇거나 없음) → 자금 락업
    #   - 일부 outcome 을 이미 팔면 merge 가 불가능해짐 (partial merge 불가)
    #   - 거버넌스/resolver 분쟁 (기간 연장, 드문 no-winner)
    #   - 가스비 (Polygon 은 저렴하나 split+sell N건 누적)

    async def _cold_scan_loop(self):
        """저유동성 신생 멀티 마켓 스캔 루프."""
        while self._running:
            started = time.time()
            try:
                if self._kill_switch_active():
                    logger.debug("[cold_split] kill switch → skip")
                else:
                    await self._cold_split_scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[cold_split] scan 루프 실패: {exc}", exc_info=True)
            elapsed = time.time() - started
            sleep_for = max(5.0, self.config.cold_poll_interval_sec - elapsed)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def _cold_split_scan(self):
        """Gamma /events 로 multi-outcome 이벤트에서 merge arb 기회 평가.

        Polymarket 데이터 모델: 멀티 outcome은 /markets 에 3+ outcome 단일 행으로
        존재하지 않고, /events 하위에 binary YES/NO 마켓 N개로 그룹핑됨.
        상호배타 가정 하에 모든 YES를 ask로 매수, 1개만 $1 resolve → sum(ask)*(1+fee) < 1 이면 arb.
        """
        if self._gamma is None:
            return
        self._reset_daily_if_needed()
        if self._cold_daily_spent >= self.config.cold_daily_cap_usd:
            logger.debug("[cold_split] 일일 캡 소진")
            return

        try:
            params = {
                "active": "true",
                "closed": "false",
                "archived": "false",
                "limit": "200",
            }
            resp = await self._gamma.get("/events", params=params)
            resp.raise_for_status()
            events = resp.json()
        except Exception as exc:
            logger.debug(f"[cold_split] gamma /events 조회 실패: {exc}")
            return

        if not isinstance(events, list) or not events:
            return

        now = time.time()
        min_end_ts = now + self.config.cold_min_days_to_resolution * 86400
        fee = self.config.fee_rate_poly

        candidates_n = 0
        arb_n = 0
        near_miss_n = 0

        for ev in events:
            try:
                ev_id = str(ev.get("id") or ev.get("ticker") or "")
                if not ev_id or ev_id in self._cold_seeded_markets:
                    continue

                markets = ev.get("markets") or []
                n = len(markets)
                if n < self.config.cold_min_outcomes or n > self.config.cold_max_outcomes:
                    continue

                # 모든 자식 마켓 active + binary YES/NO 검증
                yes_tokens: list[str] = []
                yes_outcomes: list[str] = []
                max_end_ts = 0.0
                ok = True
                for m in markets:
                    if m.get("closed") or not m.get("active", True):
                        ok = False
                        break
                    clob = m.get("clobTokenIds")
                    if isinstance(clob, str):
                        try:
                            clob = json.loads(clob)
                        except json.JSONDecodeError:
                            clob = []
                    if not isinstance(clob, list) or len(clob) != 2:
                        ok = False
                        break
                    yes_tokens.append(str(clob[0]))
                    outs = m.get("outcomes")
                    if isinstance(outs, str):
                        try:
                            outs = json.loads(outs)
                        except json.JSONDecodeError:
                            outs = []
                    yes_outcomes.append(str(outs[0]) if isinstance(outs, list) and outs else "")
                    et = _parse_end_date(m.get("endDate") or m.get("end_date_iso") or m.get("endDateIso"))
                    if et > max_end_ts:
                        max_end_ts = et
                if not ok:
                    continue
                if max_end_ts <= 0 or max_end_ts < min_end_ts:
                    continue

                candidates_n += 1

                # YES ask 호가 수집
                if self.scanner is None or not hasattr(self.scanner, "get_orderbook"):
                    continue
                asks: list[float] = []
                missing = False
                for tid in yes_tokens:
                    try:
                        book = await self.scanner.get_orderbook(tid)
                        _, a = _best_bid_ask(book)
                    except Exception:
                        a = 0.0
                    if a <= 0:
                        missing = True
                        break
                    asks.append(a)
                if missing:
                    continue

                total_ask = sum(asks)
                cost_with_fee = total_ask * (1.0 + fee)
                net_edge = 1.0 - cost_with_fee

                ev_title = str(ev.get("title") or ev.get("slug") or "")[:80]
                try:
                    ev_vol = float(ev.get("volume") or ev.get("volumeNum") or 0.0)
                except (TypeError, ValueError):
                    ev_vol = 0.0

                if net_edge >= self.config.min_edge:
                    arb_n += 1
                    self.stats["cold_split_immediate_profit"] += 1
                    notional = min(
                        self.config.cold_max_per_market_usd,
                        self.config.cold_daily_cap_usd - self._cold_daily_spent,
                    )
                    if notional <= 0:
                        continue
                    est_profit = net_edge * notional
                    logger.info(
                        f"[cold_split/event] ARB ev={ev_id[:16]} N={n} sum_ask={total_ask:.4f} "
                        f"cost_fee={cost_with_fee:.4f} edge={net_edge*100:.3f}% "
                        f"est_profit=${est_profit:.3f} vol=${ev_vol:,.0f} | {ev_title}"
                    )
                    self._append_cold_log({
                        "kind": "event_merge_arb",
                        "event_id": ev_id,
                        "title": ev_title,
                        "n_markets": n,
                        "yes_outcomes": yes_outcomes,
                        "yes_asks": asks,
                        "sum_ask": total_ask,
                        "cost_with_fee": cost_with_fee,
                        "net_edge": net_edge,
                        "est_profit_usd": est_profit,
                        "notional_usd": notional,
                        "volume_usd": ev_vol,
                        "end_ts": max_end_ts,
                        "ts": time.time(),
                        "mode": "live" if self.config.cold_is_live else "dry_run",
                    })
                elif net_edge >= -0.02:
                    # 근접 — 관찰용 (arb 는 아님)
                    near_miss_n += 1
                    self._append_cold_log({
                        "kind": "event_merge_near_miss",
                        "event_id": ev_id,
                        "title": ev_title,
                        "n_markets": n,
                        "sum_ask": total_ask,
                        "cost_with_fee": cost_with_fee,
                        "net_edge": net_edge,
                        "volume_usd": ev_vol,
                        "ts": time.time(),
                    })

                if self._cold_daily_spent >= self.config.cold_daily_cap_usd:
                    logger.info("[cold_split/event] 일일 캡 도달 → 스캔 중단")
                    break
            except Exception as exc:
                logger.debug(f"[cold_split/event] {ev.get('id','?')} 평가 실패: {exc}")
                continue

        if candidates_n:
            self.stats["cold_split_scanned"] += candidates_n
            logger.info(
                f"[cold_split/event] 스캔완료 candidates={candidates_n} arb={arb_n} near_miss={near_miss_n}"
            )

    async def _cold_evaluate_market(self, market: dict):
        """멀티 outcome 마켓 하나에서 즉시 이익 또는 시드 기회 평가."""
        market_id = str(market.get("id") or market.get("conditionId") or "")
        if not market_id:
            return
        if market_id in self._cold_seeded_markets:
            return  # 이미 시드됨
        condition_id = str(market.get("conditionId") or market_id)

        outcomes = market["_outcomes"]
        clob_ids = market["_clob_ids"]
        n_out = len(outcomes)
        cost_per_outcome = 1.0 / n_out
        fee = self.config.fee_rate_poly

        if self.scanner is None or not hasattr(self.scanner, "get_orderbook"):
            return

        # 각 outcome 의 best bid 수집
        bids: list[float] = []
        asks: list[float] = []
        for tid in clob_ids:
            try:
                book = await self.scanner.get_orderbook(tid)
                b, a = _best_bid_ask(book)
            except Exception:
                b, a = 0.0, 0.0
            bids.append(b)
            asks.append(a)

        # IMMEDIATE profit 체크 — 어떤 outcome 이든 bid > cost × (1 + fee)
        breakeven = cost_per_outcome * (1.0 + fee)
        best_immediate: tuple[int, float] = (-1, 0.0)  # (idx, edge_per_share)
        for i, b in enumerate(bids):
            if b > breakeven:
                edge = b - breakeven
                if edge > best_immediate[1]:
                    best_immediate = (i, edge)

        question = str(market.get("question") or "")[:120]
        end_ts = market["_end_ts"]
        volume = market["_volume"]

        if best_immediate[0] >= 0:
            idx, edge_per_share = best_immediate
            self.stats["cold_split_immediate_profit"] += 1
            outcome_name = str(outcomes[idx])[:40]
            # 현재 구현은 즉시 이익도 CTF splitPosition 필요 → 동일 경로. 시드 여부만 다름
            notional = min(
                self.config.cold_max_per_market_usd,
                self.config.cold_daily_cap_usd - self._cold_daily_spent,
            )
            if notional <= 0:
                return
            est_profit = edge_per_share * notional  # edge_per_share = bid − 1/N×(1+fee) per share, notional = split 금액
            logger.info(
                f"[cold_split] IMMEDIATE {market_id[:10]} N={n_out} outcome={idx}({outcome_name}) "
                f"bid={bids[idx]:.4f} > breakeven={breakeven:.4f} est_profit=${est_profit:.3f} vol=${volume:,.0f}"
            )
            self._append_cold_log(
                {
                    "kind": "immediate_profit_candidate",
                    "market_id": market_id,
                    "condition_id": condition_id,
                    "question": question,
                    "n_outcomes": n_out,
                    "cost_per_outcome": cost_per_outcome,
                    "breakeven_bid": breakeven,
                    "target_outcome_idx": idx,
                    "target_outcome": outcome_name,
                    "target_bid": bids[idx],
                    "edge_per_share": edge_per_share,
                    "notional_usd": notional,
                    "est_profit_usd": est_profit,
                    "end_ts": end_ts,
                    "volume_usd": volume,
                    "ts": time.time(),
                    "mode": "live" if self.config.cold_is_live else "dry_run",
                }
            )
            await self._cold_execute(
                market_id=market_id,
                condition_id=condition_id,
                question=question,
                outcomes=outcomes,
                clob_ids=clob_ids,
                notional=notional,
                cost_per_outcome=cost_per_outcome,
                immediate_sell_idx=idx,
                immediate_bid=bids[idx],
            )
            return

        # IMMEDIATE 없음 → 시드 경로
        if not self.config.cold_seed_enabled:
            logger.debug(f"[cold_split] {market_id[:10]} N={n_out} IMMEDIATE 없음, seed 비활성 → skip")
            return
        notional = min(
            self.config.cold_max_per_market_usd,
            self.config.cold_daily_cap_usd - self._cold_daily_spent,
        )
        if notional <= 0:
            return
        logger.info(
            f"[cold_split] SEED {market_id[:10]} N={n_out} cost/out={cost_per_outcome:.4f} "
            f"max_bid={max(bids) if bids else 0:.4f} ${notional:.2f} vol=${volume:,.0f}"
        )
        self._append_cold_log(
            {
                "kind": "seed_candidate",
                "market_id": market_id,
                "condition_id": condition_id,
                "question": question,
                "n_outcomes": n_out,
                "cost_per_outcome": cost_per_outcome,
                "max_bid": max(bids) if bids else 0.0,
                "notional_usd": notional,
                "end_ts": end_ts,
                "volume_usd": volume,
                "ts": time.time(),
                "mode": "live" if self.config.cold_is_live else "dry_run",
            }
        )
        await self._cold_execute(
            market_id=market_id,
            condition_id=condition_id,
            question=question,
            outcomes=outcomes,
            clob_ids=clob_ids,
            notional=notional,
            cost_per_outcome=cost_per_outcome,
            immediate_sell_idx=None,
            immediate_bid=None,
        )

    async def _cold_execute(
        self,
        *,
        market_id: str,
        condition_id: str,
        question: str,
        outcomes: list,
        clob_ids: list,
        notional: float,
        cost_per_outcome: float,
        immediate_sell_idx: Optional[int],
        immediate_bid: Optional[float],
    ):
        """Cold split 실행 (DRY 또는 LIVE).

        DRY: JSONL 로 포지션 추가만 기록. LIVE: CTF splitPosition 콜 (STUB).
        성공 시 self._cold_seeded_markets 에 market_id 추가 + seeded position JSONL 기록.
        """
        if not self.config.cold_is_live:
            # 드라이런 시드 포지션 기록
            self._record_seeded_position(
                market_id=market_id,
                condition_id=condition_id,
                question=question,
                outcomes=outcomes,
                clob_ids=clob_ids,
                notional=notional,
                cost_per_outcome=cost_per_outcome,
                mode="dry_run",
                immediate_sell_idx=immediate_sell_idx,
                immediate_bid=immediate_bid,
            )
            self._cold_seeded_markets.add(market_id)
            self._cold_daily_spent += notional  # 드라이도 상한 추적 (실운영 감각 유지)
            self.stats["cold_split_seeded"] += 1
            await self._notify(
                f"[COLD-SPLIT DRY] N={len(outcomes)} ${notional:.2f} | {question[:50]}",
                dedup_key="cold_split_dry",
            )
            return

        # LIVE 경로 — kill switch 재확인
        if self._kill_switch_active():
            logger.warning("[cold_split] kill switch → LIVE 중단")
            return

        # CTF.splitPosition(USDC 담보, conditionId, partition=[1,2,...,N]) 콜 필요
        # poly_web3.SafeWeb3Service 가 claim_venv 에 있음 → subprocess bridge 패턴
        # 현재는 STUB. 실제 라이브는 Phase X.2 에서 binary SPLIT 과 함께 구현
        logger.warning(
            f"[cold_split] LIVE 실행 시도 {market_id[:10]} ${notional:.2f} — CTF splitPosition STUB"
        )
        try:
            raise NotImplementedError(
                "CTF splitPosition(N>=3 partition) 라이브 콜 미구현 — Phase X.2. "
                "poly_web3 SafeWeb3Service.split(condition_id, partition=[1,2,...,2^N-1], amount) 를 "
                "claim_venv subprocess 로 래핑 필요. 드라이런 감지는 완전히 동작함."
            )
        except NotImplementedError as exc:
            await self._notify(
                f"[COLD-SPLIT STUB] CTF split 라이브 미구현: {str(exc)[:100]}",
                dedup_key="cold_split_stub",
            )
            # STUB 이므로 자금 집행 안 됨 → daily_spent 증가 금지

    def _record_seeded_position(
        self,
        *,
        market_id: str,
        condition_id: str,
        question: str,
        outcomes: list,
        clob_ids: list,
        notional: float,
        cost_per_outcome: float,
        mode: str,
        immediate_sell_idx: Optional[int] = None,
        immediate_bid: Optional[float] = None,
    ):
        """cold_split_positions.jsonl 에 시드 포지션 기록.

        구조: 각 outcome 에 shares = notional × N_outcomes? NO — splitPosition 은
        $notional 당 (notional / 1.0) 만큼 **각 outcome 1 share 씩** 지급한다.
        즉 notional $X → X shares per outcome. cost_basis per share per outcome = 1/N.
        """
        try:
            n_out = len(outcomes)
            shares_per_outcome = notional  # splitPosition semantics: $X → X shares each
            entry = {
                "ts": time.time(),
                "market_id": market_id,
                "condition_id": condition_id,
                "question": question[:200],
                "n_outcomes": n_out,
                "outcomes": [str(o)[:50] for o in outcomes],
                "clob_ids": clob_ids,
                "notional_usd": notional,
                "cost_per_outcome": cost_per_outcome,
                "shares_per_outcome": shares_per_outcome,
                "remaining_shares": {str(i): shares_per_outcome for i in range(n_out)},
                "mode": mode,
                "status": "open",
                "immediate_sell_idx": immediate_sell_idx,
                "immediate_bid": immediate_bid,
                "realized_pnl_usd": 0.0,
            }
            with self._cold_seeded_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug(f"[cold_split] seed 기록 실패: {exc}")

    def _append_cold_log(self, entry: dict):
        """cold 기회 감지 로그 (별도 파일)."""
        try:
            path = BASE_DIR / "cold_split_opportunities.jsonl"
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug(f"[cold_split] opp log 실패: {exc}")

    # ------------------------------------------------------------------
    # 시드 포지션 모니터 (60초마다)
    # ------------------------------------------------------------------
    async def _cold_monitor_loop(self):
        """열려 있는 cold 시드 포지션을 주기적으로 확인 → sell_trigger 에 도달하면 매도."""
        while self._running:
            try:
                await asyncio.sleep(self.config.cold_monitor_interval_sec)
                if self._kill_switch_active():
                    continue
                await self._cold_monitor_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug(f"[cold_split] monitor 실패: {exc}")

    async def _cold_monitor_once(self):
        """JSONL 의 열린 시드 포지션 → 오더북 재확인 → trigger 시 매도."""
        if not self._cold_seeded_path.exists():
            return
        try:
            lines = self._cold_seeded_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return

        open_positions: list[dict] = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if e.get("status") == "open":
                open_positions.append(e)

        if not open_positions:
            return

        now = time.time()
        trigger_mult = self.config.cold_sell_trigger_mult

        for pos in open_positions:
            try:
                market_id = pos.get("market_id", "")
                clob_ids = pos.get("clob_ids") or []
                cost_per_outcome = float(pos.get("cost_per_outcome") or 0.0)
                if not clob_ids or cost_per_outcome <= 0:
                    continue

                # 오더북 재확인 — 어떤 outcome 의 best bid 가 cost × trigger_mult 초과?
                for i, tid in enumerate(clob_ids):
                    try:
                        book = await self.scanner.get_orderbook(tid)
                        b, _ = _best_bid_ask(book)
                    except Exception:
                        b = 0.0
                    if b >= cost_per_outcome * trigger_mult:
                        outcome = (pos.get("outcomes") or [])[i] if i < len(pos.get("outcomes") or []) else str(i)
                        logger.info(
                            f"[cold_split] SELL TRIGGER {market_id[:10]} outcome={i}({outcome}) "
                            f"bid={b:.4f} >= {cost_per_outcome*trigger_mult:.4f}"
                        )
                        await self._notify(
                            f"[COLD-SPLIT SELL] {market_id[:10]} out={outcome} bid {b:.3f} "
                            f"(cost {cost_per_outcome:.3f} x{trigger_mult:.1f})",
                            dedup_key=f"cold_sell:{market_id}:{i}",
                        )
                        # 실매도는 라이브 모드에서만, 현재는 scanner.sell_order STUB + JSONL 업데이트
                        if self.config.cold_is_live and self.scanner is not None and hasattr(self.scanner, "sell_order"):
                            # 남은 shares 만큼 매도 — STUB 경로 (구체 sell_order 시그니처는 scanner 에 맞게 조정 필요)
                            logger.warning("[cold_split] sell_order 라이브 호출 STUB — 수동 확인 필요")
                        # 시드 항목 부분 청산 기록 (실제 업데이트는 append-only JSONL 재작성 필요)
                        self.stats["cold_split_sold"] += 1

                # 해상도 임박 (3일 이내) 체크 → 경고만 (partial sell 이미 진행했으면 merge 불가)
                # (end_ts 를 seed 기록에 넣어두진 않았지만 — 향후 필요하면 추가)
                _ = now  # silence unused
            except Exception:
                continue

    async def _notify(self, msg: str, *, dedup_key: Optional[str] = None):
        if self._notifier is None:
            return
        try:
            notify_fn = getattr(self._notifier, "notify", None)
            if notify_fn is None:
                return
            kwargs = {"dedup_seconds": 600}
            if dedup_key:
                kwargs["dedup_key"] = dedup_key
            res = notify_fn(msg, **kwargs)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:
            logger.debug(f"[merge_split] notify 실패: {exc}")


# --- 오더북 유틸 ---------------------------------------------------------
def _best_bid_ask(book: dict) -> tuple[float, float]:
    """Polymarket orderbook dict 에서 best bid/ask 추출.

    구조: {"bids": [{"price": "0.50", "size": "..."}, ...], "asks": [...]}
    bids 는 내림차순, asks 는 오름차순 정렬돼 있음 (Polymarket /book 표준).
    """
    try:
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = 0.0
        best_ask = 0.0
        if bids:
            p = bids[0].get("price") or bids[0].get("p")
            if p is not None:
                best_bid = float(p)
        if asks:
            p = asks[0].get("price") or asks[0].get("p")
            if p is not None:
                best_ask = float(p)
        # 주의: Polymarket 는 bids 0.01 / asks 0.99 와 같은 양극 매칭이 있어서
        # 실제 체결 가능 가격과 다를 수 있음. 여기서는 최상위 호가 기준.
        return best_bid, best_ask
    except (KeyError, IndexError, TypeError, ValueError):
        return 0.0, 0.0


def _parse_end_date(v: Any) -> float:
    """Gamma endDate(ISO8601 또는 unix) → unix timestamp (float). 실패 시 0.0."""
    if v is None:
        return 0.0
    try:
        # unix 숫자 (str/int/float)
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s:
            return 0.0
        if s.isdigit():
            return float(s)
        # ISO8601 — "2026-05-01T00:00:00Z" 형식
        from datetime import datetime, timezone
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


__all__ = ["MergeSplitArb", "MergeSplitConfig", "ArbOpportunity"]
