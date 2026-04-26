"""Cross-chain Bridge Client — Phase 4: 현현(chain-to-chain) 브릿지 자동화.

사용 사례 (2026-04-21 OPG 플레이북):
    Bybit 에서 OPG 100%+ 펌프가 나왔는데 Base 쪽 DEX 가격은 아직 덜 움직임 →
    BSC (싼 체인) PancakeSwap 에서 매수 → Base 로 브릿지 → Base DEX / CEX 에서 매도.

이 모듈은 브릿지 실행 + 상태추적 + API 를 담당한다. 실제 DEX 매수/매도는
`dex_trader` (Phase 3) 에, 펌프/지연 감지는 `leverage_gap_scanner` (Phase 7) 에
의존한다.

실행 경로는 세 겹의 잠금으로만 열린다:
    BRIDGE_ENABLED=true
    AND BRIDGE_DRY_RUN=false
    AND BRIDGE_LIVE_CONFIRM=true
그 외에는 모두 dry-run 으로 기록된다 (트랜잭션 없음).

브릿지 프로바이더:
    - stargate_v2 (LayerZero V2) — USDC/USDT/ETH, 30초~2분
    - across_v3                   — USDC/ETH/WBTC, 1~5분
실제 tx 호출은 Phase 4.1 에서 SDK/ABI 로 완성. 현재는 LIVE 분기에서
`NotImplementedError('stargate bridge — use manual route until Phase 4.1')`
을 raise 하고, dry-run 시뮬레이션만 완전히 동작한다.
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
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# ENV helpers — 다른 서비스와 동일한 스타일
# ----------------------------------------------------------------------


def _env(key: str, default: str = '') -> str:
    return os.getenv(key, default).strip()


def _env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, '').strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'y', 'on')


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


def _env_list(key: str, default: list[str]) -> list[str]:
    raw = os.getenv(key, '').strip()
    if not raw:
        return [t.strip().upper() for t in default if t.strip()]
    return [t.strip().upper() for t in raw.split(',') if t.strip()]


# ----------------------------------------------------------------------
# 체인 / 브릿지 상수 — 실제 사용 시 업데이트 필요 (주기적 검증 권장)
# ----------------------------------------------------------------------


# LayerZero V2 EID (Stargate 가 사용)
# Ref: https://docs.layerzero.network/v2/deployments/deployed-contracts
_LZ_EID: dict[str, int] = {
    'ethereum': 30101,
    'bsc': 30102,
    'avalanche': 30106,
    'polygon': 30109,
    'arbitrum': 30110,
    'optimism': 30111,
    'base': 30184,
}


# Stargate V2 Pool (대표 토큰 → 체인별 pool contract).
# NOTE: 실제 배포 시 공식 문서로 재확인. 여기 주소는 플레이스홀더가 아니라
# 2025~2026 Stargate V2 USDC pool 기준으로 채워져 있지만, 체인별 deployments 는
# 언제든 변경될 수 있으므로 LIVE 이전에 반드시 https://stargate.finance/v2 로 검증.
_STARGATE_V2_POOLS: dict[str, dict[str, str]] = {
    'USDC': {
        # StargatePoolUSDC (V2)
        'ethereum': '<EVM_ADDRESS>',
        'base': '<EVM_ADDRESS>',
        'arbitrum': '<EVM_ADDRESS>',
        'optimism': '<EVM_ADDRESS>',
        'polygon': '<EVM_ADDRESS>',
    },
    'USDT': {
        'ethereum': '<EVM_ADDRESS>',
        'arbitrum': '<EVM_ADDRESS>',
        'optimism': '<EVM_ADDRESS>',
        'polygon': '<EVM_ADDRESS>',
    },
    'ETH': {
        'ethereum': '<EVM_ADDRESS>',
        'base': '<EVM_ADDRESS>',
        'arbitrum': '<EVM_ADDRESS>',
        'optimism': '<EVM_ADDRESS>',
    },
}


# Across V3 SpokePool per chain (2024~2026 배포 기준).
# Ref: https://docs.across.to/reference/contract-addresses
_ACROSS_SPOKE_POOL: dict[str, str] = {
    'ethereum': '<EVM_ADDRESS>',
    'base': '<EVM_ADDRESS>',
    'arbitrum': '<EVM_ADDRESS>',
    'optimism': '<EVM_ADDRESS>',
    'polygon': '<EVM_ADDRESS>',
}


# 토큰 주소 (체인별). 18 디시멀 아닌 것은 decimals 별도 관리.
_TOKEN_ADDR: dict[str, dict[str, str]] = {
    'USDC': {
        'ethereum': '<EVM_ADDRESS>',
        'base': '<EVM_ADDRESS>',
        'arbitrum': '<EVM_ADDRESS>',
        'optimism': '<EVM_ADDRESS>',
        'polygon': '<EVM_ADDRESS>',
        'bsc': '<EVM_ADDRESS>',
    },
    'USDT': {
        'ethereum': '<EVM_ADDRESS>',
        'arbitrum': '<EVM_ADDRESS>',
        'optimism': '<EVM_ADDRESS>',
        'polygon': '<EVM_ADDRESS>',
        'bsc': '<EVM_ADDRESS>',
    },
    'WETH': {
        'ethereum': '<EVM_ADDRESS>',
        'base': '<EVM_ADDRESS>',
        'arbitrum': '<EVM_ADDRESS>',
        'optimism': '<EVM_ADDRESS>',
        'polygon': '<EVM_ADDRESS>',
    },
    'WBTC': {
        'ethereum': '<EVM_ADDRESS>',
        'arbitrum': '<EVM_ADDRESS>',
        'polygon': '<EVM_ADDRESS>',
    },
}

_TOKEN_DECIMALS: dict[str, int] = {
    'USDC': 6, 'USDT': 6, 'ETH': 18, 'WETH': 18, 'WBTC': 8,
}


# 브릿지별 토큰-체인 지원 매트릭스 (사전 검증용). 없는 조합은 reject.
_STARGATE_V2_SUPPORT: dict[str, set[str]] = {
    'USDC': {'ethereum', 'base', 'arbitrum', 'optimism', 'polygon'},
    'USDT': {'ethereum', 'arbitrum', 'optimism', 'polygon'},
    'ETH':  {'ethereum', 'base', 'arbitrum', 'optimism'},
}

_ACROSS_V3_SUPPORT: dict[str, set[str]] = {
    'USDC': {'ethereum', 'base', 'arbitrum', 'optimism', 'polygon'},
    'ETH':  {'ethereum', 'base', 'arbitrum', 'optimism'},
    'WETH': {'ethereum', 'base', 'arbitrum', 'optimism', 'polygon'},
    'WBTC': {'ethereum', 'arbitrum', 'polygon'},
}


# 대략 브릿지 ETA (s). 체결 안내용 — 실제 도착은 `check_deposit_arrived` 로 폴링.
_BRIDGE_ETA_SEC: dict[str, int] = {
    'stargate_v2': 45,
    'across_v3': 120,
}


# ----------------------------------------------------------------------
# 설정
# ----------------------------------------------------------------------


@dataclass
class BridgeConfig:
    enabled: bool = True
    dry_run: bool = True
    live_confirm: bool = False
    provider: str = 'stargate_v2'           # stargate_v2 / across_v3
    max_amount_usd: float = 500.0
    daily_cap_usd: float = 2000.0
    allowed_tokens: list[str] = field(default_factory=lambda: ['USDC', 'USDT', 'ETH', 'WETH'])
    kill_switch_file: str = 'data/KILL_BRIDGE'
    jobs_path: str = 'data/bridge_jobs.jsonl'
    slippage_pct: float = 0.3               # 0.3% 기본
    arrival_poll_sec: int = 15
    arrival_timeout_sec: int = 600          # 10 분
    auto_trigger_pump_pct: float = 50.0     # leverage_gap_scanner 자동 트리거 임계치

    @classmethod
    def load(cls) -> 'BridgeConfig':
        return cls(
            enabled=_env_bool('BRIDGE_ENABLED', True),
            dry_run=_env_bool('BRIDGE_DRY_RUN', True),
            live_confirm=_env_bool('BRIDGE_LIVE_CONFIRM', False),
            provider=_env('BRIDGE_PROVIDER', 'stargate_v2').lower(),
            max_amount_usd=max(_env_float('BRIDGE_MAX_AMOUNT_USD', 500.0), 0.0),
            daily_cap_usd=max(_env_float('BRIDGE_DAILY_CAP_USD', 2000.0), 0.0),
            allowed_tokens=_env_list('BRIDGE_ALLOWED_TOKENS', ['USDC', 'USDT', 'ETH', 'WETH']),
            kill_switch_file=_env('BRIDGE_KILL_SWITCH_FILE', 'data/KILL_BRIDGE'),
            jobs_path=_env('BRIDGE_JOBS_PATH', 'data/bridge_jobs.jsonl'),
            slippage_pct=_env_float('BRIDGE_SLIPPAGE_PCT', 0.3),
            arrival_poll_sec=max(_env_int('BRIDGE_ARRIVAL_POLL_SEC', 15), 3),
            arrival_timeout_sec=max(_env_int('BRIDGE_ARRIVAL_TIMEOUT_SEC', 600), 30),
            auto_trigger_pump_pct=_env_float('BRIDGE_AUTO_TRIGGER_PUMP_PCT', 50.0),
        )


@dataclass
class _BridgeState:
    daily_spent_usd: float = 0.0
    daily_reset_epoch: float = 0.0
    total_detected_gaps: int = 0
    total_requested: int = 0
    total_sent: int = 0
    total_received: int = 0
    total_dry_run: int = 0
    total_failed: int = 0
    last_error: str = ''
    # job_id -> dict
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


# ----------------------------------------------------------------------
# Bridge Client 본체
# ----------------------------------------------------------------------


class BridgeClient:
    """Cross-chain bridge automation.

    Methods:
        async bridge(from_chain, to_chain, token, amount_tokens, amount_usd=None) -> dict
            브릿지 job 생성 + 실행(드라이런 또는 LIVE). job_id 반환.
        async get_bridge_quote(from_chain, to_chain, token, amount) -> dict
            수수료/ETA 사전 확인 (LIVE 가 아니어도 호출 가능).
        async check_deposit_arrived(to_chain, token, amount, source_tx_hash, timeout_sec=600) -> bool
            브릿지 도착 폴링.

    Events:
        on_leverage_gap(event_dict) — LeverageGapScanner 가 호출하는 콜백. 조건 충족 시
        Telegram 알림만 울리고, 실제 실행은 Phase 4.1 에서 이어 구현.
    """

    def __init__(
        self,
        dex_trader: Any = None,
        leverage_gap_scanner: Any = None,
        telegram_service: Any = None,
        cfg: Optional[BridgeConfig] = None,
    ) -> None:
        self.dex_trader = dex_trader
        self.lgap_scanner = leverage_gap_scanner
        self.telegram = telegram_service
        self.cfg = cfg or BridgeConfig.load()
        self.state = _BridgeState(daily_reset_epoch=_today_midnight_epoch())

        self._running: bool = False
        self._write_lock = asyncio.Lock()
        # job_id -> asyncio.Task (도착 감시)
        self._arrival_tasks: dict[str, asyncio.Task] = {}

        # web3 인스턴스는 LIVE 호출 시 lazy init (start 에서는 의존성 없음)
        self._w3_cache: dict[str, Any] = {}
        self._w3_account: Any = None

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            Path(self.cfg.jobs_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[bridge] jobs_path prep failed: %s', exc)

        # leverage_gap_scanner 에 콜백 등록
        if self.lgap_scanner is not None:
            # 기존 콜백이 있을 수 있으므로 보존하면서 체이닝
            existing_cb = getattr(self.lgap_scanner, 'on_leverage_gap', None)
            if existing_cb is None:
                self.lgap_scanner.on_leverage_gap = self._on_leverage_gap
            else:
                # 체이닝: 기존 cb 호출 후 이쪽도 호출
                def _chained(ev: dict[str, Any], _prev=existing_cb, _self=self) -> None:
                    try:
                        _prev(ev)
                    except Exception:  # noqa: BLE001
                        pass
                    _self._on_leverage_gap(ev)
                self.lgap_scanner.on_leverage_gap = _chained

        logger.info(
            '[bridge] started (enabled=%s dry_run=%s live_confirm=%s provider=%s '
            'max=$%.0f daily_cap=$%.0f allowed=%s)',
            self.cfg.enabled, self.cfg.dry_run, self.cfg.live_confirm,
            self.cfg.provider, self.cfg.max_amount_usd, self.cfg.daily_cap_usd,
            ','.join(self.cfg.allowed_tokens),
        )

    async def stop(self) -> None:
        self._running = False
        # 도착 감시 태스크 정리
        tasks = list(self._arrival_tasks.values())
        for t in tasks:
            if not t.done():
                t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._arrival_tasks.clear()
        logger.info('[bridge] stopped')

    # ------------------------------------------------------------------
    # 상태
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_rollover_daily()
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and not self._kill_switch_active()
        )
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'live_armed': live_armed,
            'provider': self.cfg.provider,
            'kill_switch_active': self._kill_switch_active(),
            'kill_switch_file': self.cfg.kill_switch_file,
            'allowed_tokens': list(self.cfg.allowed_tokens),
            'max_amount_usd': self.cfg.max_amount_usd,
            'daily_cap_usd': self.cfg.daily_cap_usd,
            'daily_spent_usd': round(self.state.daily_spent_usd, 2),
            'slippage_pct': self.cfg.slippage_pct,
            'arrival_timeout_sec': self.cfg.arrival_timeout_sec,
            'total_detected_gaps': self.state.total_detected_gaps,
            'total_requested': self.state.total_requested,
            'total_sent': self.state.total_sent,
            'total_received': self.state.total_received,
            'total_dry_run': self.state.total_dry_run,
            'total_failed': self.state.total_failed,
            'last_error': self.state.last_error,
            'open_jobs': {
                jid: {
                    k: v for k, v in job.items()
                    if k in ('from_chain', 'to_chain', 'token', 'amount', 'status', 'ts')
                }
                for jid, job in self.state.jobs.items()
                if job.get('status') not in {'complete', 'failed'}
            },
            'jobs_path': self.cfg.jobs_path,
        }

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        return self.state.jobs.get(job_id)

    def recent_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        path = Path(self.cfg.jobs_path)
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('[bridge] recent_jobs read err: %s', exc)
            return []
        out: list[dict[str, Any]] = []
        for raw in reversed(lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except Exception:  # noqa: BLE001
                continue
            if len(out) >= limit:
                break
        return out

    # ------------------------------------------------------------------
    # 레갭 이벤트 구독 (Phase 7 콜백)
    # ------------------------------------------------------------------

    def _on_leverage_gap(self, event: dict[str, Any]) -> None:
        """leverage_gap_scanner 로부터 펌프 이벤트 수신.

        Phase 4 는 "알림 + 로깅" 만 한다. 실제 자동 매수→브릿지→매도는 Phase 4.1 에서.
        """
        self.state.total_detected_gaps += 1
        try:
            ticker = str(event.get('ticker') or '').upper()
            pump_exchange = str(event.get('exchange') or '').lower()
            pump_pct = float(event.get('pump_pct') or 0.0)
            laggards = event.get('laggard_exchanges') or []

            if not ticker or pump_pct < self.cfg.auto_trigger_pump_pct:
                return
            if not laggards:
                # 모든 거래소가 같이 움직인 경우 = 체인간 갭 기대 낮음
                logger.info(
                    '[bridge] skip auto-trigger %s: pump=%.2f%% but no laggards',
                    ticker, pump_pct,
                )
                return

            lag_preview = ', '.join(
                f"{(l.get('exchange') or '?')}+{float(l.get('pump_pct') or 0):.1f}%"
                for l in laggards[:3]
            )
            text = (
                f'🌉 브릿지 기회: {ticker} {pump_exchange} +{pump_pct:.2f}% '
                f'vs {lag_preview} — buy laggard side, bridge to pump side '
                f'(Phase 4.1 auto-execute pending)'
            )
            logger.warning(text)
            # Telegram 알림 (fire-and-forget)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._notify(text))
            except RuntimeError:
                pass

            # Phase 4.1 에서 dex_trader + self.bridge() 연동 예정. 현재는 기록만.
        except Exception as exc:  # noqa: BLE001
            logger.warning('[bridge] on_leverage_gap err: %s', exc)

    # ------------------------------------------------------------------
    # 견적
    # ------------------------------------------------------------------

    async def get_bridge_quote(
        self,
        from_chain: str,
        to_chain: str,
        token: str,
        amount: float,
    ) -> dict[str, Any]:
        """사전 견적 — route 유효성 + 대략 수수료/ETA. 실제 온체인 호출은 하지 않음.

        반환:
            {ok, provider, from, to, token, amount, est_fee_token, est_eta_sec,
             route_supported, reasons: []}
        """
        provider = self.cfg.provider
        from_chain = (from_chain or '').lower()
        to_chain = (to_chain or '').lower()
        token_up = (token or '').upper()
        reasons: list[str] = []

        if not self._is_token_allowed(token_up):
            reasons.append(f'token {token_up} not in whitelist')
        if from_chain == to_chain:
            reasons.append('from/to same chain')
        if amount is None or amount <= 0:
            reasons.append('amount must be > 0')

        route_ok = self._route_supported(provider, token_up, from_chain, to_chain)
        if not route_ok:
            reasons.append(f'{provider} does not support {token_up} {from_chain}->{to_chain}')

        # 대략 수수료 추정: LZ/Stargate 는 고정가스 + pool fee 0.01~0.06%, Across 는 LP fee 0.04~0.15%
        est_fee_bps = 6 if provider == 'stargate_v2' else 15
        est_fee_token = (amount or 0.0) * est_fee_bps / 10_000.0
        est_eta = _BRIDGE_ETA_SEC.get(provider, 180)

        return {
            'ok': not reasons,
            'provider': provider,
            'from': from_chain,
            'to': to_chain,
            'token': token_up,
            'amount': amount,
            'est_fee_token': round(est_fee_token, 6),
            'est_fee_bps': est_fee_bps,
            'est_eta_sec': est_eta,
            'route_supported': route_ok,
            'reasons': reasons,
        }

    # ------------------------------------------------------------------
    # 브릿지 실행
    # ------------------------------------------------------------------

    async def bridge(
        self,
        from_chain: str,
        to_chain: str,
        token: str,
        amount_tokens: float,
        amount_usd: Optional[float] = None,
        origin: str = 'manual',
    ) -> dict[str, Any]:
        """브릿지 job 을 생성하고 실행한다.

        Args:
            from_chain / to_chain: 'ethereum', 'base', 'bsc', 'arbitrum', ...
            token: 'USDC' | 'USDT' | 'ETH' | 'WETH' | 'WBTC'
            amount_tokens: native 토큰 단위 (USDC 면 달러, ETH 면 ether)
            amount_usd: 일일 cap 계산용 (미지정이면 stablecoin 은 amount_tokens, 그 외는
                        상위 모듈이 계산해서 넘겨야 함. 안 주면 conservative 추정)
            origin: 'manual' | 'lgap_auto' | 'dex_trader' 등 호출 경로 라벨

        Returns:
            {ok, code, job_id, status, tx_hash?, eta_sec?, message?}
        """
        self.state.total_requested += 1
        job_id = f'br_{int(time.time())}_{uuid.uuid4().hex[:8]}'
        now_ts = int(time.time())
        from_chain = (from_chain or '').lower()
        to_chain = (to_chain or '').lower()
        token_up = (token or '').upper()
        provider = self.cfg.provider

        # 효과적 USD 산출 (일일 cap 체크용)
        eff_usd = self._estimate_usd(token_up, amount_tokens, amount_usd)

        # ---- 게이트 0: enabled
        if not self.cfg.enabled:
            return await self._fail_job(
                job_id, 'DISABLED', 'bridge disabled', from_chain, to_chain,
                token_up, amount_tokens, eff_usd, now_ts, origin,
            )

        # ---- 게이트 1: kill switch
        if self._kill_switch_active():
            return await self._fail_job(
                job_id, 'KILL_SWITCH',
                f'kill switch present: {self.cfg.kill_switch_file}',
                from_chain, to_chain, token_up, amount_tokens, eff_usd, now_ts, origin,
            )

        # ---- 게이트 2: 입력 검증
        if from_chain == to_chain or not from_chain or not to_chain:
            return await self._fail_job(
                job_id, 'INVALID_ROUTE', 'from/to same or empty',
                from_chain, to_chain, token_up, amount_tokens, eff_usd, now_ts, origin,
            )
        if amount_tokens is None or amount_tokens <= 0:
            return await self._fail_job(
                job_id, 'INVALID_AMOUNT', 'amount must be > 0',
                from_chain, to_chain, token_up, amount_tokens, eff_usd, now_ts, origin,
            )

        # ---- 게이트 3: 토큰 화이트리스트
        if not self._is_token_allowed(token_up):
            return await self._fail_job(
                job_id, 'TOKEN_NOT_ALLOWED',
                f'{token_up} not in whitelist {self.cfg.allowed_tokens}',
                from_chain, to_chain, token_up, amount_tokens, eff_usd, now_ts, origin,
            )

        # ---- 게이트 4: provider route 지원
        if not self._route_supported(provider, token_up, from_chain, to_chain):
            return await self._fail_job(
                job_id, 'ROUTE_UNSUPPORTED',
                f'{provider} does not support {token_up} {from_chain}->{to_chain}',
                from_chain, to_chain, token_up, amount_tokens, eff_usd, now_ts, origin,
            )

        # ---- 게이트 5: 단건 / 일일 캡
        if eff_usd > self.cfg.max_amount_usd:
            return await self._fail_job(
                job_id, 'MAX_AMOUNT_EXCEEDED',
                f'single bridge ${eff_usd:.2f} > cap ${self.cfg.max_amount_usd:.2f}',
                from_chain, to_chain, token_up, amount_tokens, eff_usd, now_ts, origin,
            )
        self._maybe_rollover_daily()
        if self.state.daily_spent_usd + eff_usd > self.cfg.daily_cap_usd:
            return await self._fail_job(
                job_id, 'DAILY_CAP_EXCEEDED',
                (f'daily ${self.state.daily_spent_usd:.2f} + ${eff_usd:.2f} > '
                 f'cap ${self.cfg.daily_cap_usd:.2f}'),
                from_chain, to_chain, token_up, amount_tokens, eff_usd, now_ts, origin,
            )

        # ---- job 등록
        job: dict[str, Any] = {
            'job_id': job_id,
            'ts': now_ts,
            'origin': origin,
            'provider': provider,
            'from_chain': from_chain,
            'to_chain': to_chain,
            'token': token_up,
            'amount': float(amount_tokens),
            'amount_usd': round(eff_usd, 2),
            'status': 'requested',
            'from_tx': None,
            'to_tx': None,
            'eta_sec': _BRIDGE_ETA_SEC.get(provider, 180),
            'error': None,
        }
        self.state.jobs[job_id] = job
        await self._append_jobs_record({**job, 'event': 'requested'})

        # ---- 트리플 락: LIVE vs DRY-RUN
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
        )

        if not live_armed:
            return await self._execute_dry_run(job)

        return await self._execute_live(job)

    # ------------------------------------------------------------------
    # 실행 — dry-run
    # ------------------------------------------------------------------

    async def _execute_dry_run(self, job: dict[str, Any]) -> dict[str, Any]:
        self.state.total_dry_run += 1
        job['status'] = 'from_sent'
        job['from_tx'] = f'0xDRY{job["job_id"][-32:]}'
        logger.info(
            '[DRY-BRIDGE] %s %s→%s %.6g %s ~ETA %ds',
            job['job_id'], job['from_chain'], job['to_chain'],
            job['amount'], job['token'], job['eta_sec'],
        )
        await self._append_jobs_record({**job, 'event': 'from_sent_dry'})
        # 시뮬레이션: ETA 후에 status complete 로 전이
        if self._running:
            self._arrival_tasks[job['job_id']] = asyncio.create_task(
                self._simulate_arrival(job['job_id'], job['eta_sec']),
                name=f'bridge_simarr_{job["job_id"]}',
            )
        await self._notify(
            f'🧪 [DRY] 브릿지 요청: {job["token"]} {job["amount"]} '
            f'{job["from_chain"]}→{job["to_chain"]} (ETA {job["eta_sec"]}s)',
            alert_key='dry_bridge',
        )
        return {
            'ok': True,
            'code': 'DRY_RUN',
            'job_id': job['job_id'],
            'status': job['status'],
            'tx_hash': job['from_tx'],
            'eta_sec': job['eta_sec'],
            'message': 'recorded as dry-run (set BRIDGE_LIVE_CONFIRM=true to LIVE)',
        }

    async def _simulate_arrival(self, job_id: str, eta_sec: int) -> None:
        try:
            await asyncio.sleep(max(1, eta_sec))
            job = self.state.jobs.get(job_id)
            if job is None or job.get('status') == 'failed':
                return
            job['status'] = 'complete'
            job['to_tx'] = f'0xDRY{job_id[-32:]}'
            await self._append_jobs_record({**job, 'event': 'complete_dry'})
            logger.info('[DRY-BRIDGE] %s complete (simulated)', job_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning('[bridge] sim arrival err: %s', exc)
        finally:
            self._arrival_tasks.pop(job_id, None)

    # ------------------------------------------------------------------
    # 실행 — LIVE (Phase 4.1 에서 실제 트랜잭션 완성)
    # ------------------------------------------------------------------

    async def _execute_live(self, job: dict[str, Any]) -> dict[str, Any]:
        """실제 체인 트랜잭션.

        Phase 4 스코프: dry-run 이 주력. LIVE 분기는 여기서 의도적으로
        NotImplementedError 로 막아둔다. 사용자는 수동 브릿지 라우트를 타도록.
        Phase 4.1 에서 SDK 연동 후 해제.
        """
        # 일일 cap 은 LIVE 에서 실제로 차감해야 하므로 선차감(실패 시 복구)
        self.state.daily_spent_usd += job['amount_usd']
        try:
            # 여기서 web3 연결 확인 (LIVE 경로에서만 의존성 필요)
            rpc_url = self._rpc_url(job['from_chain'])
            if not rpc_url:
                raise RuntimeError(f'DEX_{job["from_chain"].upper()}_RPC_URL not configured')
            pk = os.getenv('DEX_PRIVATE_KEY', '').strip()
            if not pk:
                raise RuntimeError('DEX_PRIVATE_KEY not configured')

            if job['provider'] == 'stargate_v2':
                raise NotImplementedError(
                    'stargate bridge — use manual route until Phase 4.1'
                )
            elif job['provider'] == 'across_v3':
                raise NotImplementedError(
                    'across bridge — use manual route until Phase 4.1'
                )
            else:
                raise RuntimeError(f'unknown provider {job["provider"]}')
        except Exception as exc:  # noqa: BLE001
            # cap 복구
            self.state.daily_spent_usd = max(0.0, self.state.daily_spent_usd - job['amount_usd'])
            self.state.total_failed += 1
            self.state.last_error = f'{type(exc).__name__}: {exc}'
            job['status'] = 'failed'
            job['error'] = str(exc)
            await self._append_jobs_record({**job, 'event': 'failed_live'})
            logger.error('[bridge] LIVE %s failed: %s', job['job_id'], exc)
            await self._notify(
                f'🚨 브릿지 실패: {job["job_id"]} {job["token"]} '
                f'{job["from_chain"]}→{job["to_chain"]} — {exc}'
            )
            return {
                'ok': False,
                'code': type(exc).__name__,
                'job_id': job['job_id'],
                'status': 'failed',
                'message': str(exc),
            }

    # ------------------------------------------------------------------
    # 도착 확인 — Phase 4.1 에서 실제 tx receipt / LZ scan API 폴링
    # ------------------------------------------------------------------

    async def check_deposit_arrived(
        self,
        to_chain: str,
        token: str,
        amount: float,
        source_tx_hash: str,
        timeout_sec: int = 600,
    ) -> bool:
        """브릿지 도착 폴링. Phase 4 에서는 dry-run job 의 simulated status 만 조회.

        LIVE 구현 (Phase 4.1):
            1) LayerZero Scan API (https://scan.layerzero-api.com/v1/messages/tx/<hash>) 폴링
            2) Across: https://across.to/api/deposit-status?originTxHash=...
            3) 둘 다 실패 시 destination chain 에서 recipient balance 델타 관찰
        """
        deadline = time.time() + max(timeout_sec, 30)
        # source_tx_hash 로 job 역추적
        target_job = None
        for job in self.state.jobs.values():
            if job.get('from_tx') == source_tx_hash:
                target_job = job
                break

        while time.time() < deadline:
            if target_job is not None:
                if target_job.get('status') == 'complete':
                    return True
                if target_job.get('status') == 'failed':
                    return False
            # Phase 4.1 에서 여기에 실 RPC / scan API 폴링 삽입
            await asyncio.sleep(max(self.cfg.arrival_poll_sec, 3))
        return False

    # ------------------------------------------------------------------
    # 내부 유틸
    # ------------------------------------------------------------------

    def _route_supported(
        self,
        provider: str,
        token: str,
        from_chain: str,
        to_chain: str,
    ) -> bool:
        if provider == 'stargate_v2':
            support = _STARGATE_V2_SUPPORT.get(token)
        elif provider == 'across_v3':
            support = _ACROSS_V3_SUPPORT.get(token)
        else:
            return False
        if not support:
            return False
        return from_chain in support and to_chain in support

    def _is_token_allowed(self, token: str) -> bool:
        return token.upper() in set(self.cfg.allowed_tokens)

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:  # noqa: BLE001
            return False

    def _maybe_rollover_daily(self) -> None:
        today = _today_midnight_epoch()
        if today > self.state.daily_reset_epoch:
            self.state.daily_spent_usd = 0.0
            self.state.daily_reset_epoch = today

    def _estimate_usd(self, token: str, amount: float, amount_usd: Optional[float]) -> float:
        if amount_usd is not None and amount_usd > 0:
            return float(amount_usd)
        # 스테이블은 1:1
        if token in {'USDC', 'USDT', 'DAI', 'USDE'}:
            return float(amount or 0.0)
        # 상위에서 가격 안 줬으면 conservative 로 amount * 0 처리하면 0 으로 통과될 수 있어
        # 안전하게 max_amount_usd 로 간주 → cap 이 강제로 걸리도록
        logger.warning(
            '[bridge] no amount_usd given for %s; forcing conservative cap check',
            token,
        )
        return max(self.cfg.max_amount_usd, 1.0) + 1.0

    def _rpc_url(self, chain: str) -> str:
        return os.getenv(f'DEX_{chain.upper()}_RPC_URL', '').strip()

    async def _append_jobs_record(self, record: dict[str, Any]) -> None:
        path = Path(self.cfg.jobs_path)
        async with self._write_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(record, ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.debug('[bridge] jobs append err: %s', exc)

    async def _fail_job(
        self,
        job_id: str,
        code: str,
        message: str,
        from_chain: str,
        to_chain: str,
        token: str,
        amount: float,
        amount_usd: float,
        ts: int,
        origin: str,
    ) -> dict[str, Any]:
        self.state.total_failed += 1
        self.state.last_error = f'{code}: {message}'
        record = {
            'event': 'rejected',
            'job_id': job_id,
            'ts': ts,
            'origin': origin,
            'provider': self.cfg.provider,
            'from_chain': from_chain,
            'to_chain': to_chain,
            'token': token,
            'amount': amount,
            'amount_usd': round(amount_usd, 2),
            'status': 'failed',
            'error': f'{code}: {message}',
        }
        await self._append_jobs_record(record)
        logger.info('[bridge] reject %s: %s', job_id, message)
        return {
            'ok': False,
            'code': code,
            'job_id': job_id,
            'status': 'failed',
            'message': message,
        }

    async def _notify(self, text: str, alert_key: str | None = None) -> None:
        if self.telegram is None:
            return
        try:
            send = getattr(self.telegram, '_send_message', None)
            if send is not None:
                try:
                    await send(text, alert_key=alert_key)
                    return
                except TypeError:
                    await send(text)
                    return
            send = getattr(self.telegram, 'send_message', None)
            if send is None:
                return
            await send(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[bridge] telegram err: %s', exc)

    # ------------------------------------------------------------------
    # 수동 abort (API)
    # ------------------------------------------------------------------

    def abort_job(self, job_id: str) -> dict[str, Any]:
        """상태 전이만 수행 — 실제 브릿지는 온체인 불가역이므로 취소 불가.

        dry-run 의 경우 simulate_arrival 태스크를 중단해서 complete 로 가지 않게 한다.
        """
        job = self.state.jobs.get(job_id)
        if not job:
            return {'ok': False, 'code': 'JOB_NOT_FOUND', 'message': 'job not found'}
        if job.get('status') in {'complete', 'failed'}:
            return {'ok': False, 'code': 'ALREADY_TERMINAL', 'message': job.get('status')}

        task = self._arrival_tasks.pop(job_id, None)
        if task and not task.done():
            task.cancel()

        job['status'] = 'failed'
        job['error'] = 'aborted_by_user'
        # append 는 fire-and-forget 로 (sync 컨텍스트)
        try:
            asyncio.get_running_loop().create_task(
                self._append_jobs_record({**job, 'event': 'aborted'})
            )
        except RuntimeError:
            pass
        return {'ok': True, 'job_id': job_id, 'status': 'failed'}
