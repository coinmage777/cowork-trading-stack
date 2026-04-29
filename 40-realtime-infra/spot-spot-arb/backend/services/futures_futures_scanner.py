"""선선갭(Futures-vs-Futures) 감지기.

Poller.state 에서 각 티커의 거래소별 futures_bbo (bid/ask)를 읽어
모든 거래소 쌍에 대해 arbitrage spread 를 계산한다.

spread_pct = (sell_bid - buy_ask) / buy_ask * 100

기본 설정은 dry-run (감지 + 로그 + Telegram 알림 only).
실제 주문은 Phase 2 에서 추가.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _env(key: str, default: str = '') -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, '').strip() or default)
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, '').strip() or default))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'y', 'on')


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key, '').strip()
    if not raw:
        return default
    return [t.strip().lower() for t in raw.split(',') if t.strip()]


@dataclass
class FFOpportunity:
    ticker: str
    buy_exchange: str
    sell_exchange: str
    buy_ask: float
    sell_bid: float
    spread_pct: float
    notional_usd: float
    ts: int


@dataclass
class FFConfig:
    enabled: bool = True
    poll_interval_sec: int = 10
    min_spread_pct: float = 0.3  # 0.3% = ~0.15% 순수익 (양측 taker 0.075% 감안)
    max_spread_pct: float = 20.0  # 20%+ = 명백 데이터 오류 (다른 토큰, 정지, 오염) → 제외
    exchanges: list[str] = field(default_factory=lambda: ['binance', 'bybit', 'okx', 'bitget', 'gate', 'htx'])
    top_n_logged: int = 5
    notify_threshold_pct: float = 0.5  # 텔레그램 알림은 0.5%+ 만
    notify_cooldown_sec: int = 600  # 같은 (ticker, buy_ex, sell_ex) 10분 쿨다운

    # Executor (Phase 2)
    executor_enabled: bool = False       # 실행 플래그 — 기본 FALSE
    executor_dry_run: bool = True        # DRY RUN — 기본 TRUE (로그만)
    enter_threshold_pct: float = 0.8     # 진입은 0.8%+ (보수적)
    notional_usd: float = 30.0           # 한 건 $30
    leverage: int = 3                    # 레버리지 3배
    daily_cap_usd: float = 90.0          # 하루 $90 (3건)
    per_pair_cooldown_min: int = 30      # 같은 (ticker, buy_ex, sell_ex) 30분 쿨다운

    # Bybit ADL guard (2025-12-19 ADL 조건 변경 대응)
    # 8시간 누적 손실 기준 ADL 이 격발되므로, 보험기금 저점/funding 극단 시 Bybit leg 회피.
    bybit_adl_guard_enabled: bool = True
    bybit_adl_guard_min_fund_usd: float = 500_000_000.0  # 보험기금 < $500M → 회피
    bybit_adl_guard_funding_abs_pct: float = 0.5          # |funding| > 0.5% → 회피
    bybit_adl_guard_refresh_sec: int = 300                # 5분 캐시
    bybit_adl_guard_http_timeout_sec: int = 8

    @classmethod
    def load(cls) -> 'FFConfig':
        return cls(
            enabled=_env_bool('FF_SCANNER_ENABLED', True),
            poll_interval_sec=_env_int('FF_SCANNER_POLL_INTERVAL_SEC', 10),
            min_spread_pct=_env_float('FF_SCANNER_MIN_SPREAD_PCT', 0.3),
            max_spread_pct=_env_float('FF_SCANNER_MAX_SPREAD_PCT', 20.0),
            exchanges=_env_list('FF_SCANNER_EXCHANGES', ['binance', 'bybit', 'okx', 'bitget', 'gate', 'htx']),
            top_n_logged=_env_int('FF_SCANNER_TOP_N_LOGGED', 5),
            notify_threshold_pct=_env_float('FF_SCANNER_NOTIFY_THRESHOLD_PCT', 0.5),
            notify_cooldown_sec=_env_int('FF_SCANNER_NOTIFY_COOLDOWN_SEC', 600),
            executor_enabled=_env_bool('FF_EXECUTOR_ENABLED', False),
            executor_dry_run=_env_bool('FF_EXECUTOR_DRY_RUN', True),
            enter_threshold_pct=_env_float('FF_EXECUTOR_ENTER_THRESHOLD_PCT', 0.8),
            notional_usd=_env_float('FF_EXECUTOR_NOTIONAL_USD', 30.0),
            leverage=_env_int('FF_EXECUTOR_LEVERAGE', 3),
            daily_cap_usd=_env_float('FF_EXECUTOR_DAILY_CAP_USD', 90.0),
            per_pair_cooldown_min=_env_int('FF_EXECUTOR_PER_PAIR_COOLDOWN_MIN', 30),
            bybit_adl_guard_enabled=_env_bool('FF_SCANNER_BYBIT_ADL_GUARD', True),
            bybit_adl_guard_min_fund_usd=_env_float('BYBIT_ADL_GUARD_MIN_FUND_USD', 500_000_000.0),
            bybit_adl_guard_funding_abs_pct=_env_float('BYBIT_ADL_GUARD_FUNDING_ABS_PCT', 0.5),
            bybit_adl_guard_refresh_sec=_env_int('BYBIT_ADL_GUARD_REFRESH_SEC', 300),
            bybit_adl_guard_http_timeout_sec=_env_int('BYBIT_ADL_GUARD_HTTP_TIMEOUT_SEC', 8),
        )


class FuturesFuturesScanner:
    def __init__(self, poller, telegram_service=None, hedge_service=None) -> None:
        self.cfg = FFConfig.load()
        self.poller = poller
        self.telegram = telegram_service
        self.hedge_service = hedge_service
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_notify_ts: dict[str, float] = {}  # key: ticker|buy|sell
        self._recent_opportunities: list[FFOpportunity] = []  # 최근 50개
        self._total_scans = 0
        self._total_detections = 0

        # Executor state
        self._last_execute_ts: dict[str, float] = {}   # key: ticker|buy|sell
        self._daily_spent_usd: float = 0.0
        self._daily_reset_day: str = ''  # YYYY-MM-DD (UTC)
        self._total_entries_attempted = 0
        self._total_entries_executed = 0
        self._total_entries_dryrun = 0
        self._total_entries_skipped = 0
        self._last_executed: Optional[dict[str, Any]] = None
        self._execute_lock = asyncio.Lock()

        # Bybit ADL guard 캐시
        self._bybit_adl_block_until_ts: float = 0.0   # 이 ts 전까지는 bybit leg 차단
        self._bybit_adl_last_check_ts: float = 0.0
        self._bybit_adl_last_reason: str = ''
        self._bybit_adl_last_fund_usd: float = 0.0
        self._bybit_adl_last_extreme_funding: Optional[dict[str, float]] = None
        self._bybit_adl_skipped_count: int = 0
        self._bybit_adl_refresh_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._running or not self.cfg.enabled:
            if not self.cfg.enabled:
                logger.info('[ff_scanner] disabled via env')
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name='ff_scanner_loop')
        logger.info(
            '[ff_scanner] started | min_spread=%.2f%% exchanges=%s poll=%ds',
            self.cfg.min_spread_pct, self.cfg.exchanges, self.cfg.poll_interval_sec,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._scan()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('[ff_scanner] scan error: %s', exc)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _scan(self) -> None:
        state = self.poller.state
        if not state:
            return
        self._total_scans += 1
        opps: list[FFOpportunity] = []
        now = int(time.time())

        for ticker, gap_result in state.items():
            if not gap_result or not gap_result.exchanges:
                continue
            # 거래소별 futures bbo 수집
            ex_bbo: dict[str, tuple[float, float]] = {}  # ex -> (bid, ask)
            for ex_name in self.cfg.exchanges:
                ex_data = gap_result.exchanges.get(ex_name)
                if not ex_data:
                    continue
                bbo = ex_data.futures_bbo
                if bbo is None:
                    continue
                bid = bbo.bid
                ask = bbo.ask
                if bid is None or ask is None or bid <= 0 or ask <= 0:
                    continue
                if ask <= bid:  # inverted book = 데이터 이상
                    continue
                ex_bbo[ex_name] = (bid, ask)

            if len(ex_bbo) < 2:
                continue

            # 모든 페어 pairwise 비교
            names = list(ex_bbo.keys())
            for i in range(len(names)):
                for j in range(len(names)):
                    if i == j:
                        continue
                    buy_ex = names[i]  # 여기서 매수 (ask 지불)
                    sell_ex = names[j]  # 여기서 매도 (bid 수취)
                    buy_ask = ex_bbo[buy_ex][1]
                    sell_bid = ex_bbo[sell_ex][0]
                    if sell_bid <= buy_ask:
                        continue
                    spread_pct = (sell_bid - buy_ask) / buy_ask * 100.0
                    if spread_pct < self.cfg.min_spread_pct or spread_pct > self.cfg.max_spread_pct:
                        continue
                    opps.append(FFOpportunity(
                        ticker=str(ticker),
                        buy_exchange=buy_ex,
                        sell_exchange=sell_ex,
                        buy_ask=buy_ask,
                        sell_bid=sell_bid,
                        spread_pct=spread_pct,
                        notional_usd=0.0,
                        ts=now,
                    ))

        if not opps:
            return

        # Bybit ADL guard — bybit leg 포함 기회 필터링 (2025-12-19 ADL 조건 변경)
        if self.cfg.bybit_adl_guard_enabled:
            await self._refresh_bybit_adl_guard_if_needed()
            if self._bybit_adl_block_until_ts > time.time():
                before = len(opps)
                opps = [
                    o for o in opps
                    if o.buy_exchange != 'bybit' and o.sell_exchange != 'bybit'
                ]
                blocked = before - len(opps)
                if blocked > 0:
                    self._bybit_adl_skipped_count += blocked
                    logger.info(
                        '[ff_scanner] bybit_adl_guard_blocked: dropped %d bybit-leg opps (reason=%s)',
                        blocked, self._bybit_adl_last_reason,
                    )
                if not opps:
                    return

        # 큰 spread 순 정렬
        opps.sort(key=lambda o: -o.spread_pct)
        self._total_detections += len(opps)

        # 최근 opportunity 기록 (50개 cap)
        self._recent_opportunities.extend(opps[: self.cfg.top_n_logged])
        self._recent_opportunities[:] = self._recent_opportunities[-50:]

        # 상위 N 로그
        logger.info(
            '[ff_scanner] scan: %d opportunities (min=%.2f%%), top:',
            len(opps), self.cfg.min_spread_pct,
        )
        for o in opps[: self.cfg.top_n_logged]:
            logger.info(
                '  %s: buy %s@%.6g → sell %s@%.6g  spread=%.3f%%',
                o.ticker, o.buy_exchange, o.buy_ask,
                o.sell_exchange, o.sell_bid, o.spread_pct,
            )

        # Telegram 알림 (notify_threshold 이상 + 쿨다운)
        for o in opps[:3]:
            if o.spread_pct < self.cfg.notify_threshold_pct:
                continue
            key = f'{o.ticker}|{o.buy_exchange}|{o.sell_exchange}'
            last = self._last_notify_ts.get(key, 0.0)
            if now - last < self.cfg.notify_cooldown_sec:
                continue
            self._last_notify_ts[key] = now
            # low-spread(<3%) alerts are noisy → give filter key
            alert_key = 'ff_low_spread' if o.spread_pct < 3.0 else None
            await self._notify(
                f'🎯 선선갭 {o.ticker} {o.spread_pct:.2f}%\n'
                f'  buy {o.buy_exchange} @{o.buy_ask:.6g}\n'
                f'  sell {o.sell_exchange} @{o.sell_bid:.6g}',
                alert_key=alert_key,
            )

        # Executor (Phase 2) — 최상위 기회 중 threshold 이상이면 진입 시도
        await self._maybe_execute_top(opps, now)

    async def _maybe_execute_top(self, opps: list[FFOpportunity], now: int) -> None:
        """상위 opportunity 에 대해 안전 게이트 통과 시 실제/dry-run 진입.

        절대 경로상 한 번 호출됨 — hedge_service 없거나 executor_enabled=False
        면 no-op.
        """
        if not self.cfg.executor_enabled:
            return
        if self.hedge_service is None:
            logger.debug('[ff_executor] disabled: hedge_service is None')
            return
        if not opps:
            return

        self._reset_daily_if_needed(now)

        # 오름차순 정렬된 opps 는 이미 self._scan 에서 해제됐으므로 기대: 내림차순 정렬
        for opp in opps[: max(1, self.cfg.top_n_logged)]:
            if opp.spread_pct < self.cfg.enter_threshold_pct:
                return  # 나머지는 더 작은 spread 이므로 조기 종료
            # Bybit ADL guard (실행 경로 이중 방어 — scan 단계에서 이미 필터되지만
            # 상태가 refresh 경계에서 바뀌는 경우를 위해 여기서도 재검사)
            if (
                self.cfg.bybit_adl_guard_enabled
                and self._bybit_adl_block_until_ts > time.time()
                and (opp.buy_exchange == 'bybit' or opp.sell_exchange == 'bybit')
            ):
                logger.info(
                    '[ff_executor] skip %s/%s→%s — bybit_adl_guard_blocked (%s)',
                    opp.ticker, opp.buy_exchange, opp.sell_exchange,
                    self._bybit_adl_last_reason,
                )
                self._bybit_adl_skipped_count += 1
                continue
            # Key: (ticker, buy, sell)
            key = f'{opp.ticker}|{opp.buy_exchange}|{opp.sell_exchange}'

            # 쿨다운 게이트
            last_exec = self._last_execute_ts.get(key, 0.0)
            cooldown_sec = max(0, self.cfg.per_pair_cooldown_min) * 60
            if cooldown_sec > 0 and (now - last_exec) < cooldown_sec:
                logger.debug(
                    '[ff_executor] skip %s (cooldown %.0fs left)',
                    key, cooldown_sec - (now - last_exec),
                )
                continue

            # 일일 cap 게이트
            projected = self._daily_spent_usd + self.cfg.notional_usd
            if projected > self.cfg.daily_cap_usd + 1e-9:
                logger.info(
                    '[ff_executor] daily cap reached: spent=$%.2f + $%.2f > cap=$%.2f',
                    self._daily_spent_usd, self.cfg.notional_usd, self.cfg.daily_cap_usd,
                )
                return  # 오늘은 더 이상 진입 불가

            # 중복 잡 방지 (동일 ticker 에 열린 FF job 존재 여부)
            try:
                existing = None
                if hasattr(self.hedge_service, '_latest_open_ff_job'):
                    existing = self.hedge_service._latest_open_ff_job(opp.ticker)
                if existing:
                    logger.info(
                        '[ff_executor] skip %s — existing FF job %s',
                        key, existing.get('job_id'),
                    )
                    continue
            except Exception as exc:
                logger.warning('[ff_executor] existing-check err: %s', exc)
                continue

            self._total_entries_attempted += 1
            self._last_execute_ts[key] = now

            if self.cfg.executor_dry_run:
                self._total_entries_dryrun += 1
                msg = (
                    f'[DRY-FF] would enter {opp.ticker} '
                    f'{opp.buy_exchange}→{opp.sell_exchange} '
                    f'spread={opp.spread_pct:.3f}% '
                    f'notional=${self.cfg.notional_usd:.0f} '
                    f'lev={self.cfg.leverage}x'
                )
                logger.info(msg)
                self._last_executed = {
                    'mode': 'dry_run',
                    'ticker': opp.ticker,
                    'buy_exchange': opp.buy_exchange,
                    'sell_exchange': opp.sell_exchange,
                    'spread_pct': opp.spread_pct,
                    'ts': now,
                }
                # dry-run 도 cap 계산에 반영하여 폭주 방지
                self._daily_spent_usd += self.cfg.notional_usd
                await self._notify(msg)
                continue

            # LIVE 진입 — 반드시 executor_enabled=True AND executor_dry_run=False
            async with self._execute_lock:
                logger.warning(
                    '[ff_executor] LIVE ENTER: %s buy=%s sell=%s spread=%.3f%% notional=$%.2f lev=%dx',
                    opp.ticker, opp.buy_exchange, opp.sell_exchange,
                    opp.spread_pct, self.cfg.notional_usd, self.cfg.leverage,
                )
                try:
                    result = await self.hedge_service.enter_ff(
                        ticker=opp.ticker,
                        buy_exchange=opp.buy_exchange,
                        sell_exchange=opp.sell_exchange,
                        notional_usd=self.cfg.notional_usd,
                        leverage=self.cfg.leverage,
                    )
                except Exception as exc:
                    logger.error('[ff_executor] enter_ff exception: %s', exc)
                    result = {'ok': False, 'code': 'EXCEPTION', 'message': str(exc)}

            ok = bool(result.get('ok'))
            if ok:
                self._total_entries_executed += 1
                self._daily_spent_usd += self.cfg.notional_usd
                self._last_executed = {
                    'mode': 'live',
                    'ticker': opp.ticker,
                    'buy_exchange': opp.buy_exchange,
                    'sell_exchange': opp.sell_exchange,
                    'spread_pct': opp.spread_pct,
                    'ok': True,
                    'ts': now,
                    'job_id': (result.get('job') or {}).get('job_id') if isinstance(result.get('job'), dict) else None,
                }
                status = result.get('status')
                await self._notify(
                    f'🟢 FF ENTERED {opp.ticker} '
                    f'{opp.buy_exchange}→{opp.sell_exchange} '
                    f'{opp.spread_pct:.2f}% status={status}'
                )
            else:
                self._total_entries_skipped += 1
                self._last_executed = {
                    'mode': 'live',
                    'ticker': opp.ticker,
                    'buy_exchange': opp.buy_exchange,
                    'sell_exchange': opp.sell_exchange,
                    'spread_pct': opp.spread_pct,
                    'ok': False,
                    'code': result.get('code'),
                    'message': result.get('message'),
                    'ts': now,
                }
                logger.warning(
                    '[ff_executor] FAILED %s: %s %s',
                    opp.ticker, result.get('code'), result.get('message'),
                )
                await self._notify(
                    f'🔴 FF FAILED {opp.ticker} '
                    f'{opp.buy_exchange}→{opp.sell_exchange} '
                    f'code={result.get("code")} msg={result.get("message")}'
                )
            # 한 스캔당 최대 1건만 진입
            return

    def _reset_daily_if_needed(self, now: int) -> None:
        import time as _t
        today = _t.strftime('%Y-%m-%d', _t.gmtime(now))
        if today != self._daily_reset_day:
            if self._daily_reset_day:
                logger.info(
                    '[ff_executor] daily reset: %s → %s (spent $%.2f)',
                    self._daily_reset_day, today, self._daily_spent_usd,
                )
            self._daily_reset_day = today
            self._daily_spent_usd = 0.0

    async def _refresh_bybit_adl_guard_if_needed(self) -> None:
        """Bybit ADL 가드 상태를 `refresh_sec` 주기로 재평가.

        차단 조건:
            1) 보험기금(insurance fund) USDT 잔고 < min_fund_usd
            2) linear category 에서 |fundingRate| > funding_abs_pct/100 인 심볼 발생
        둘 중 하나면 block_until_ts = now + refresh_sec (재평가 때까지 차단).

        네트워크 에러 시 차단하지 않음 (실행 중단 최소화). 다음 refresh 에서 재시도.
        """
        now = time.time()
        if (now - self._bybit_adl_last_check_ts) < max(60, self.cfg.bybit_adl_guard_refresh_sec):
            return
        # 중복 refresh 방지
        if self._bybit_adl_refresh_lock.locked():
            return
        async with self._bybit_adl_refresh_lock:
            self._bybit_adl_last_check_ts = now
            try:
                import aiohttp  # type: ignore
            except Exception as exc:  # noqa: BLE001
                logger.debug('[ff_scanner] bybit_adl_guard aiohttp missing: %s', exc)
                return

            timeout = aiohttp.ClientTimeout(total=max(3, self.cfg.bybit_adl_guard_http_timeout_sec))
            fund_usd = 0.0
            extreme_funding: Optional[dict[str, float]] = None
            block_reason: str = ''

            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    # 1) 보험기금 조회
                    try:
                        url = 'https://api.bybit.com/v5/market/insurance'
                        async with session.get(url, params={'coin': 'USDT'}) as r:
                            if r.status == 200:
                                data = await r.json()
                                lst = (data.get('result') or {}).get('list') or []
                                for row in lst:
                                    if not isinstance(row, dict):
                                        continue
                                    if str(row.get('coin') or '').upper() != 'USDT':
                                        continue
                                    try:
                                        fund_usd = float(row.get('balance') or 0.0)
                                    except (TypeError, ValueError):
                                        fund_usd = 0.0
                                    break
                    except Exception as exc:  # noqa: BLE001
                        logger.debug('[ff_scanner] bybit insurance fetch err: %s', exc)

                    # 2) funding 극단 체크 (linear top symbols)
                    try:
                        url = 'https://api.bybit.com/v5/market/tickers'
                        async with session.get(url, params={'category': 'linear'}) as r:
                            if r.status == 200:
                                data = await r.json()
                                rows = (data.get('result') or {}).get('list') or []
                                max_abs = 0.0
                                max_sym = ''
                                for row in rows:
                                    if not isinstance(row, dict):
                                        continue
                                    try:
                                        fr = float(row.get('fundingRate') or 0.0)
                                    except (TypeError, ValueError):
                                        continue
                                    # fundingRate 는 비율(예: 0.0001 = 0.01%). pct 로 환산
                                    fr_pct = fr * 100.0
                                    if abs(fr_pct) > max_abs:
                                        max_abs = abs(fr_pct)
                                        max_sym = str(row.get('symbol') or '')
                                if max_abs > 0.0:
                                    extreme_funding = {'symbol_max_abs_pct': max_abs}
                                    if max_sym:
                                        extreme_funding['symbol_max_abs_pct_symbol_hash'] = float(hash(max_sym) % 1_000_000)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug('[ff_scanner] bybit tickers fetch err: %s', exc)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[ff_scanner] bybit_adl_guard session err: %s', exc)
                return

            # 판정
            if fund_usd > 0 and fund_usd < self.cfg.bybit_adl_guard_min_fund_usd:
                block_reason = f'insurance_fund_low:${fund_usd:,.0f}<${self.cfg.bybit_adl_guard_min_fund_usd:,.0f}'
            elif extreme_funding is not None:
                max_abs = float(extreme_funding.get('symbol_max_abs_pct') or 0.0)
                if max_abs > self.cfg.bybit_adl_guard_funding_abs_pct:
                    block_reason = f'extreme_funding:{max_abs:.3f}%>{self.cfg.bybit_adl_guard_funding_abs_pct:.3f}%'

            self._bybit_adl_last_fund_usd = fund_usd
            self._bybit_adl_last_extreme_funding = extreme_funding
            if block_reason:
                self._bybit_adl_last_reason = block_reason
                self._bybit_adl_block_until_ts = now + self.cfg.bybit_adl_guard_refresh_sec
                logger.warning(
                    '[ff_scanner] bybit_adl_guard ARMED: %s (block %ds)',
                    block_reason, self.cfg.bybit_adl_guard_refresh_sec,
                )
            else:
                self._bybit_adl_last_reason = 'clear'
                self._bybit_adl_block_until_ts = 0.0

    async def _notify(self, text: str, alert_key: str | None = None) -> None:
        try:
            if self.telegram is not None:
                await self.telegram._send_message(text, alert_key=alert_key)
        except Exception as exc:
            logger.debug('[ff_scanner] telegram err: %s', exc)

    def status(self) -> dict[str, Any]:
        return {
            'running': self._running,
            'enabled': self.cfg.enabled,
            'total_scans': self._total_scans,
            'total_detections': self._total_detections,
            'config': {
                'min_spread_pct': self.cfg.min_spread_pct,
                'exchanges': self.cfg.exchanges,
                'poll_interval_sec': self.cfg.poll_interval_sec,
                'notify_threshold_pct': self.cfg.notify_threshold_pct,
            },
            'executor': {
                'enabled': self.cfg.executor_enabled,
                'dry_run': self.cfg.executor_dry_run,
                'enter_threshold_pct': self.cfg.enter_threshold_pct,
                'notional_usd': self.cfg.notional_usd,
                'leverage': self.cfg.leverage,
                'daily_cap_usd': self.cfg.daily_cap_usd,
                'per_pair_cooldown_min': self.cfg.per_pair_cooldown_min,
                'daily_spent_usd': self._daily_spent_usd,
                'daily_reset_day': self._daily_reset_day,
                'total_attempted': self._total_entries_attempted,
                'total_executed': self._total_entries_executed,
                'total_dryrun': self._total_entries_dryrun,
                'total_skipped': self._total_entries_skipped,
                'last_executed': self._last_executed,
                'has_hedge_service': self.hedge_service is not None,
            },
            'bybit_adl_guard': {
                'enabled': self.cfg.bybit_adl_guard_enabled,
                'min_fund_usd': self.cfg.bybit_adl_guard_min_fund_usd,
                'funding_abs_pct': self.cfg.bybit_adl_guard_funding_abs_pct,
                'refresh_sec': self.cfg.bybit_adl_guard_refresh_sec,
                'armed': (self._bybit_adl_block_until_ts > time.time()),
                'block_until_ts': self._bybit_adl_block_until_ts,
                'last_check_ts': self._bybit_adl_last_check_ts,
                'last_reason': self._bybit_adl_last_reason,
                'last_fund_usd': self._bybit_adl_last_fund_usd,
                'skipped_count': self._bybit_adl_skipped_count,
            },
        }

    def recent_opportunities(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                'ticker': o.ticker,
                'buy_exchange': o.buy_exchange,
                'sell_exchange': o.sell_exchange,
                'buy_ask': o.buy_ask,
                'sell_bid': o.sell_bid,
                'spread_pct': round(o.spread_pct, 3),
                'ts': o.ts,
            }
            for o in sorted(self._recent_opportunities, key=lambda x: -x.spread_pct)[:limit]
        ]
