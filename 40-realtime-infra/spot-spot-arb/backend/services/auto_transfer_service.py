"""Bithumb → Binance 전송 아비트라지 오케스트레이터.

설계 철학 (auto_trigger + auto_exit 패턴 준수):
- poller.state 에서 실시간 갭 감시
- 역프 감지 (Bithumb ask * usdt_krw < Binance bid, gap < threshold)
- 해당 코인 withdraw 가능 + Binance deposit 가능 확인 시 상태머신 진입
- State machine: PENDING → BITHUMB_BOUGHT → WITHDRAWING → DEPOSITED → BINANCE_SOLD → COMPLETE
- dry_run=True 기본값: 실주문/실출금 호출 절대 없음. 로깅만.
- 실자금 안전장치: daily cap, per-ticker cooldown, kill switch, 동시 실행 제한

주의:
- Bithumb 출금 API 서명/수취인 정보는 `withdraw_service`에 이미 구현됨 — 재구현 금지
- Binance market sell 은 `hedge_service._submit_market_order` 재사용
- 어떤 네트워크를 쓸지 (TRX vs ERC20 등) 자동 선택은 STUB — dry_run에서만 동작,
  LIVE에서는 `ticker_network_map` 수동 설정 필수
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from backend.exchanges import manager as exchange_manager
from backend.exchanges.types import GapResult

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Env helpers (auto_exit_service 패턴 그대로)
# ------------------------------------------------------------------

def _env(key: str, default: str = '') -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, '').strip() or default)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(os.getenv(key, '').strip() or default))
    except (TypeError, ValueError):
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
    return [t.strip().upper() for t in raw.split(',') if t.strip()]


# ------------------------------------------------------------------
# State machine
# ------------------------------------------------------------------

STATE_PENDING = 'PENDING'
STATE_BITHUMB_BOUGHT = 'BITHUMB_BOUGHT'
STATE_WITHDRAWING = 'WITHDRAWING'
STATE_DEPOSITED = 'DEPOSITED'
STATE_BINANCE_SOLD = 'BINANCE_SOLD'
STATE_COMPLETE = 'COMPLETE'
STATE_FAILED = 'FAILED'
STATE_STUCK = 'STUCK'
STATE_ABORTED = 'ABORTED'


TRANSFER_JOBS_FILE = os.path.join(
    os.path.dirname(__file__), '..', '..', 'transfer_jobs.json',
)


# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

@dataclass
class AutoTransferConfig:
    enabled: bool = False
    dry_run: bool = True
    watchlist: list[str] = field(default_factory=lambda: ['SPK', 'TRX', 'XLM', 'EOS', 'ADA', 'XRP'])
    target_exchange: str = 'binance'

    # 역프 진입 기준: gap < threshold (parity = 10000).
    # 예: 9800 = 2%+ 역프 (Bithumb 가격이 Binance USDT 환산가보다 2% 저렴)
    gap_threshold: float = 9800.0

    nominal_usd: float = 30.0
    min_net_usd: float = 2.0

    # 수수료 상수 (bps 기준으로 관리하면 편리하지만 직관적인 비율 유지)
    bithumb_fee_pct: float = 0.0004   # 0.04%
    binance_fee_pct: float = 0.001    # 0.1% (taker market)
    drift_buffer_pct: float = 0.003   # 0.3% 가격 drift 버퍼 (10분 전송 동안 불리한 변동)

    daily_cap_usd: float = 100.0
    per_ticker_cooldown_min: int = 30
    poll_interval_sec: int = 20
    withdraw_timeout_min: int = 20
    kill_switch_file: str = 'data/KILL_TRANSFER'

    # 티커별 네트워크 맵 — LIVE 사용 시 반드시 수동 설정 필요
    # 예: {"SPK": "POLYGON", "TRX": "TRX", "XLM": "XLM"}
    ticker_network_map: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls) -> 'AutoTransferConfig':
        raw_map = _env('AUTO_TRANSFER_TICKER_NETWORK_MAP', '')
        ticker_map: dict[str, str] = {}
        if raw_map:
            try:
                parsed = json.loads(raw_map)
                if isinstance(parsed, dict):
                    ticker_map = {
                        str(k).strip().upper(): str(v).strip()
                        for k, v in parsed.items()
                        if k and v
                    }
            except Exception as exc:
                logger.warning(
                    '[auto_transfer] invalid AUTO_TRANSFER_TICKER_NETWORK_MAP json: %s', exc,
                )

        return cls(
            enabled=_env_bool('AUTO_TRANSFER_ENABLED', False),
            dry_run=_env_bool('AUTO_TRANSFER_DRY_RUN', True),
            watchlist=_env_list(
                'AUTO_TRANSFER_WATCHLIST',
                ['SPK', 'TRX', 'XLM', 'EOS', 'ADA', 'XRP'],
            ),
            target_exchange=_env('AUTO_TRANSFER_TARGET_EXCHANGE', 'binance').lower(),
            gap_threshold=_env_float('AUTO_TRANSFER_GAP_THRESHOLD', 9800.0),
            nominal_usd=_env_float('AUTO_TRANSFER_NOMINAL_USD', 30.0),
            min_net_usd=_env_float('AUTO_TRANSFER_MIN_NET_USD', 2.0),
            bithumb_fee_pct=_env_float('AUTO_TRANSFER_BITHUMB_FEE_PCT', 0.0004),
            binance_fee_pct=_env_float('AUTO_TRANSFER_BINANCE_FEE_PCT', 0.001),
            drift_buffer_pct=_env_float('AUTO_TRANSFER_DRIFT_BUFFER_PCT', 0.003),
            daily_cap_usd=_env_float('AUTO_TRANSFER_DAILY_CAP_USD', 100.0),
            per_ticker_cooldown_min=_env_int('AUTO_TRANSFER_PER_TICKER_COOLDOWN_MIN', 30),
            poll_interval_sec=_env_int('AUTO_TRANSFER_POLL_INTERVAL_SEC', 20),
            withdraw_timeout_min=_env_int('AUTO_TRANSFER_WITHDRAW_TIMEOUT_MIN', 20),
            kill_switch_file=_env('AUTO_TRANSFER_KILL_SWITCH_FILE', 'data/KILL_TRANSFER'),
            ticker_network_map=ticker_map,
        )


# ------------------------------------------------------------------
# Job store (mirror of hedge_jobs.py pattern, simplified)
# ------------------------------------------------------------------

class TransferJobStore:
    """로컬 JSON 파일에 transfer job 영속화."""

    def __init__(self, path: str = TRANSFER_JOBS_FILE) -> None:
        self._path = path
        self._items: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if not os.path.exists(self._path):
                self._items = []
                return
            with open(self._path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            raw_items = data.get('items', [])
            if not isinstance(raw_items, list):
                raw_items = []
            self._items = [item for item in raw_items if isinstance(item, dict)]
            logger.info('[auto_transfer] loaded %d transfer jobs', len(self._items))
        except Exception as exc:
            logger.error('[auto_transfer] job load failed: %s', exc)
            self._items = []

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path) or '.', exist_ok=True)
            with open(self._path, 'w', encoding='utf-8') as fh:
                json.dump({'items': self._items}, fh, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error('[auto_transfer] job save failed: %s', exc)

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        job = {
            'job_id': str(uuid.uuid4()),
            'created_at': now,
            'updated_at': now,
            'state': STATE_PENDING,
            'events': [],
            **payload,
        }
        self._items.append(job)
        self._save()
        return dict(job)

    def update(self, job_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        for idx, item in enumerate(self._items):
            if item.get('job_id') != job_id:
                continue
            merged = {**item, **updates, 'updated_at': int(time.time())}
            self._items[idx] = merged
            self._save()
            return dict(merged)
        return None

    def append_event(self, job_id: str, event: dict[str, Any]) -> None:
        for idx, item in enumerate(self._items):
            if item.get('job_id') != job_id:
                continue
            events = list(item.get('events') or [])
            events.append({'ts': int(time.time()), **event})
            item['events'] = events[-40:]  # 최근 40개만 유지
            item['updated_at'] = int(time.time())
            self._items[idx] = item
            self._save()
            return

    def list_jobs(self, limit: int = 50, ticker: Optional[str] = None) -> list[dict[str, Any]]:
        items = self._items
        if ticker:
            target = ticker.strip().upper()
            items = [it for it in items if str(it.get('ticker', '')).upper() == target]
        sorted_items = sorted(
            items, key=lambda it: int(it.get('created_at', 0) or 0), reverse=True,
        )
        return [dict(it) for it in sorted_items[:max(0, int(limit))]]

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        for item in self._items:
            if item.get('job_id') == job_id:
                return dict(item)
        return None

    def active_jobs(self) -> list[dict[str, Any]]:
        terminal = {STATE_COMPLETE, STATE_FAILED, STATE_STUCK, STATE_ABORTED}
        return [dict(it) for it in self._items if str(it.get('state')) not in terminal]

    def active_ticker_set(self) -> set[str]:
        return {
            str(it.get('ticker', '')).upper()
            for it in self.active_jobs()
            if it.get('ticker')
        }


# ------------------------------------------------------------------
# Safety state
# ------------------------------------------------------------------

def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


@dataclass
class TransferSafetyState:
    daily_spent_usd: float = 0.0
    daily_reset_epoch: float = field(default_factory=_today_midnight_epoch)
    last_entry_ts_per_ticker: dict[str, float] = field(default_factory=dict)
    last_decisions: list[dict] = field(default_factory=list)


class TransferSafetyGate:
    def __init__(self, cfg: AutoTransferConfig, job_store: TransferJobStore) -> None:
        self.cfg = cfg
        self.jobs = job_store
        self.state = TransferSafetyState()

    def _rollover(self) -> None:
        today = _today_midnight_epoch()
        if today > self.state.daily_reset_epoch:
            logger.info(
                '[auto_transfer] daily rollover: spent=%.2f reset',
                self.state.daily_spent_usd,
            )
            self.state.daily_spent_usd = 0.0
            self.state.daily_reset_epoch = today

    def kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:
            return False

    def can_enter(self, ticker: str, nominal_usd: float) -> tuple[bool, str]:
        if not self.cfg.enabled:
            return False, 'disabled'
        if self.kill_switch_active():
            return False, f'kill_switch {self.cfg.kill_switch_file}'

        self._rollover()
        if self.state.daily_spent_usd + nominal_usd > self.cfg.daily_cap_usd:
            return False, (
                f'daily_cap '
                f'({self.state.daily_spent_usd:.2f}+{nominal_usd:.2f}>'
                f'{self.cfg.daily_cap_usd:.2f})'
            )

        # 동일 티커 동시 실행 차단
        active = self.jobs.active_ticker_set()
        if ticker.upper() in active:
            return False, f'active_transfer_exists on {ticker}'

        last_ts = self.state.last_entry_ts_per_ticker.get(ticker.upper(), 0.0)
        cooldown_sec = self.cfg.per_ticker_cooldown_min * 60
        elapsed = time.time() - last_ts
        if elapsed < cooldown_sec:
            return False, f'cooldown {int(cooldown_sec - elapsed)}s on {ticker}'

        return True, 'ok'

    def record_entry(self, ticker: str, nominal_usd: float) -> None:
        self._rollover()
        self.state.daily_spent_usd += nominal_usd
        self.state.last_entry_ts_per_ticker[ticker.upper()] = time.time()

    def status(self) -> dict[str, Any]:
        self._rollover()
        now = time.time()
        cooldown_sec = self.cfg.per_ticker_cooldown_min * 60
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'kill_switch_active': self.kill_switch_active(),
            'daily_spent_usd': round(self.state.daily_spent_usd, 2),
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'cooldown_tickers': {
                k: max(0, int(cooldown_sec - (now - v)))
                for k, v in self.state.last_entry_ts_per_ticker.items()
                if (now - v) < cooldown_sec
            },
            'active_tickers': sorted(self.jobs.active_ticker_set()),
        }


# ------------------------------------------------------------------
# Main service
# ------------------------------------------------------------------

class AutoTransferService:
    """Bithumb → Binance 전송 아비트라지 자동화.

    Dependencies:
      - poller (PollerService): 실시간 갭 + 네트워크 상태
      - bithumb_client: 스팟 매수용 (None 허용 — hedge_service 경유)
      - withdraw_service (WithdrawService): 출금 preview/execute
      - hedge_service (HedgeTradeService): market buy/sell helper 재사용
      - telegram_service (optional): 알림
    """

    def __init__(
        self,
        poller,
        bithumb_client=None,
        withdraw_service=None,
        hedge_service=None,
        telegram_service=None,
    ) -> None:
        self.cfg = AutoTransferConfig.load()
        self.poller = poller
        self.bithumb_client = bithumb_client  # 예비 포인터 (현재는 hedge_service 경유)
        self.withdraw_service = withdraw_service
        self.hedge_service = hedge_service
        self.telegram = telegram_service

        self.jobs = TransferJobStore()
        self.safety = TransferSafetyGate(self.cfg, self.jobs)

        self._task: Optional[asyncio.Task] = None
        self._running = False

        self._total_triggers = 0
        self._total_executes = 0
        self._total_completed = 0

        # dependency completeness — 누락이면 로그 + skip silently
        self._deps_ok = all([
            self.poller is not None,
            self.withdraw_service is not None,
            self.hedge_service is not None,
        ])
        if not self._deps_ok:
            logger.warning(
                '[auto_transfer] missing deps (poller=%s, withdraw=%s, hedge=%s) — '
                'service inert',
                self.poller is not None,
                self.withdraw_service is not None,
                self.hedge_service is not None,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name='auto_transfer_loop')
        logger.info(
            '[auto_transfer] started | enabled=%s dry_run=%s watchlist=%s '
            'target=%s gap<%.0f nominal=$%.0f min_net=$%.2f',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.watchlist,
            self.cfg.target_exchange, self.cfg.gap_threshold,
            self.cfg.nominal_usd, self.cfg.min_net_usd,
        )
        await self._notify(
            f'🚚 Auto-transfer started\n'
            f'enabled={self.cfg.enabled} dry_run={self.cfg.dry_run}\n'
            f'watchlist={",".join(self.cfg.watchlist)}\n'
            f'gap<{self.cfg.gap_threshold:.0f} → ${self.cfg.nominal_usd:.0f} '
            f'via {self.cfg.target_exchange}',
            alert_key='started_auto_transfer',
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info('[auto_transfer] stopped')

    async def _loop(self) -> None:
        while self._running:
            try:
                if self._deps_ok and self.cfg.enabled:
                    await self._tick()
                    await self._monitor_active_jobs()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('[auto_transfer] tick error: %s', exc, exc_info=True)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        state = self.poller.state if self.poller else {}
        if not state:
            return

        # 1) watchlist 순차 + 광역 스캐너 후보 합산
        candidates = self._collect_candidates(state)

        for ticker, gap, source in candidates:
            self._total_triggers += 1
            ok, reason = self.safety.can_enter(ticker, self.cfg.nominal_usd)
            if not ok:
                logger.debug(
                    '[auto_transfer] %s gap=%.1f BLOCKED: %s', ticker, gap, reason,
                )
                continue

            gap_result = state.get(ticker)
            if gap_result is None:
                continue

            viable, context = self._assess_opportunity(ticker, gap_result)
            if not viable:
                logger.info(
                    '[auto_transfer] %s gap=%.1f not viable: %s',
                    ticker, gap, context.get('reason', '?'),
                )
                continue

            logger.info(
                '[auto_transfer] %s gap=%.1f src=%s → start job dry_run=%s '
                '(est_net=$%.2f)',
                ticker, gap, source, self.cfg.dry_run,
                float(context.get('est_net_usd', 0.0)),
            )
            await self._start_job(ticker=ticker, gap=gap, source=source, context=context)

    def _collect_candidates(self, state: dict[str, GapResult]) -> list[tuple[str, float, str]]:
        target_ex = self.cfg.target_exchange
        out: list[tuple[str, float, str]] = []
        watchset = {t.upper() for t in self.cfg.watchlist}

        for ticker, gap_result in state.items():
            tk = str(ticker).upper()
            if not gap_result:
                continue
            ex_data = gap_result.exchanges.get(target_ex)
            if not ex_data:
                continue
            # 우리는 Binance 스팟 bid 기준 (현물 매도할 곳)
            spot_gap = getattr(ex_data, 'spot_gap', None)
            if spot_gap is None:
                # spot_gap 없으면 futures_gap이라도 참고 (근사)
                spot_gap = getattr(ex_data, 'futures_gap', None)
            if spot_gap is None:
                continue
            if spot_gap >= self.cfg.gap_threshold:
                continue
            # 상식 범위 (50% 이상 역프는 가격 오류 의심)
            if spot_gap < 5000:
                continue

            source = 'watchlist' if tk in watchset else 'wide_scan'
            out.append((tk, float(spot_gap), source))

        # gap 오름차순 (역프 큰 순) + 상위 5개 제한
        out.sort(key=lambda x: x[1])
        return out[:5]

    # ------------------------------------------------------------------
    # Opportunity assessment (fee + profit calc)
    # ------------------------------------------------------------------

    def _assess_opportunity(
        self,
        ticker: str,
        gap_result: GapResult,
    ) -> tuple[bool, dict[str, Any]]:
        """수익성 + 출금/입금 상태 체크.

        Returns (viable, context).
        """
        ctx: dict[str, Any] = {'ticker': ticker}

        bith = gap_result.bithumb
        bithumb_ask = getattr(bith, 'ask', None)
        usdt_krw = getattr(bith, 'usdt_krw_last', None)
        if not bithumb_ask or bithumb_ask <= 0:
            ctx['reason'] = 'bithumb_ask_unavailable'
            return False, ctx
        if not usdt_krw or usdt_krw <= 0:
            ctx['reason'] = 'usdt_krw_unavailable'
            return False, ctx

        ex_data = gap_result.exchanges.get(self.cfg.target_exchange)
        if not ex_data:
            ctx['reason'] = f'{self.cfg.target_exchange}_missing'
            return False, ctx

        spot_bbo = getattr(ex_data, 'spot_bbo', None)
        if spot_bbo is None or not getattr(spot_bbo, 'bid', None) or spot_bbo.bid <= 0:
            ctx['reason'] = f'{self.cfg.target_exchange}_spot_bid_unavailable'
            return False, ctx
        target_bid_usdt = float(spot_bbo.bid)

        # Bithumb 가격을 USDT 환산: ask_krw / usdt_krw
        bithumb_ask_usdt = float(bithumb_ask) / float(usdt_krw)
        if bithumb_ask_usdt <= 0:
            ctx['reason'] = 'bithumb_ask_usdt_invalid'
            return False, ctx

        # gross edge (매도가 - 매수가) / 매수가
        gross_edge_pct = (target_bid_usdt - bithumb_ask_usdt) / bithumb_ask_usdt
        if gross_edge_pct <= 0:
            ctx['reason'] = f'no_gross_edge ({gross_edge_pct:.4f})'
            return False, ctx

        nominal = self.cfg.nominal_usd
        qty = nominal / bithumb_ask_usdt

        # 출금 수수료 (Bithumb 네트워크 테이블)
        withdrawal_fee_coin = self._pick_bithumb_withdraw_fee(gap_result, ticker)
        if withdrawal_fee_coin is None:
            ctx['reason'] = 'bithumb_withdraw_fee_unavailable'
            return False, ctx

        # coin 단위 수수료 → USDT 환산 (target 거래소 bid 기준 = 실제 매도가)
        withdrawal_fee_usdt = withdrawal_fee_coin * target_bid_usdt

        # 예상 비용 합계 (USDT)
        bithumb_fee_usdt = nominal * self.cfg.bithumb_fee_pct
        binance_fee_usdt = nominal * (1 + gross_edge_pct) * self.cfg.binance_fee_pct
        drift_buffer_usdt = nominal * self.cfg.drift_buffer_pct
        total_cost_usdt = (
            bithumb_fee_usdt + binance_fee_usdt + withdrawal_fee_usdt + drift_buffer_usdt
        )
        est_gross_usdt = nominal * gross_edge_pct
        est_net_usdt = est_gross_usdt - total_cost_usdt

        ctx.update({
            'bithumb_ask_krw': float(bithumb_ask),
            'usdt_krw': float(usdt_krw),
            'bithumb_ask_usdt': bithumb_ask_usdt,
            'target_bid_usdt': target_bid_usdt,
            'gross_edge_pct': round(gross_edge_pct, 6),
            'qty': qty,
            'withdrawal_fee_coin': float(withdrawal_fee_coin),
            'withdrawal_fee_usdt': withdrawal_fee_usdt,
            'bithumb_fee_usdt': bithumb_fee_usdt,
            'binance_fee_usdt': binance_fee_usdt,
            'drift_buffer_usdt': drift_buffer_usdt,
            'est_gross_usdt': est_gross_usdt,
            'est_net_usd': est_net_usdt,
            'nominal_usd': nominal,
        })

        if est_net_usdt < self.cfg.min_net_usd:
            ctx['reason'] = (
                f'net_below_threshold ({est_net_usdt:.2f} < {self.cfg.min_net_usd:.2f})'
            )
            return False, ctx

        # 3) 네트워크 상태 체크
        network = self.cfg.ticker_network_map.get(ticker)
        if not network:
            # 네트워크가 단일 withdraw-enabled 인 경우만 자동 선택 허용
            network = self._auto_pick_network(gap_result)
            if not network:
                ctx['reason'] = 'network_not_configured'
                return False, ctx

        # Bithumb withdraw 가능 확인
        if not self._bithumb_can_withdraw(gap_result, network):
            ctx['reason'] = f'bithumb_withdraw_disabled_{network}'
            return False, ctx

        # 타겟 거래소 deposit 가능 확인
        if not self._target_can_deposit(gap_result, network):
            ctx['reason'] = f'{self.cfg.target_exchange}_deposit_disabled_{network}'
            return False, ctx

        ctx['network'] = network
        return True, ctx

    def _pick_bithumb_withdraw_fee(
        self,
        gap_result: GapResult,
        ticker: str,
    ) -> Optional[float]:
        """Bithumb 측 출금 수수료 (coin 단위) 추출."""
        bith = gap_result.bithumb
        # 1) network 테이블 우선
        for net in (bith.networks or []):
            if net.withdraw and net.fee is not None:
                return float(net.fee)
        # 2) 출금한도 expected_fee fallback
        wl = getattr(bith, 'withdrawal_limit', None)
        if wl is not None and getattr(wl, 'expected_fee', None) is not None:
            try:
                return float(wl.expected_fee)
            except (TypeError, ValueError):
                return None
        return None

    def _auto_pick_network(self, gap_result: GapResult) -> Optional[str]:
        """Bithumb 에서 withdraw 가능 && target 에서 deposit 가능한 네트워크가 유일한 경우 자동 선택.

        복수면 ticker_network_map 수동 설정 강제.
        """
        bith = gap_result.bithumb
        ex_data = gap_result.exchanges.get(self.cfg.target_exchange)
        if not ex_data:
            return None

        def _norm(x: str) -> str:
            return ''.join(ch for ch in (x or '').upper() if ch.isalnum())

        bithumb_wd = {
            _norm(n.network): n.network
            for n in (bith.networks or []) if n.withdraw
        }
        target_dp = {
            _norm(n.network): n.network
            for n in (ex_data.networks or []) if n.deposit
        }
        common = set(bithumb_wd.keys()) & set(target_dp.keys())
        if len(common) == 1:
            key = next(iter(common))
            return bithumb_wd[key]
        return None

    def _bithumb_can_withdraw(self, gap_result: GapResult, network: str) -> bool:
        def _norm(x: str) -> str:
            return ''.join(ch for ch in (x or '').upper() if ch.isalnum())
        target_norm = _norm(network)
        for n in (gap_result.bithumb.networks or []):
            if _norm(n.network) == target_norm and n.withdraw:
                return True
        return False

    def _target_can_deposit(self, gap_result: GapResult, network: str) -> bool:
        ex_data = gap_result.exchanges.get(self.cfg.target_exchange)
        if not ex_data:
            return False

        def _norm(x: str) -> str:
            return ''.join(ch for ch in (x or '').upper() if ch.isalnum())
        target_norm = _norm(network)
        for n in (ex_data.networks or []):
            if _norm(n.network) == target_norm and n.deposit:
                return True
        return False

    # ------------------------------------------------------------------
    # State machine driver
    # ------------------------------------------------------------------

    async def _start_job(
        self,
        ticker: str,
        gap: float,
        source: str,
        context: dict[str, Any],
    ) -> None:
        job = self.jobs.create({
            'ticker': ticker,
            'source': source,
            'gap_at_trigger': round(gap, 2),
            'dry_run': self.cfg.dry_run,
            'network': context.get('network'),
            'nominal_usd': self.cfg.nominal_usd,
            'est_net_usd': round(float(context.get('est_net_usd', 0.0)), 4),
            'context': context,
            'target_exchange': self.cfg.target_exchange,
        })
        self.safety.record_entry(ticker, self.cfg.nominal_usd)

        if self.cfg.dry_run:
            decision = {
                'ts': int(time.time()),
                'ticker': ticker,
                'gap': round(gap, 2),
                'source': source,
                'context': {k: v for k, v in context.items() if k != 'reason'},
                'result': 'dry_run_skip',
            }
            self.safety.state.last_decisions.append(decision)
            self.safety.state.last_decisions[:] = self.safety.state.last_decisions[-20:]
            self.jobs.update(job['job_id'], {'state': STATE_COMPLETE, 'dry_run_result': 'skip'})
            self.jobs.append_event(job['job_id'], {
                'type': 'dry_run_decision',
                'detail': {
                    'gap': round(gap, 2),
                    'est_net_usd': float(context.get('est_net_usd', 0.0)),
                    'network': context.get('network'),
                },
            })
            await self._notify(
                f'🧪 [DRY-RUN] {ticker} gap={gap:.0f} network={context.get("network")}\n'
                f'  would transfer ${self.cfg.nominal_usd:.0f} est_net=$'
                f'{float(context.get("est_net_usd", 0.0)):.2f}',
                alert_key='dry_auto_transfer',
            )
            return

        # LIVE path — 각 단계를 순차 실행 (실패/timeout 시 상태 전이)
        self._total_executes += 1
        try:
            await self._drive_live_job(job, context)
        except NotImplementedError as exc:
            self.jobs.update(job['job_id'], {
                'state': STATE_FAILED,
                'error_code': 'NOT_IMPLEMENTED',
                'error_message': str(exc),
            })
            self.jobs.append_event(job['job_id'], {'type': 'aborted', 'reason': str(exc)})
            logger.error('[auto_transfer] %s LIVE aborted (stub): %s', ticker, exc)
            await self._notify(f'❌ {ticker} LIVE aborted (not implemented): {exc}')
        except Exception as exc:
            self.jobs.update(job['job_id'], {
                'state': STATE_FAILED,
                'error_code': 'EXCEPTION',
                'error_message': str(exc),
            })
            self.jobs.append_event(job['job_id'], {'type': 'exception', 'detail': str(exc)})
            logger.error('[auto_transfer] %s LIVE exception: %s', ticker, exc, exc_info=True)
            await self._notify(f'❌ {ticker} LIVE exception: {exc}')

    async def _drive_live_job(
        self,
        job: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """LIVE 실행 (dry_run=False). 실자금.

        STUB: Bithumb deposit network → Binance deposit address 자동 해석, 수취인 정보,
        출금 서명은 withdraw_service가 커버. 단 아래 단계는 아직 end-to-end 검증 전이므로
        `AUTO_TRANSFER_LIVE_CONFIRM=true` 환경변수로 명시적 허용하지 않으면 중단.
        """
        job_id = job['job_id']
        ticker = job['ticker']
        network = job.get('network') or context.get('network')

        # 안전장치: LIVE 실행 명시 확인
        if not _env_bool('AUTO_TRANSFER_LIVE_CONFIRM', False):
            raise NotImplementedError(
                'LIVE path requires AUTO_TRANSFER_LIVE_CONFIRM=true env var + '
                'manual E2E verification. This ensures no accidental real-money runs. '
                'Review the implementation, verify withdraw recipient info, then flip it.'
            )

        # 실제로는 withdraw_service / hedge_service 존재 여부 먼저 확인
        if self.withdraw_service is None or self.hedge_service is None:
            raise NotImplementedError('withdraw_service or hedge_service unavailable')

        qty = float(context.get('qty') or 0.0)
        if qty <= 0:
            raise ValueError(f'invalid qty {qty}')

        # --- Step 1: Bithumb market buy ---
        self.jobs.update(job_id, {'state': STATE_PENDING})
        self.jobs.append_event(job_id, {'type': 'step', 'detail': 'bithumb_buy_start'})

        symbol_spot = f'{ticker}/KRW'
        ref_price_krw = float(context.get('bithumb_ask_krw') or 0.0)
        spot_leg = await self.hedge_service._submit_market_order(
            exchange_instance=None,
            exchange_name='bithumb',
            symbol=symbol_spot,
            side='buy',
            amount=qty,
            market='spot',
            reference_price=ref_price_krw if ref_price_krw > 0 else None,
        )
        filled_qty = float(spot_leg.get('filled_qty', 0) or 0)
        if filled_qty <= 0:
            self.jobs.update(job_id, {
                'state': STATE_FAILED,
                'bithumb_buy_leg': spot_leg,
                'error_code': 'BITHUMB_BUY_UNFILLED',
            })
            await self._notify(f'❌ {ticker} bithumb buy unfilled: {spot_leg.get("error")}')
            return
        self.jobs.update(job_id, {
            'state': STATE_BITHUMB_BOUGHT,
            'bithumb_buy_leg': spot_leg,
            'filled_qty': filled_qty,
            'bithumb_avg_price_krw': spot_leg.get('avg_price'),
        })
        self.jobs.append_event(job_id, {
            'type': 'step',
            'detail': 'bithumb_buy_filled',
            'filled_qty': filled_qty,
        })

        # --- Step 2: Withdraw to target exchange ---
        # withdraw_service 는 preview → execute 2단계.
        preview = await self.withdraw_service.preview(
            ticker=ticker,
            target_exchange=self.cfg.target_exchange,
            withdraw_network=network,
            deposit_network=network,
        )
        if not preview.get('ok'):
            self.jobs.update(job_id, {
                'state': STATE_STUCK,
                'withdraw_preview_error': preview,
                'error_code': preview.get('code', 'WITHDRAW_PREVIEW_FAILED'),
            })
            await self._notify(
                f'⚠️ {ticker} BITHUMB_BOUGHT but withdraw preview failed: '
                f'{preview.get("code")} {preview.get("message")}'
            )
            return

        exec_result = await self.withdraw_service.execute(
            preview_token=preview['preview_token'],
        )
        if not exec_result.get('ok'):
            self.jobs.update(job_id, {
                'state': STATE_STUCK,
                'withdraw_execute_error': exec_result,
                'error_code': exec_result.get('code', 'WITHDRAW_EXECUTE_FAILED'),
            })
            await self._notify(
                f'⚠️ {ticker} BITHUMB_BOUGHT but withdraw execute failed: '
                f'{exec_result.get("code")}'
            )
            return

        withdraw_job = exec_result.get('job') or {}
        self.jobs.update(job_id, {
            'state': STATE_WITHDRAWING,
            'withdraw_started_at': int(time.time()),
            'withdraw_job': withdraw_job,
            'withdraw_amount': preview.get('withdraw_amount'),
        })
        self.jobs.append_event(job_id, {
            'type': 'step',
            'detail': 'withdraw_submitted',
            'tx_id': withdraw_job.get('tx_id'),
        })

        await self._notify(
            f'✈️ {ticker} withdraw submitted\n'
            f'  amount={preview.get("withdraw_amount")} network={network}\n'
            f'  job={withdraw_job.get("job_id", "?")}'
        )

        # 이후 단계 (DEPOSITED 감지 → Binance 매도) 는 _monitor_active_jobs 에서 처리.

    async def _monitor_active_jobs(self) -> None:
        """WITHDRAWING / DEPOSITED 상태 job 들을 주기적으로 감시.

        - WITHDRAWING 이 withdraw_timeout_min 초과 → STUCK
        - 입금 확인은 타겟 거래소 balance 조회가 필요하므로 현재 STUB — 알림만.
        - Binance market sell 로직 역시 입금 확인 후 동작하므로 STUB.
        """
        now = int(time.time())
        timeout_sec = self.cfg.withdraw_timeout_min * 60

        for job in self.jobs.active_jobs():
            state = str(job.get('state'))
            job_id = job.get('job_id')
            ticker = job.get('ticker', '?')

            if state == STATE_WITHDRAWING:
                started = int(job.get('withdraw_started_at') or 0)
                if started and (now - started) > timeout_sec:
                    self.jobs.update(job_id, {
                        'state': STATE_STUCK,
                        'error_code': 'WITHDRAW_TIMEOUT',
                        'error_message': f'withdraw exceeded {self.cfg.withdraw_timeout_min}min',
                    })
                    await self._notify(
                        f'⚠️ {ticker} withdraw timeout ({self.cfg.withdraw_timeout_min}min) — '
                        f'manual check required. job={job_id}'
                    )
                    continue
                # TODO: 타겟 거래소 balance 폴링하여 DEPOSITED 전이.
                # 현재는 수동 확인 후 /api/auto/transfer-abort 또는 UI에서 전이.

            elif state == STATE_DEPOSITED:
                # TODO: Binance market sell (hedge_service._submit_market_order) 호출.
                # 현재는 stub — 안전을 위해 LIVE 경로 미구현.
                logger.debug(
                    '[auto_transfer] %s DEPOSITED state requires manual sell (stub)', ticker,
                )

    # ------------------------------------------------------------------
    # Manual control API
    # ------------------------------------------------------------------

    async def trigger_manual(
        self,
        ticker: str,
        dry_run: Optional[bool] = None,
    ) -> dict[str, Any]:
        ticker = str(ticker or '').strip().upper()
        if not ticker:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker required'}
        if not self._deps_ok:
            return {'ok': False, 'code': 'DEPS_UNAVAILABLE', 'message': 'service inert'}

        original_dry = self.cfg.dry_run
        if dry_run is not None:
            self.cfg.dry_run = bool(dry_run)

        try:
            state = self.poller.state if self.poller else {}
            gap_result = state.get(ticker)
            if not gap_result:
                return {'ok': False, 'code': 'NO_DATA', 'message': f'{ticker} not in poller.state'}

            ex_data = gap_result.exchanges.get(self.cfg.target_exchange)
            gap = None
            if ex_data:
                gap = getattr(ex_data, 'spot_gap', None) or getattr(ex_data, 'futures_gap', None)
            if gap is None:
                return {'ok': False, 'code': 'NO_GAP', 'message': f'{ticker} gap unavailable'}

            ok, reason = self.safety.can_enter(ticker, self.cfg.nominal_usd)
            if not ok:
                return {'ok': False, 'code': 'SAFETY_BLOCKED', 'message': reason}

            viable, ctx = self._assess_opportunity(ticker, gap_result)
            if not viable:
                return {
                    'ok': False,
                    'code': 'NOT_VIABLE',
                    'message': ctx.get('reason', 'unknown'),
                    'context': ctx,
                }
            await self._start_job(ticker=ticker, gap=float(gap), source='manual', context=ctx)
            return {'ok': True, 'ticker': ticker, 'gap': float(gap), 'context': ctx}
        finally:
            self.cfg.dry_run = original_dry

    def abort_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if not job:
            return {'ok': False, 'code': 'JOB_NOT_FOUND', 'message': 'job not found'}
        if str(job.get('state')) in {STATE_COMPLETE, STATE_FAILED, STATE_ABORTED}:
            return {'ok': False, 'code': 'TERMINAL', 'message': f'job already {job.get("state")}'}
        updated = self.jobs.update(job_id, {
            'state': STATE_ABORTED,
            'error_code': 'MANUAL_ABORT',
            'error_message': 'aborted via API',
        })
        self.jobs.append_event(job_id, {'type': 'manual_abort'})
        return {'ok': True, 'job': updated}

    # ------------------------------------------------------------------
    # Notifier / status
    # ------------------------------------------------------------------

    async def _notify(self, text: str, alert_key: str | None = None) -> None:
        try:
            if self.telegram is not None:
                await self.telegram._send_message(text, alert_key=alert_key)
        except Exception as exc:
            logger.debug('[auto_transfer] telegram send failed: %s', exc)

    def status(self) -> dict[str, Any]:
        return {
            'running': self._running,
            'deps_ok': self._deps_ok,
            'config': {
                'enabled': self.cfg.enabled,
                'dry_run': self.cfg.dry_run,
                'watchlist': self.cfg.watchlist,
                'target_exchange': self.cfg.target_exchange,
                'gap_threshold': self.cfg.gap_threshold,
                'nominal_usd': self.cfg.nominal_usd,
                'min_net_usd': self.cfg.min_net_usd,
                'daily_cap_usd': self.cfg.daily_cap_usd,
                'per_ticker_cooldown_min': self.cfg.per_ticker_cooldown_min,
                'poll_interval_sec': self.cfg.poll_interval_sec,
                'withdraw_timeout_min': self.cfg.withdraw_timeout_min,
                'ticker_network_map': self.cfg.ticker_network_map,
            },
            'safety': self.safety.status(),
            'stats': {
                'total_triggers': self._total_triggers,
                'total_executes': self._total_executes,
                'total_completed': self._total_completed,
            },
            'recent_decisions': self.safety.state.last_decisions[-10:],
            'active_jobs': self.jobs.active_jobs()[-10:],
            'recent_jobs': self.jobs.list_jobs(limit=10),
        }

    def set_dry_run(self, dry_run: bool) -> None:
        self.cfg.dry_run = bool(dry_run)
        logger.info('[auto_transfer] dry_run=%s', self.cfg.dry_run)

    def set_enabled(self, enabled: bool) -> None:
        self.cfg.enabled = bool(enabled)
        logger.info('[auto_transfer] enabled=%s', self.cfg.enabled)
