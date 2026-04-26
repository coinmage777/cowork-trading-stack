"""reversal_sniper.py — Polymarket 5-min BTC/ETH/SOL tail-option reversal sniper.

전략 (Arkstar / BlueNode / ovyg7f 스타일):
- Polymarket 5분 Up/Down 마켓 (btc/eth/sol)
- 윈도우 마감 직전 (seconds_to_close <= REVERSAL_ENTER_WINDOW_SEC, 기본 30초) 에,
  시가(strike) ↔ 현재가 차이가 극히 작으면 (|gap_pct| <= REVERSAL_MAX_GAP_PCT, 기본 0.05%)
  아직 방향이 결정되지 않은 상태 → 반대쪽으로 "꼬리" 리버설 확률이 비대칭적으로 유리해짐.
- 현재 가격 > strike ⇒ UP 쪽이 $0.99 근처 (거의 확정), DOWN 쪽이 $0.01 근처 → DOWN(=losing side)을 $0.01 에 매수.
- 리버설 발생 시 losing 쪽이 $1 로 정산 → 100 shares × $1 = $100 수령 → +$99 수익.
- 리버설 미발생 시 $1 손실. **BE WR = 1%**. 100번 시도 1회만 적중해도 본전.

핵심 안전장치 (triple-lock):
  1) REVERSAL_SNIPE_ENABLED=true  # 코드 패스 활성
  2) REVERSAL_SNIPE_DRY_RUN=true  # 기본 ON (드라이런에서도 탐지/로그 완전 동작)
  3) REVERSAL_SNIPE_LIVE_CONFIRM=false  # 둘 다 풀려야 라이브 진입
  4) kill switch 파일 (data/KILL_REVERSAL_SNIPE) 존재 시 즉시 차단
  5) per-market single-fire (같은 market_id 재발사 방지)
  6) 일일 지출 상한 (REVERSAL_SNIPE_DAILY_CAP_USD, 기본 $30 → 하루 30발)

통합: main.py TradingBot 생성자에서 REVERSAL_SNIPE_ENABLED=true 일 때
  self._reversal_sniper = ReversalSniper(
      scanner=self.scanner,
      binance_price_source=self.price_engines,
      db_logger=self.db_logger,
      notifier=_poly_notifier,
  )
  start() 은 start() 루프 안에서 create_task.
  stop() 은 _cleanup 에서 호출.

본 모듈은 MergeSplitArb 패턴을 따라 DRY_RUN 에서도 탐지/회계/JSONL 로깅이 완전히 동작한다.
LIVE 경로는 scanner.place_order(token_id, side='BUY', size, price, mode='live') 를 호출.
  - scanner.place_order 는 size 를 "USD notional" 로 받고 내부에서 shares = size/price 계산.
  - $1 notional × $0.01 price = 100 shares 가 되며, scanner 의 min $1 cost 검사를 통과.
  - scanner 내부에 가격 하한 (0.001) 검사가 이미 있어 0원 주문 차단됨.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("polybot.reversal_sniper")

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"

# 5분 마켓 — Gamma 이벤트 slug 에 쓰이는 자산 코드 / PriceEngine 키
ASSETS = (
    ("btc", "BTC"),
    ("eth", "ETH"),
    ("sol", "SOL"),
)

# 5분 마켓 = 300 초 창
WINDOW_SEC = 300


# ----------------------------------------------------------------------
# 환경변수 유틸
# ----------------------------------------------------------------------
def _env_str(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return (v or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    v = (os.environ.get(key) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_float(key: str, default: float) -> float:
    try:
        raw = (os.environ.get(key) or "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        raw = (os.environ.get(key) or "").strip()
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------
@dataclass
class ReversalSniperConfig:
    enabled: bool = field(default_factory=lambda: _env_bool("REVERSAL_SNIPE_ENABLED", False))
    dry_run: bool = field(default_factory=lambda: _env_bool("REVERSAL_SNIPE_DRY_RUN", True))
    live_confirm: bool = field(default_factory=lambda: _env_bool("REVERSAL_SNIPE_LIVE_CONFIRM", False))
    max_gap_pct: float = field(default_factory=lambda: _env_float("REVERSAL_SNIPE_MAX_GAP_PCT", 0.05))  # 퍼센트 (0.05% 수준)
    enter_window_sec: int = field(default_factory=lambda: _env_int("REVERSAL_SNIPE_ENTER_WINDOW_SEC", 30))
    notional_usd: float = field(default_factory=lambda: _env_float("REVERSAL_SNIPE_NOTIONAL_USD", 1.0))
    shares_per_order: int = field(default_factory=lambda: _env_int("REVERSAL_SNIPE_SHARES_PER_ORDER", 100))
    limit_price: float = field(default_factory=lambda: _env_float("REVERSAL_SNIPE_LIMIT_PRICE", 0.01))
    daily_cap_usd: float = field(default_factory=lambda: _env_float("REVERSAL_SNIPE_DAILY_CAP_USD", 30.0))
    poll_interval_sec: int = field(default_factory=lambda: _env_int("REVERSAL_SNIPE_POLL_INTERVAL_SEC", 10))
    min_market_volume_usd: float = field(default_factory=lambda: _env_float("REVERSAL_SNIPE_MIN_MARKET_VOLUME_USD", 100.0))
    kill_switch_file: str = field(default_factory=lambda: _env_str("REVERSAL_SNIPE_KILL_SWITCH_FILE", "data/KILL_REVERSAL_SNIPE"))
    # 가격 sanity — 0 또는 너무 큰 값 방어
    min_ref_price: float = 0.000001
    # Telegram dedup 윈도우 (같은 market 알림 중복 방지)
    notify_dedup_sec: int = 300
    # --- Predict.fun 전용 설정 ---
    predict_enabled: bool = field(default_factory=lambda: _env_bool("REVERSAL_SNIPE_PREDICT_ENABLED", True))
    predict_enter_window_sec: int = field(default_factory=lambda: _env_int("REVERSAL_SNIPE_PREDICT_ENTER_WINDOW_SEC", 60))
    predict_max_gap_pct: float = field(default_factory=lambda: _env_float("REVERSAL_SNIPE_PREDICT_MAX_GAP_PCT", 0.1))
    predict_shares_per_order: int = field(default_factory=lambda: _env_int("REVERSAL_SNIPE_PREDICT_SHARES_PER_ORDER", 100))
    predict_limit_price: float = field(default_factory=lambda: _env_float("REVERSAL_SNIPE_PREDICT_LIMIT_PRICE", 0.01))
    predict_fee_rate_bps: int = field(default_factory=lambda: _env_int("REVERSAL_SNIPE_PREDICT_FEE_RATE_BPS", 100))  # 1% taker

    @property
    def is_live(self) -> bool:
        return self.enabled and (not self.dry_run) and self.live_confirm


# ----------------------------------------------------------------------
# 기회 레코드
# ----------------------------------------------------------------------
@dataclass
class ReversalCandidate:
    market_id: str
    condition_id: str
    asset: str
    question: str
    strike_price: float
    current_price: float
    gap_pct: float  # (current - strike) / strike, 부호 포함 (퍼센트 단위 X, 소수)
    seconds_to_close: float
    up_token_id: str
    down_token_id: str
    up_price: float
    down_price: float
    losing_side: str  # "UP" | "DOWN"
    losing_token_id: str
    losing_price_ref: float
    notional_usd: float
    shares: int
    limit_price: float
    detected_at: float
    volume_usd: float = 0.0
    venue: str = "polymarket"  # "polymarket" | "predict"
    # Predict.fun 전용 메타 (polymarket 에서는 기본값)
    predict_market_id: int = 0
    predict_neg_risk: bool = False
    predict_yield_bearing: bool = False
    predict_fee_rate_bps: int = 100


# ----------------------------------------------------------------------
# 메인 클래스
# ----------------------------------------------------------------------
class ReversalSniper:
    """5분 Up/Down 마켓에서 마감 직전 gap 이 거의 0인 마켓에 $0.01 × 100 shares 꼬리 매수.

    Args:
        scanner: MarketScanner. fetch_active_markets / place_order / _gamma_client 등 재사용.
        binance_price_source: 다음 중 하나
            - dict {asset_symbol: PriceEngine-like obj with .latest_price}
            - 단일 PriceEngine-like object (.latest_price float)
            - callable(asset_symbol: str) -> float  (혹은 async callable)
        db_logger: DataLogger (옵션)
        notifier: notifier 모듈 (notifier.notify async) (옵션)
        config: ReversalSniperConfig
        mode: "paper" | "shadow" | "live" — scanner.place_order mode 인자에 그대로 전달.
    """

    def __init__(
        self,
        scanner: Any,
        binance_price_source: Any = None,
        db_logger: Any = None,
        notifier: Any = None,
        config: Optional[ReversalSniperConfig] = None,
        mode: str = "paper",
        predict_client: Any = None,
        predict_fetcher: Any = None,
    ):
        self.scanner = scanner
        self.binance_source = binance_price_source
        self.db_logger = db_logger
        self.notifier = notifier
        self.config = config or ReversalSniperConfig()
        self.mode = mode
        # Predict.fun integration — 둘 다 None 이면 Predict path 비활성
        self.predict_client = predict_client
        self.predict_fetcher = predict_fetcher
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # 상태
        # per-market single-fire 는 venue 가 다르면 충돌하므로 "{venue}:{market_id}" 포맷으로 저장
        self._fired_markets: set[str] = set()
        self._daily_spent: float = 0.0
        self._daily_date: str = ""
        self._trade_ids: dict[str, int] = {}  # fire_key -> DB trade_id (close 시 사용)
        self._pending_resolutions: dict[str, dict[str, Any]] = {}  # fire_key -> snipe meta

        # 통계 (polymarket / predict 통합)
        self.stats = {
            "scans": 0,
            "candidates_evaluated": 0,
            "near_close_seen": 0,
            "gap_filtered_out": 0,
            "opportunities_detected": 0,
            "orders_attempted": 0,
            "orders_placed": 0,
            "orders_failed": 0,
            "resolved_wins": 0,
            "resolved_losses": 0,
            "total_pnl_usd": 0.0,
            "last_opp_at": 0.0,
            # venue-별 세분화
            "poly_orders_placed": 0,
            "predict_orders_placed": 0,
            "predict_scans": 0,
            "predict_opps": 0,
            "predict_orders_failed": 0,
        }

        # JSONL 경로
        self._opp_log_path = DATA_DIR / "reversal_snipes.jsonl"
        self._opp_log_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------
    async def start(self):
        if not self.config.enabled:
            logger.info("[reversal_snipe] REVERSAL_SNIPE_ENABLED=false → 비활성")
            return
        if self._running:
            logger.warning("[reversal_snipe] 이미 실행 중")
            return
        self._running = True
        logger.info(
            "[reversal_snipe] 시작 — dry_run=%s live_confirm=%s gap<=%.4f%% window=%ds "
            "notional=$%.2f shares=%d limit=%.4f daily_cap=$%.1f poll=%ds",
            self.config.dry_run, self.config.live_confirm, self.config.max_gap_pct,
            self.config.enter_window_sec, self.config.notional_usd, self.config.shares_per_order,
            self.config.limit_price, self.config.daily_cap_usd, self.config.poll_interval_sec,
        )
        if self.config.is_live:
            logger.warning("[reversal_snipe] !!! LIVE 모드 활성 — 실자금 집행 가능 !!!")
            await self._notify(
                f"[REVERSAL-SNIPE] LIVE 모드 시작 — 즉시 중단: touch {self.config.kill_switch_file}"
            )
        else:
            logger.info("[reversal_snipe] DRY_RUN 모드 — 탐지/로깅만 (주문 미전송)")

        self._task = asyncio.create_task(self._scan_loop())
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def stop(self):
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # 메인 루프
    # ------------------------------------------------------------------
    async def _scan_loop(self):
        while self._running:
            started = time.time()
            try:
                if self._kill_switch_active():
                    logger.debug("[reversal_snipe] kill switch → skip")
                else:
                    await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[reversal_snipe] scan 실패: {exc}", exc_info=True)
            elapsed = time.time() - started
            sleep_for = max(1.0, self.config.poll_interval_sec - elapsed)
            try:
                await asyncio.sleep(sleep_for)
            except asyncio.CancelledError:
                break

    async def _scan_once(self):
        """Polymarket + Predict.fun 양쪽을 병렬로 스캔. 일일 캡은 통합 공유."""
        self._reset_daily_if_needed()
        self.stats["scans"] += 1

        if self._daily_spent >= self.config.daily_cap_usd:
            logger.debug("[reversal_snipe] 일일 캡 소진")
            return

        # 두 venue 를 병렬 실행 — 한쪽 실패가 다른쪽 중단시키지 않게 gather(return_exceptions)
        tasks = [self._scan_polymarket_markets()]
        predict_on = (
            self.config.predict_enabled
            and self.predict_client is not None
            and self.predict_fetcher is not None
        )
        if predict_on:
            tasks.append(self._scan_predict_markets())

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError):
                logger.debug(f"[reversal_snipe] venue scan 예외: {r}")

    async def _scan_polymarket_markets(self):
        """Polymarket 5분 Up/Down 마켓 스캔."""
        if self._daily_spent >= self.config.daily_cap_usd:
            return
        markets = await self._fetch_all_5m_markets()
        if not markets:
            return

        now = time.time()
        for market, asset in markets:
            try:
                await self._evaluate_market(market, asset, now)
            except Exception as exc:
                logger.debug(f"[reversal_snipe] 평가 실패 {market.get('id', '?')}: {exc}")
            if self._daily_spent >= self.config.daily_cap_usd:
                logger.info("[reversal_snipe] 일일 캡 도달 → polymarket 스캔 중단")
                break

    async def _scan_predict_markets(self):
        """Predict.fun 1시간 Up/Down 마켓 스캔 (BTC/ETH/SOL 만 — BNB 는 Binance USD 페어 없음).

        Polymarket 보다 창이 넓고 (60s default) gap 허용치도 큼 (0.1%).
        """
        if self.predict_client is None or self.predict_fetcher is None:
            return
        if self._daily_spent >= self.config.daily_cap_usd:
            return

        self.stats["predict_scans"] += 1
        try:
            raw_markets = await self.predict_fetcher.fetch_open_crypto_markets()
        except Exception as exc:
            logger.debug(f"[reversal_snipe] predict fetch 실패: {exc}")
            return
        if not raw_markets:
            return

        # BTC/ETH/SOL 만 (Binance 에서 USDT 페어로 현재가/strike 조회 가능)
        allowed_assets = {"BTC", "ETH", "SOL"}
        now = time.time()
        for m in raw_markets:
            try:
                if m.get("asset", "") not in allowed_assets:
                    continue
                await self._evaluate_predict_market(m, now)
            except Exception as exc:
                logger.debug(f"[reversal_snipe] predict eval 실패 {m.get('id', '?')}: {exc}")
            if self._daily_spent >= self.config.daily_cap_usd:
                logger.info("[reversal_snipe] 일일 캡 도달 → predict 스캔 중단")
                break

    # ------------------------------------------------------------------
    # 마켓 수집
    # ------------------------------------------------------------------
    async def _fetch_all_5m_markets(self) -> list[tuple[dict, str]]:
        """현재와 직전 윈도우의 btc/eth/sol 5분 마켓을 Gamma /events slug 로 조회.

        반환: [(raw_market_dict, asset_symbol), ...]
        """
        gamma = getattr(self.scanner, "_gamma_client", None)
        if gamma is None:
            logger.debug("[reversal_snipe] scanner._gamma_client 미보유 → 마켓 수집 불가")
            return []

        now = time.time()
        current_ws = int(now // WINDOW_SEC) * WINDOW_SEC
        # 이전, 현재 두 개 윈도우만 체크 (마감 직전 포착이 목표)
        timestamps = (current_ws - WINDOW_SEC, current_ws)

        result: list[tuple[dict, str]] = []
        for slug_asset, asset_sym in ASSETS:
            for ts in timestamps:
                slug = f"{slug_asset}-updown-5m-{ts}"
                try:
                    resp = await gamma.get("/events", params={"slug": slug})
                    resp.raise_for_status()
                    data = resp.json()
                    if not data:
                        continue
                    events = data if isinstance(data, list) else [data]
                    for event in events:
                        for raw in event.get("markets", []) or []:
                            if raw.get("closed"):
                                continue
                            result.append((raw, asset_sym))
                except Exception as exc:
                    # 404 / timeout 은 정상 (아직 슬러그 없음) — debug 만
                    logger.debug(f"[reversal_snipe] slug {slug} fetch fail: {exc}")
        return result

    # ------------------------------------------------------------------
    # 단일 마켓 평가
    # ------------------------------------------------------------------
    async def _evaluate_market(self, raw: dict, asset: str, now: float):
        # 기본 메타 추출
        market_id = str(raw.get("conditionId") or raw.get("id") or "")
        if not market_id:
            return
        fire_key = f"polymarket:{market_id}"
        if fire_key in self._fired_markets:
            return  # 단발 발사 원칙

        # expiry 판별
        end_date = raw.get("endDate") or raw.get("end_date_iso") or raw.get("endDateIso") or ""
        expiry_ts = _parse_iso_or_unix(end_date)
        if expiry_ts <= 0:
            return
        seconds_to_close = expiry_ts - now
        if seconds_to_close <= 0:
            return
        if seconds_to_close > self.config.enter_window_sec:
            # 아직 먼 마켓은 스킵 (단, 여기서도 카운트)
            return
        self.stats["near_close_seen"] += 1

        # outcomes / 토큰 / 가격 파싱
        outcomes = _decode_jsonish(raw.get("outcomes"), [])
        clob_ids = _decode_jsonish(raw.get("clobTokenIds"), [])
        prices = _decode_jsonish(raw.get("outcomePrices"), [])
        if len(clob_ids) < 2 or len(outcomes) < 2:
            return

        up_idx, down_idx = 0, 1
        for i, o in enumerate(outcomes):
            label = str(o).strip().lower()
            if label == "up":
                up_idx = i
            elif label == "down":
                down_idx = i
        if up_idx == down_idx:
            return
        try:
            up_token_id = str(clob_ids[up_idx])
            down_token_id = str(clob_ids[down_idx])
        except (IndexError, TypeError):
            return

        up_price = _safe_float(prices[up_idx] if up_idx < len(prices) else 0.5, 0.5)
        down_price = _safe_float(prices[down_idx] if down_idx < len(prices) else 0.5, 0.5)

        # 볼륨 필터 (극소 볼륨 마켓은 스킵)
        volume = _safe_float(raw.get("volume") or raw.get("volumeNum") or 0.0, 0.0)
        if volume < self.config.min_market_volume_usd:
            return

        question = str(raw.get("question") or "")[:120]

        # strike 추출 — 5분 창 시작 = expiry - 300s 기준 Binance 가격
        strike_price = await self._resolve_strike_price(asset, expiry_ts, raw)
        if strike_price <= self.config.min_ref_price:
            # strike 를 못 구하면 판단 불가 → 스킵 (드라이런에서도 로그)
            logger.debug(f"[reversal_snipe] strike 미해결 asset={asset} market={market_id[:10]}")
            return

        # 현재가 (Binance oracle)
        current_price = await self._get_current_price(asset)
        if current_price <= self.config.min_ref_price:
            logger.debug(f"[reversal_snipe] current_price 미해결 asset={asset}")
            return

        gap_signed = (current_price - strike_price) / strike_price  # 소수 (0.0005 = 0.05%)
        gap_pct_abs = abs(gap_signed) * 100.0  # 퍼센트 단위로 변환

        self.stats["candidates_evaluated"] += 1

        if gap_pct_abs > self.config.max_gap_pct:
            self.stats["gap_filtered_out"] += 1
            logger.debug(
                f"[reversal_snipe] gap too wide asset={asset} mkt={market_id[:10]} "
                f"gap={gap_pct_abs:.4f}% (limit={self.config.max_gap_pct:.4f}%) close_in={seconds_to_close:.1f}s"
            )
            return

        # losing side 결정
        # 현재가 > strike ⇒ UP 이 이길 쪽 (가격 ~0.99) → 리버설 = DOWN 승 → DOWN 을 $0.01 에 매수
        # 현재가 < strike ⇒ DOWN 이 이길 쪽 → 리버설 = UP 승 → UP 을 $0.01 에 매수
        # 현재가 == strike ⇒ 완전 타이 (매우 드문 케이스) → up_price/down_price 참고해서 낮은 쪽을 losing 으로
        if gap_signed > 0:
            losing_side = "DOWN"
            losing_token_id = down_token_id
            losing_price_ref = down_price
        elif gap_signed < 0:
            losing_side = "UP"
            losing_token_id = up_token_id
            losing_price_ref = up_price
        else:
            # tie — 가격 더 낮은 쪽 (시장이 믿지 않는 쪽) = 리버설 기대 쪽
            if up_price <= down_price:
                losing_side = "UP"
                losing_token_id = up_token_id
                losing_price_ref = up_price
            else:
                losing_side = "DOWN"
                losing_token_id = down_token_id
                losing_price_ref = down_price

        # 일일 캡 재확인 + notional clip
        remaining_cap = self.config.daily_cap_usd - self._daily_spent
        if remaining_cap <= 0:
            return
        notional = min(self.config.notional_usd, remaining_cap)
        # shares 는 config 고정 (기본 100)
        shares = self.config.shares_per_order

        candidate = ReversalCandidate(
            market_id=market_id,
            condition_id=str(raw.get("conditionId") or market_id),
            asset=asset,
            question=question,
            strike_price=strike_price,
            current_price=current_price,
            gap_pct=gap_signed,
            seconds_to_close=seconds_to_close,
            up_token_id=up_token_id,
            down_token_id=down_token_id,
            up_price=up_price,
            down_price=down_price,
            losing_side=losing_side,
            losing_token_id=losing_token_id,
            losing_price_ref=losing_price_ref,
            notional_usd=notional,
            shares=shares,
            limit_price=self.config.limit_price,
            detected_at=now,
            volume_usd=volume,
        )

        self.stats["opportunities_detected"] += 1
        self.stats["last_opp_at"] = now
        self._append_opp_log(candidate, stage="detected")

        await self._handle_candidate(candidate)

    # ------------------------------------------------------------------
    # 기회 처리 / 주문 발사 (venue dispatcher)
    # ------------------------------------------------------------------
    async def _handle_candidate(self, c: ReversalCandidate):
        if c.venue == "predict":
            await self._handle_predict_candidate(c)
        else:
            await self._handle_polymarket_candidate(c)

    async def _handle_polymarket_candidate(self, c: ReversalCandidate):
        tag = f"poly/{c.asset}/{c.market_id[:10]}"
        fire_key = f"polymarket:{c.market_id}"
        summary = (
            f"[reversal_snipe] {tag} close_in={c.seconds_to_close:.1f}s "
            f"strike={c.strike_price:.4f} cur={c.current_price:.4f} "
            f"gap={c.gap_pct*100:.4f}% losing={c.losing_side} "
            f"(up={c.up_price:.3f}/down={c.down_price:.3f}) vol=${c.volume_usd:,.0f} | {c.question[:60]}"
        )
        logger.info(summary)

        # 재발사 방지 — 탐지 즉시 등록 (DRY 든 LIVE 든)
        self._fired_markets.add(fire_key)

        # DRY_RUN → 로그만
        if not self.config.is_live:
            logger.info(
                f"[DRY-REVERSAL] polymarket {c.asset} gap={abs(c.gap_pct)*100:.4f}% "
                f"losing_side={c.losing_side} @${c.limit_price:.4f} x{c.shares}sh"
            )
            await self._notify(
                f"[REVERSAL-SNIPE DRY] poly {c.asset} gap={abs(c.gap_pct)*100:.4f}% "
                f"losing={c.losing_side} @${c.limit_price:.2f} x{c.shares}sh",
                dedup_key=f"reversal_dry:poly:{c.asset}",
            )
            # 드라이런 지출 추적 — 실감 유지
            self._daily_spent += c.notional_usd
            self._append_opp_log(c, stage="dry_run")
            return

        # LIVE 경로 — 3-lock 재검증
        if not self.config.live_confirm:
            logger.warning("[reversal_snipe] live_confirm=false → 실행 차단")
            return
        if self._kill_switch_active():
            logger.warning("[reversal_snipe] kill switch 활성 → 실행 차단")
            return

        # scanner.place_order 존재 여부 체크
        place = getattr(self.scanner, "place_order", None)
        if place is None or not asyncio.iscoroutinefunction(place):
            logger.error("[reversal_snipe] scanner.place_order 미구현 또는 동기 함수 → 스킵")
            return

        self.stats["orders_attempted"] += 1
        order_id: Optional[str] = None
        try:
            # scanner.place_order(token_id, side, size, price, mode)
            # size 는 달러 notional. shares = round(max(5, size/price), 2) 로 내부 계산됨.
            # $1 × $0.01 = 100 shares 가 나와 scanner 의 min 5/$1 검사 통과.
            order_id = await place(
                token_id=c.losing_token_id,
                side="BUY",
                size=c.notional_usd,
                price=c.limit_price,
                mode="live",
            )
        except Exception as exc:
            self.stats["orders_failed"] += 1
            logger.error(f"[reversal_snipe] 주문 예외 {tag}: {exc}", exc_info=True)
            await self._notify(
                f"[REVERSAL-SNIPE FAIL] poly {c.asset} exc={str(exc)[:100]}",
                dedup_key=f"reversal_fail:poly:{c.asset}",
            )
            return

        if not order_id:
            self.stats["orders_failed"] += 1
            logger.warning(f"[reversal_snipe] 주문 거부 또는 None 반환 {tag}")
            await self._notify(
                f"[REVERSAL-SNIPE REJECT] poly {c.asset} {c.losing_side} @${c.limit_price:.2f}",
                dedup_key=f"reversal_reject:poly:{c.asset}",
            )
            self._append_opp_log(c, stage="rejected")
            return

        # 성공 — 지출 갱신, DB 로깅, 텔레그램
        self.stats["orders_placed"] += 1
        self.stats["poly_orders_placed"] += 1
        self._daily_spent += c.notional_usd
        trade_id = self._log_trade_open(c, order_id)
        if trade_id > 0:
            self._trade_ids[fire_key] = trade_id
        self._pending_resolutions[fire_key] = {
            "candidate": c,
            "order_id": str(order_id),
            "trade_id": trade_id,
            "placed_at": time.time(),
        }
        self._append_opp_log(c, stage="placed", order_id=str(order_id))
        await self._notify(
            f"[REVERSAL-SNIPE LIVE] poly {c.asset} gap={abs(c.gap_pct)*100:.4f}% "
            f"{c.losing_side} @${c.limit_price:.2f} x{c.shares}sh → ord={str(order_id)[:16]}",
            dedup_key=f"reversal_live:poly:{c.asset}",
        )

    async def _handle_predict_candidate(self, c: ReversalCandidate):
        """Predict.fun 리버설 스나이프 주문 경로.

        PredictClient.place_order(market_id:int, token_id:str, side:str, price:float,
                                   shares:int, neg_risk, yield_bearing, fee_rate_bps) → Optional[Dict]
        """
        tag = f"predict/{c.asset}/{str(c.predict_market_id)[:10]}"
        fire_key = f"predict:{c.market_id}"
        summary = (
            f"[reversal_snipe] {tag} close_in={c.seconds_to_close:.1f}s "
            f"strike={c.strike_price:.4f} cur={c.current_price:.4f} "
            f"gap={c.gap_pct*100:.4f}% losing={c.losing_side} | {c.question[:60]}"
        )
        logger.info(summary)

        # 재발사 방지
        self._fired_markets.add(fire_key)

        # DRY_RUN
        if not self.config.is_live:
            logger.info(
                f"[DRY-REVERSAL] predict {c.asset} gap={abs(c.gap_pct)*100:.4f}% "
                f"losing_side={c.losing_side} @${c.limit_price:.4f} x{c.shares}sh"
            )
            await self._notify(
                f"[REVERSAL-SNIPE DRY] predict {c.asset} gap={abs(c.gap_pct)*100:.4f}% "
                f"losing={c.losing_side} @${c.limit_price:.2f} x{c.shares}sh",
                dedup_key=f"reversal_dry:predict:{c.asset}",
            )
            self._daily_spent += c.notional_usd
            self._append_opp_log(c, stage="dry_run")
            return

        # LIVE 경로 — 3-lock 재검증
        if not self.config.live_confirm:
            logger.warning("[reversal_snipe] live_confirm=false → 실행 차단 (predict)")
            return
        if self._kill_switch_active():
            logger.warning("[reversal_snipe] kill switch 활성 → 실행 차단 (predict)")
            return

        if self.predict_client is None:
            logger.error("[reversal_snipe] predict_client=None → LIVE 스킵")
            return

        place = getattr(self.predict_client, "place_order", None)
        if place is None or not asyncio.iscoroutinefunction(place):
            # PredictClient 시그니처가 다를 경우 안전하게 stub
            logger.error("[reversal_snipe] predict_client.place_order 미구현/동기 → LIVE 스킵 (Phase X.1)")
            self.stats["predict_orders_failed"] += 1
            self._append_opp_log(c, stage="stub_live")
            # 드라이-런처럼 취급 — 크래시 방지
            return

        self.stats["orders_attempted"] += 1
        order_result: Any = None
        try:
            # predict place_order 는 "shares" 파라미터를 받음 (달러 notional 이 아님).
            # 100 shares × $0.01 = $1 cost. Predict 최소 notional 요구사항은 SDK 내부 검사에 위임.
            order_result = await place(
                market_id=int(c.predict_market_id),
                token_id=str(c.losing_token_id),
                side="BUY",
                price=float(c.limit_price),
                shares=int(c.shares),
                neg_risk=bool(c.predict_neg_risk),
                yield_bearing=bool(c.predict_yield_bearing),
                fee_rate_bps=int(c.predict_fee_rate_bps),
            )
        except NotImplementedError as exc:
            # Stub path — 상위에서 명시적 미구현 신호
            self.stats["predict_orders_failed"] += 1
            logger.warning(f"[reversal_snipe] predict stub (Phase X.1): {exc}")
            self._append_opp_log(c, stage="stub_live")
            return
        except Exception as exc:
            self.stats["orders_failed"] += 1
            self.stats["predict_orders_failed"] += 1
            logger.error(f"[reversal_snipe] predict 주문 예외 {tag}: {exc}", exc_info=True)
            await self._notify(
                f"[REVERSAL-SNIPE FAIL] predict {c.asset} exc={str(exc)[:100]}",
                dedup_key=f"reversal_fail:predict:{c.asset}",
            )
            return

        # place_order 가 dict 또는 None 반환. None/falsy = 거부
        if not order_result:
            self.stats["orders_failed"] += 1
            self.stats["predict_orders_failed"] += 1
            logger.warning(f"[reversal_snipe] predict 주문 거부 또는 None {tag}")
            await self._notify(
                f"[REVERSAL-SNIPE REJECT] predict {c.asset} {c.losing_side} @${c.limit_price:.2f}",
                dedup_key=f"reversal_reject:predict:{c.asset}",
            )
            self._append_opp_log(c, stage="rejected")
            return

        # 성공
        order_id = ""
        if isinstance(order_result, dict):
            order_id = str(
                order_result.get("orderID")
                or order_result.get("order_id")
                or order_result.get("hash")
                or ""
            )
        else:
            order_id = str(order_result)

        self.stats["orders_placed"] += 1
        self.stats["predict_orders_placed"] += 1
        self._daily_spent += c.notional_usd
        trade_id = self._log_trade_open(c, order_id or "predict_ok")
        if trade_id > 0:
            self._trade_ids[fire_key] = trade_id
        self._pending_resolutions[fire_key] = {
            "candidate": c,
            "order_id": order_id,
            "trade_id": trade_id,
            "placed_at": time.time(),
        }
        self._append_opp_log(c, stage="placed", order_id=order_id)
        await self._notify(
            f"[REVERSAL-SNIPE LIVE] predict {c.asset} gap={abs(c.gap_pct)*100:.4f}% "
            f"{c.losing_side} @${c.limit_price:.2f} x{c.shares}sh → ord={order_id[:16]}",
            dedup_key=f"reversal_live:predict:{c.asset}",
        )

    # ------------------------------------------------------------------
    # Predict.fun 단일 마켓 평가
    # ------------------------------------------------------------------
    async def _evaluate_predict_market(self, m: dict, now: float):
        """PredictMarketFetcher.fetch_open_crypto_markets() 가 반환하는 dict 한 건 평가."""
        market_id_int = m.get("id")
        if market_id_int is None:
            return
        market_id = str(market_id_int)
        fire_key = f"predict:{market_id}"
        if fire_key in self._fired_markets:
            return

        asset = str(m.get("asset", "")).upper()
        if not asset:
            return

        expiry_ts = _safe_float(m.get("expiry_time", 0), 0.0)
        if expiry_ts <= 0:
            return
        seconds_to_close = expiry_ts - now
        if seconds_to_close <= 0:
            return
        if seconds_to_close > self.config.predict_enter_window_sec:
            return

        self.stats["near_close_seen"] += 1

        # strike 가격
        strike = _safe_float(m.get("strike_price", 0), 0.0)
        if strike <= self.config.min_ref_price:
            logger.debug(f"[reversal_snipe] predict strike 미해결 asset={asset} mkt={market_id}")
            return

        # 현재가 — 우선 binance_source (PriceEngine) 사용, 없으면 predict_sniper.get_binance_price REST 폴백
        current_price = await self._get_current_price(asset)
        if current_price <= self.config.min_ref_price:
            current_price = await self._fetch_binance_rest_price(asset)
        if current_price <= self.config.min_ref_price:
            logger.debug(f"[reversal_snipe] predict current_price 미해결 asset={asset}")
            return

        gap_signed = (current_price - strike) / strike
        gap_pct_abs = abs(gap_signed) * 100.0
        self.stats["candidates_evaluated"] += 1

        if gap_pct_abs > self.config.predict_max_gap_pct:
            self.stats["gap_filtered_out"] += 1
            logger.debug(
                f"[reversal_snipe] predict gap too wide asset={asset} mkt={market_id} "
                f"gap={gap_pct_abs:.4f}% (limit={self.config.predict_max_gap_pct:.4f}%) "
                f"close_in={seconds_to_close:.1f}s"
            )
            return

        up_token = str(m.get("up_token", "") or "")
        down_token = str(m.get("down_token", "") or "")
        if not up_token or not down_token:
            return

        # losing side: current > strike → UP 유리 → 리버설 = DOWN 승 → DOWN 매수
        if gap_signed > 0:
            losing_side = "DOWN"
            losing_token_id = down_token
        elif gap_signed < 0:
            losing_side = "UP"
            losing_token_id = up_token
        else:
            # tie — UP 기본
            losing_side = "UP"
            losing_token_id = up_token

        # 일일 캡 재검
        remaining_cap = self.config.daily_cap_usd - self._daily_spent
        if remaining_cap <= 0:
            return

        shares = self.config.predict_shares_per_order
        limit_price = self.config.predict_limit_price
        notional = min(shares * limit_price, remaining_cap)
        if notional <= 0:
            return

        question = str(m.get("question", ""))[:120]

        candidate = ReversalCandidate(
            market_id=market_id,
            condition_id=market_id,
            asset=asset,
            question=question,
            strike_price=strike,
            current_price=current_price,
            gap_pct=gap_signed,
            seconds_to_close=seconds_to_close,
            up_token_id=up_token,
            down_token_id=down_token,
            up_price=0.0,  # predict orderbook 미조회 (cost 절감, Polymarket 과 의미 다름)
            down_price=0.0,
            losing_side=losing_side,
            losing_token_id=losing_token_id,
            losing_price_ref=limit_price,
            notional_usd=notional,
            shares=shares,
            limit_price=limit_price,
            detected_at=now,
            volume_usd=0.0,
            venue="predict",
            predict_market_id=int(market_id_int),
            predict_neg_risk=bool(m.get("neg_risk", False)),
            predict_yield_bearing=bool(m.get("yield_bearing", False)),
            predict_fee_rate_bps=int(m.get("fee_rate_bps", self.config.predict_fee_rate_bps) or self.config.predict_fee_rate_bps),
        )

        self.stats["opportunities_detected"] += 1
        self.stats["predict_opps"] += 1
        self.stats["last_opp_at"] = now
        self._append_opp_log(candidate, stage="detected")
        await self._handle_candidate(candidate)

    async def _fetch_binance_rest_price(self, asset: str) -> float:
        """binance_source 가 비어있을 때 폴백 — predict_sniper.get_binance_price 재사용.

        테스트 / 시작 직후 PriceEngine 워밍업 전 상황 방어.
        """
        try:
            from predict_sniper import get_binance_price  # local import — predict 모듈 의존성 회피
            return await get_binance_price(f"{asset}USDT")
        except Exception as exc:
            logger.debug(f"[reversal_snipe] binance REST fallback 실패 {asset}: {exc}")
            return 0.0

    # ------------------------------------------------------------------
    # strike 해결 로직
    # ------------------------------------------------------------------
    async def _resolve_strike_price(self, asset: str, expiry_ts: float, raw: dict) -> float:
        """strike (candle open) 가격 해결.

        우선순위:
          1) raw['description'] 또는 raw['groupItemTitle'] 등에 숫자 "$109,123.45" 패턴 있으면 파싱
          2) PriceEngine.fetch_strike_price(window_start_ts) 있으면 호출
          3) PriceEngine.latest_price (창 내에서는 strike ≈ latest 근사)

        창 시작 = expiry_ts - 300s.
        """
        # 1) description 파싱 시도
        desc_candidates = []
        for key in ("description", "groupItemTitle", "title", "question"):
            v = raw.get(key)
            if isinstance(v, str):
                desc_candidates.append(v)
        for text in desc_candidates:
            parsed = _parse_strike_from_text(text)
            if parsed > 0:
                return parsed

        # 2) engine.fetch_strike_price 시도
        engine = self._get_engine(asset)
        window_start = expiry_ts - WINDOW_SEC
        if engine is not None:
            fetch = getattr(engine, "fetch_strike_price", None)
            if fetch is not None:
                try:
                    res = fetch(window_start)
                    if asyncio.iscoroutine(res):
                        res = await res
                    px = _safe_float(res, 0.0)
                    if px > 0:
                        return px
                except Exception as exc:
                    logger.debug(f"[reversal_snipe] fetch_strike_price {asset} 실패: {exc}")

            # 3) latest_price 근사
            lp = _safe_float(getattr(engine, "latest_price", 0.0), 0.0)
            if lp > 0:
                return lp

        # 마지막 — binance_source 가 callable 또는 dict 이면 아래 _get_current_price 에 위임
        cur = await self._get_current_price(asset)
        return cur if cur > 0 else 0.0

    async def _get_current_price(self, asset: str) -> float:
        """binance_source 에서 asset 현재가 조회."""
        src = self.binance_source
        if src is None:
            return 0.0

        # dict {asset: engine} 패턴
        if isinstance(src, dict):
            engine = src.get(asset) or src.get(asset.upper()) or src.get(asset.lower())
            if engine is not None:
                lp = _safe_float(getattr(engine, "latest_price", 0.0), 0.0)
                if lp > 0:
                    return lp
            return 0.0

        # 단일 engine-like (property .latest_price)
        lp = getattr(src, "latest_price", None)
        if lp is not None:
            try:
                return float(lp)
            except (TypeError, ValueError):
                pass

        # callable
        if callable(src):
            try:
                res = src(asset)
                if asyncio.iscoroutine(res):
                    res = await res
                return _safe_float(res, 0.0)
            except Exception as exc:
                logger.debug(f"[reversal_snipe] binance_source callable 실패: {exc}")
                return 0.0

        return 0.0

    def _get_engine(self, asset: str) -> Any:
        src = self.binance_source
        if isinstance(src, dict):
            return src.get(asset) or src.get(asset.upper()) or src.get(asset.lower())
        if hasattr(src, "latest_price"):
            return src
        return None

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
            self._fired_markets = set()  # 일별 단발 발사 초기화

    def _append_opp_log(self, c: ReversalCandidate, *, stage: str = "detected", order_id: str = ""):
        """JSONL 에 기회/발사 로그 append (드라이런 포함)."""
        try:
            entry = {
                "ts": c.detected_at,
                "stage": stage,
                "venue": c.venue,
                "asset": c.asset,
                "market_id": c.market_id,
                "condition_id": c.condition_id,
                "question": c.question[:120],
                "strike_price": c.strike_price,
                "current_price": c.current_price,
                "gap_pct": c.gap_pct,  # 소수 (부호 포함)
                "seconds_to_close": c.seconds_to_close,
                "up_price": c.up_price,
                "down_price": c.down_price,
                "losing_side": c.losing_side,
                "losing_token_id": c.losing_token_id,
                "losing_price_ref": c.losing_price_ref,
                "notional_usd": c.notional_usd,
                "shares": c.shares,
                "limit_price": c.limit_price,
                "volume_usd": c.volume_usd,
                "order_id": order_id,
                "mode": "live" if self.config.is_live else ("dry_run" if self.config.dry_run else "disabled"),
                "predict_market_id": c.predict_market_id,
            }
            with self._opp_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug(f"[reversal_snipe] opp log 실패: {exc}")

    def _log_trade_open(self, c: ReversalCandidate, order_id: str) -> int:
        """DB trades 테이블에 open 기록. 실패해도 무시 (로깅만)."""
        if self.db_logger is None:
            return -1
        try:
            # side: Polymarket 관례대로 항상 "BUY" 하되, signal_values 로 UP/DOWN 보존
            # venue 별 market_group / duration / strategy_name 분기
            if c.venue == "predict":
                duration_min = 60.0  # Predict.fun 1시간 마켓 기본
                market_group = f"predict_{c.asset.lower()}_1h_reversal"
                strategy_name = "reversal_snipe_predict"
            else:
                duration_min = WINDOW_SEC / 60.0
                market_group = f"{c.asset.lower()}_5m_reversal"
                strategy_name = "reversal_snipe"
            tid = self.db_logger.log_trade(
                market_id=c.market_id,
                side="BUY",
                size=c.notional_usd,
                entry_price=c.limit_price,
                signal_values={
                    "strategy": strategy_name,
                    "venue": c.venue,
                    "losing_side": c.losing_side,
                    "strike_price": c.strike_price,
                    "current_price": c.current_price,
                    "gap_pct": c.gap_pct,
                    "seconds_to_close": c.seconds_to_close,
                    "up_price": c.up_price,
                    "down_price": c.down_price,
                    "shares": c.shares,
                    "volume_usd": c.volume_usd,
                    "predict_market_id": c.predict_market_id,
                },
                model_prob=0.5,  # symmetric reversal prior
                market_prob=c.limit_price,
                edge=1.0 - c.limit_price,  # 이론 BE edge
                kelly_fraction=0.0,
                expiry_time=str(int(c.detected_at + c.seconds_to_close)),
                market_question=c.question[:200],
                order_id=str(order_id)[:200],
                mode="live" if self.config.is_live else "paper",
                strategy_name=strategy_name,
                market_liquidity=c.volume_usd,
                minutes_to_expiry=c.seconds_to_close / 60.0,
                asset_symbol=c.asset,
                market_group=market_group,
                market_duration_min=duration_min,
                token_id=str(c.losing_token_id)[:200],
            )
            return int(tid) if tid is not None else -1
        except Exception as exc:
            logger.warning(f"[reversal_snipe] DB log 실패: {exc}")
            return -1

    async def _notify(self, msg: str, *, dedup_key: Optional[str] = None):
        n = self.notifier
        if n is None:
            return
        try:
            fn = getattr(n, "notify", None)
            if fn is None:
                return
            kwargs: dict[str, Any] = {"dedup_seconds": self.config.notify_dedup_sec}
            if dedup_key:
                kwargs["dedup_key"] = dedup_key
            res = fn(msg, **kwargs)
            if asyncio.iscoroutine(res):
                await res
        except Exception as exc:
            logger.debug(f"[reversal_snipe] notify 실패: {exc}")


# ----------------------------------------------------------------------
# 모듈 유틸
# ----------------------------------------------------------------------
def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _decode_jsonish(value: Any, default: Any):
    if value in (None, ""):
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _parse_iso_or_unix(v: Any) -> float:
    """ISO8601 str, unix 숫자 → unix timestamp (float). 실패 0.0."""
    if v is None:
        return 0.0
    try:
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s:
            return 0.0
        if s.isdigit():
            return float(s)
        from datetime import datetime, timezone
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _parse_strike_from_text(text: str) -> float:
    """description/title 에서 'above $109,123.45' 같은 strike 가격 패턴 추출.

    Polymarket 5분 Up/Down 마켓 설명에는 보통
    "Will BTC be above $109,200 at 14:05 UTC?" 형식이 있음.
    """
    if not text:
        return 0.0
    try:
        import re as _re
        # $숫자 (쉼표 + 소수점 허용) — 첫 매치 사용
        m = _re.search(r"\$\s*([0-9][\d,]*(?:\.\d+)?)", text)
        if not m:
            return 0.0
        raw = m.group(1).replace(",", "")
        return _safe_float(raw, 0.0)
    except Exception:
        return 0.0


__all__ = ["ReversalSniper", "ReversalSniperConfig", "ReversalCandidate"]
