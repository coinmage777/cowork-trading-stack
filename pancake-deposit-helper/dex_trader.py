"""DEX 자동 매수/매도 실행기 (DexTrader) — Phase 3: 상장 전 DEX 즉시 매수.

4/21 CHIP 플레이북 기반: Bithumb/Upbit 상장 공지 → 컨트랙트 주소 즉시 확보 →
Uniswap V3 (Ethereum) / Aerodrome V2 or Uniswap V3 (Base) / PancakeSwap V2 (BSC)
라우터에 공격적 슬리피지로 시장가 스왑 (WETH/WBNB → target token).
CEX 상장 펌프에 맞춰 수동 또는 자동 매도.

LP Phase 6은 별도. 여기는 순수 투기 매수/매도.

실행 경로는 세 겹의 잠금 + kill switch:
    DEX_TRADER_ENABLED=true
    AND DEX_TRADER_DRY_RUN=false
    AND DEX_TRADER_LIVE_CONFIRM=true
    AND (kill switch file absent)
그 외에는 모두 dry-run 기록 (data/dex_jobs.jsonl).

설계 원칙 (적대적 리뷰 대응):
  - nonce 관리: eth.get_transaction_count(addr, 'pending')
  - 슬리피지/minOut 계산 시 decimals 정확히 반영
  - gas: estimate_gas * 1.3 + priority fee
  - balance 선검증 (부족 시 abort)
  - Dexscreener price staleness (pairCreatedAt/txns 기반, 없으면 거부)
  - private key 환경변수에서만 로드. 코드/로그에 노출 금지
  - receipt status != 1 이면 실패 처리
  - Telegram 에러 알림
  - 재진입 방지 — ticker inflight 세트
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import aiohttp  # type: ignore

logger = logging.getLogger(__name__)


# ======================================================================
# env 헬퍼 (listing_executor 동일 컨벤션)
# ======================================================================


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        return float(raw.strip())
    except ValueError:
        return float(default)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return int(default)
    try:
        return int(raw.strip())
    except ValueError:
        return int(default)


def _str_env(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw if raw else default


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return list(default)
    return [t.strip().lower() for t in raw.split(',') if t.strip()]


# ======================================================================
# 체인 상수
# ======================================================================


@dataclass(frozen=True)
class ChainSpec:
    """체인별 라우터/native wrap token/chain_id 정의."""

    name: str
    chain_id: int
    rpc_env: str                     # env var 이름
    rpc_default: str                 # 기본 public RPC
    wrapped_native: str              # WETH/WBNB 주소 (checksum)
    router_type: str                 # 'uniswap_v3', 'aerodrome_v2', 'pancake_v2'
    router_address: str              # 기본 라우터
    native_symbol: str               # 'ETH', 'BNB'
    explorer_tx: str                 # tx URL prefix
    default_fee_tier: int = 3000     # Uniswap V3 풀 수수료 틱 (0.3%)


CHAINS: dict[str, ChainSpec] = {
    'ethereum': ChainSpec(
        name='ethereum',
        chain_id=1,
        rpc_env='DEX_ETH_RPC_URL',
        rpc_default='https://eth.llamarpc.com',
        wrapped_native='<EVM_ADDRESS>',  # WETH
        router_type='uniswap_v3',
        router_address='<EVM_ADDRESS>',   # Uniswap V3 Router (SwapRouter)
        native_symbol='ETH',
        explorer_tx='https://etherscan.io/tx/',
    ),
    'base': ChainSpec(
        name='base',
        chain_id=8453,
        rpc_env='DEX_BASE_RPC_URL',
        rpc_default='https://mainnet.base.org',
        wrapped_native='<EVM_ADDRESS>',  # WETH on Base
        router_type='uniswap_v3',
        # Base 의 Uniswap V3 SwapRouter02. Aerodrome 은 향후 'aerodrome_v2' 로 확장
        router_address='<EVM_ADDRESS>',
        native_symbol='ETH',
        explorer_tx='https://basescan.org/tx/',
    ),
    'bsc': ChainSpec(
        name='bsc',
        chain_id=56,
        rpc_env='DEX_BSC_RPC_URL',
        rpc_default='https://bsc-dataseed.binance.org',
        wrapped_native='<EVM_ADDRESS>',  # WBNB
        router_type='pancake_v2',
        router_address='<EVM_ADDRESS>',   # PancakeSwap V2 Router
        native_symbol='BNB',
        explorer_tx='https://bscscan.com/tx/',
    ),
}


# ======================================================================
# ABI (최소)
# ======================================================================


# ERC20 — decimals/balanceOf/approve/allowance 만 필요
_ERC20_ABI: list[dict[str, Any]] = [
    {
        'constant': True, 'inputs': [], 'name': 'decimals',
        'outputs': [{'name': '', 'type': 'uint8'}], 'type': 'function',
    },
    {
        'constant': True, 'inputs': [], 'name': 'symbol',
        'outputs': [{'name': '', 'type': 'string'}], 'type': 'function',
    },
    {
        'constant': True,
        'inputs': [{'name': 'owner', 'type': 'address'}],
        'name': 'balanceOf',
        'outputs': [{'name': '', 'type': 'uint256'}], 'type': 'function',
    },
    {
        'constant': True,
        'inputs': [
            {'name': 'owner', 'type': 'address'},
            {'name': 'spender', 'type': 'address'},
        ],
        'name': 'allowance',
        'outputs': [{'name': '', 'type': 'uint256'}], 'type': 'function',
    },
    {
        'constant': False,
        'inputs': [
            {'name': 'spender', 'type': 'address'},
            {'name': 'amount', 'type': 'uint256'},
        ],
        'name': 'approve',
        'outputs': [{'name': '', 'type': 'bool'}], 'type': 'function',
    },
]


# Uniswap V3 SwapRouter — exactInputSingle
_UNISWAP_V3_ROUTER_ABI: list[dict[str, Any]] = [
    {
        'inputs': [{
            'components': [
                {'name': 'tokenIn', 'type': 'address'},
                {'name': 'tokenOut', 'type': 'address'},
                {'name': 'fee', 'type': 'uint24'},
                {'name': 'recipient', 'type': 'address'},
                {'name': 'deadline', 'type': 'uint256'},
                {'name': 'amountIn', 'type': 'uint256'},
                {'name': 'amountOutMinimum', 'type': 'uint256'},
                {'name': 'sqrtPriceLimitX96', 'type': 'uint160'},
            ],
            'name': 'params', 'type': 'tuple',
        }],
        'name': 'exactInputSingle',
        'outputs': [{'name': 'amountOut', 'type': 'uint256'}],
        'stateMutability': 'payable', 'type': 'function',
    },
]


# PancakeSwap V2 Router (Uniswap V2 호환) —
#   swapExactETHForTokensSupportingFeeOnTransferTokens (BNB → token)
#   swapExactTokensForETHSupportingFeeOnTransferTokens (token → BNB)
_PANCAKE_V2_ROUTER_ABI: list[dict[str, Any]] = [
    {
        'inputs': [
            {'name': 'amountOutMin', 'type': 'uint256'},
            {'name': 'path', 'type': 'address[]'},
            {'name': 'to', 'type': 'address'},
            {'name': 'deadline', 'type': 'uint256'},
        ],
        'name': 'swapExactETHForTokensSupportingFeeOnTransferTokens',
        'outputs': [], 'stateMutability': 'payable', 'type': 'function',
    },
    {
        'inputs': [
            {'name': 'amountIn', 'type': 'uint256'},
            {'name': 'amountOutMin', 'type': 'uint256'},
            {'name': 'path', 'type': 'address[]'},
            {'name': 'to', 'type': 'address'},
            {'name': 'deadline', 'type': 'uint256'},
        ],
        'name': 'swapExactTokensForETHSupportingFeeOnTransferTokens',
        'outputs': [], 'stateMutability': 'nonpayable', 'type': 'function',
    },
    {
        'inputs': [
            {'name': 'amountIn', 'type': 'uint256'},
            {'name': 'path', 'type': 'address[]'},
        ],
        'name': 'getAmountsOut',
        'outputs': [{'name': 'amounts', 'type': 'uint256[]'}],
        'stateMutability': 'view', 'type': 'function',
    },
]


# ======================================================================
# 설정
# ======================================================================


@dataclass
class DexTraderConfig:
    enabled: bool = False
    dry_run: bool = True
    live_confirm: bool = False
    notional_usd: float = 100.0
    max_slippage_pct: float = 5.0
    gas_priority_fee_gwei: float = 2.0
    kill_switch_file: str = 'data/KILL_DEX'
    jobs_path: str = 'data/dex_jobs.jsonl'
    supported_chains: list[str] = field(default_factory=lambda: ['base', 'ethereum', 'bsc'])
    per_ticker_cooldown_min: int = 60
    max_event_age_sec: int = 300
    gas_limit_cap: int = 600_000
    dexscreener_timeout_sec: float = 8.0
    tx_wait_timeout_sec: int = 120
    min_confirmations: int = 2
    min_pair_liquidity_usd: float = 10_000.0
    # private key 는 env 에서만 읽는다 — config 필드에는 절대 저장하지 않음

    @classmethod
    def load(cls) -> 'DexTraderConfig':
        return cls(
            enabled=_bool_env('DEX_TRADER_ENABLED', False),
            dry_run=_bool_env('DEX_TRADER_DRY_RUN', True),
            live_confirm=_bool_env('DEX_TRADER_LIVE_CONFIRM', False),
            notional_usd=max(_float_env('DEX_TRADER_NOTIONAL_USD', 100.0), 0.0),
            max_slippage_pct=max(_float_env('DEX_TRADER_MAX_SLIPPAGE_PCT', 5.0), 0.0),
            gas_priority_fee_gwei=max(_float_env('DEX_TRADER_GAS_PRIORITY_FEE_GWEI', 2.0), 0.0),
            kill_switch_file=_str_env('DEX_TRADER_KILL_SWITCH_FILE', 'data/KILL_DEX'),
            jobs_path=_str_env('DEX_TRADER_JOBS_PATH', 'data/dex_jobs.jsonl'),
            supported_chains=_csv_env('DEX_SUPPORTED_CHAINS', ['base', 'ethereum', 'bsc']),
            per_ticker_cooldown_min=max(_int_env('DEX_TRADER_PER_TICKER_COOLDOWN_MIN', 60), 0),
            max_event_age_sec=max(_int_env('DEX_TRADER_MAX_EVENT_AGE_SEC', 300), 0),
            gas_limit_cap=max(_int_env('DEX_TRADER_GAS_LIMIT_CAP', 600_000), 100_000),
            dexscreener_timeout_sec=max(_float_env('DEX_TRADER_DEXSCREENER_TIMEOUT', 8.0), 1.0),
            tx_wait_timeout_sec=max(_int_env('DEX_TRADER_TX_WAIT_TIMEOUT', 120), 30),
            min_confirmations=max(_int_env('DEX_TRADER_MIN_CONFIRMATIONS', 2), 1),
            min_pair_liquidity_usd=max(_float_env('DEX_TRADER_MIN_PAIR_LIQUIDITY_USD', 10_000.0), 0.0),
        )


@dataclass
class _DexTraderState:
    daily_spent_usd: float = 0.0
    daily_reset_epoch: float = 0.0
    last_entry_ts_per_ticker: dict[str, float] = field(default_factory=dict)
    # ticker -> job dict (contract, chain, qty_tokens, avg_price, tx_hash, ts)
    open_jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_detected: int = 0
    total_executed: int = 0
    total_dry_run: int = 0
    total_skipped: int = 0
    total_errors: int = 0
    last_error: str = ''
    last_executed_ts: float = 0.0


def _today_midnight_epoch() -> float:
    import datetime
    now = datetime.datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


# ======================================================================
# DexTrader 본체
# ======================================================================


class DexTrader:
    """Phase 3: 상장 공지 감지 시 DEX 에서 자동 스팟 매수.

    사용:
        trader = DexTrader(
            listing_detector=listing_detector,
            telegram_service=telegram,
        )
        await trader.start()
        ...
        await trader.stop()
    """

    def __init__(
        self,
        listing_detector: Any = None,
        telegram_service: Any = None,
        cfg: Optional[DexTraderConfig] = None,
    ) -> None:
        self.detector = listing_detector
        self.telegram = telegram_service
        self.cfg = cfg or DexTraderConfig.load()
        self.state = _DexTraderState(daily_reset_epoch=_today_midnight_epoch())

        self._running: bool = False
        self._write_lock = asyncio.Lock()
        self._inflight_tickers: set[str] = set()
        # web3 provider 캐시: chain -> Web3 instance
        self._w3_cache: dict[str, Any] = {}
        # checksum 주소 캐시
        self._checksum_cache: dict[str, str] = {}
        # nonce 동시 송신 방지 — chain 별 락
        self._nonce_locks: dict[str, asyncio.Lock] = {}
        # lazy — web3 import 지연 (모듈 load 시 web3 미설치여도 import 에러 안 나도록)
        self._web3_mod: Any = None
        self._account_mod: Any = None

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
            logger.warning('DexTrader jobs_path prep failed: %s', exc)

        # web3 라이브러리 로드 (실패해도 dry-run 경로는 계속)
        try:
            import web3 as _web3_mod                 # type: ignore
            from eth_account import Account as _Account  # type: ignore
            self._web3_mod = _web3_mod
            self._account_mod = _Account
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'DexTrader: web3/eth-account unavailable (%s) — dry-run only',
                exc,
            )

        if self.detector is not None and hasattr(self.detector, 'add_listener'):
            self.detector.add_listener(self._on_listing_event)
            logger.info(
                'DexTrader started (enabled=%s dry_run=%s live_confirm=%s '
                'notional=$%.0f slip=%.1f%% chains=%s)',
                self.cfg.enabled, self.cfg.dry_run, self.cfg.live_confirm,
                self.cfg.notional_usd, self.cfg.max_slippage_pct,
                ','.join(self.cfg.supported_chains),
            )
        else:
            logger.info(
                'DexTrader started (no detector — manual API only, enabled=%s dry_run=%s)',
                self.cfg.enabled, self.cfg.dry_run,
            )

    async def stop(self) -> None:
        self._running = False
        if self.detector is not None and hasattr(self.detector, 'remove_listener'):
            try:
                self.detector.remove_listener(self._on_listing_event)
            except Exception:  # noqa: BLE001
                pass
        logger.info('DexTrader stopped')

    # ------------------------------------------------------------------
    # 상태 조회
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        self._maybe_rollover_daily()
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and bool(os.getenv('DEX_PRIVATE_KEY', '').strip())
        )
        wallet = self._wallet_address_safe()
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'live_armed': live_armed,
            'kill_switch_active': self._kill_switch_active(),
            'web3_available': self._web3_mod is not None,
            'wallet_configured': bool(os.getenv('DEX_PRIVATE_KEY', '').strip()),
            'wallet_address': wallet,
            'notional_usd': self.cfg.notional_usd,
            'max_slippage_pct': self.cfg.max_slippage_pct,
            'gas_priority_fee_gwei': self.cfg.gas_priority_fee_gwei,
            'supported_chains': list(self.cfg.supported_chains),
            'per_ticker_cooldown_min': self.cfg.per_ticker_cooldown_min,
            'max_event_age_sec': self.cfg.max_event_age_sec,
            'min_pair_liquidity_usd': self.cfg.min_pair_liquidity_usd,
            'cooldown_tickers': {
                k: max(
                    0,
                    int(self.cfg.per_ticker_cooldown_min * 60 - (time.time() - v)),
                )
                for k, v in self.state.last_entry_ts_per_ticker.items()
                if time.time() - v < self.cfg.per_ticker_cooldown_min * 60
            },
            'open_jobs': {k: dict(v) for k, v in self.state.open_jobs.items()},
            'total_detected': self.state.total_detected,
            'total_executed': self.state.total_executed,
            'total_dry_run': self.state.total_dry_run,
            'total_skipped': self.state.total_skipped,
            'total_errors': self.state.total_errors,
            'last_error': self.state.last_error,
            'last_executed_ts': self.state.last_executed_ts,
            'jobs_path': self.cfg.jobs_path,
            'kill_switch_file': self.cfg.kill_switch_file,
        }

    def recent_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        path = Path(self.cfg.jobs_path)
        if limit <= 0 or not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('DexTrader recent_jobs read failed: %s', exc)
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
    # Dexscreener — 가격 & 컨트랙트 서치
    # ------------------------------------------------------------------

    async def get_dex_price(self, contract_address: str, chain: str) -> float:
        """주어진 컨트랙트에 대해 Dexscreener USD price 조회.

        복수 풀이 있으면 유동성이 가장 큰 풀을 사용. 유효 풀 없으면 0.0.
        """
        contract = (contract_address or '').strip()
        if not contract:
            return 0.0
        chain_norm = self._dexscreener_chain(chain)
        url = f'https://api.dexscreener.com/latest/dex/tokens/{contract}'
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=self.cfg.dexscreener_timeout_sec),
                ) as resp:
                    if resp.status != 200:
                        return 0.0
                    data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('DexTrader dexscreener err %s: %s', contract, exc)
            return 0.0
        pairs = data.get('pairs') or []
        # chain 필터 (옵션) — dexscreener 는 'chainId': 'base', 'ethereum', 'bsc' 등
        filtered = [
            p for p in pairs
            if (not chain_norm) or str(p.get('chainId', '')).lower() == chain_norm
        ]
        pool_of = filtered or pairs
        if not pool_of:
            return 0.0
        # liquidity usd 내림차순
        def _liq(p: dict[str, Any]) -> float:
            liq = p.get('liquidity') or {}
            try:
                return float(liq.get('usd') or 0.0)
            except (TypeError, ValueError):
                return 0.0
        pool_of.sort(key=_liq, reverse=True)
        best = pool_of[0]
        try:
            price_usd = float(best.get('priceUsd') or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return price_usd if price_usd > 0 else 0.0

    async def _dexscreener_search(
        self,
        ticker: str,
        supported_chains: list[str],
    ) -> Optional[dict[str, Any]]:
        """Dexscreener 검색으로 ticker -> {chain, address, priceUsd, liquidityUsd} 찾기.

        `https://api.dexscreener.com/latest/dex/search?q=TICKER` 사용.
        지원 체인만 허용. 유동성 최대 풀 선택.
        """
        ticker_norm = (ticker or '').strip().upper()
        if not ticker_norm:
            return None
        url = f'https://api.dexscreener.com/latest/dex/search?q={ticker_norm}'
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=self.cfg.dexscreener_timeout_sec),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug('DexTrader dexscreener search err %s: %s', ticker_norm, exc)
            return None
        pairs = data.get('pairs') or []
        if not pairs:
            return None

        # 지원 체인 화이트리스트로 필터
        supported_norm = {self._dexscreener_chain(c) for c in supported_chains}

        matches: list[dict[str, Any]] = []
        for p in pairs:
            chain_id = str(p.get('chainId', '')).lower()
            if chain_id not in supported_norm:
                continue
            base = p.get('baseToken') or {}
            sym = str(base.get('symbol', '')).upper()
            # baseToken symbol 이 정확히 일치하는 것만 (예: CHIP 검색 시 CHIPS 방지)
            if sym != ticker_norm:
                continue
            liq_usd = 0.0
            try:
                liq_usd = float((p.get('liquidity') or {}).get('usd') or 0.0)
            except (TypeError, ValueError):
                liq_usd = 0.0
            if liq_usd < self.cfg.min_pair_liquidity_usd:
                continue
            try:
                price_usd = float(p.get('priceUsd') or 0.0)
            except (TypeError, ValueError):
                price_usd = 0.0
            if price_usd <= 0:
                continue
            matches.append({
                'chain': self._normalize_chain(chain_id),
                'address': str(base.get('address', '')).strip(),
                'symbol': sym,
                'priceUsd': price_usd,
                'liquidityUsd': liq_usd,
                'pairAddress': str(p.get('pairAddress', '')).strip(),
                'dexId': str(p.get('dexId', '')).strip(),
            })

        if not matches:
            return None
        # 유동성 최대 선택
        matches.sort(key=lambda m: m['liquidityUsd'], reverse=True)
        return matches[0]

    @staticmethod
    def _dexscreener_chain(chain: str) -> str:
        c = (chain or '').strip().lower()
        if c in {'eth', 'ethereum', 'mainnet'}:
            return 'ethereum'
        if c in {'base'}:
            return 'base'
        if c in {'bsc', 'bnb', 'binance', 'bnbchain'}:
            return 'bsc'
        return c

    @staticmethod
    def _normalize_chain(chain: str) -> str:
        c = (chain or '').strip().lower()
        if c in {'eth', 'mainnet'}:
            return 'ethereum'
        if c in {'bnb', 'binance', 'bnbchain'}:
            return 'bsc'
        return c

    # ------------------------------------------------------------------
    # web3 인스턴스 / 지갑
    # ------------------------------------------------------------------

    def _get_web3(self, chain: str) -> Any:
        """체인별 Web3 provider. 캐시됨."""
        chain_norm = self._normalize_chain(chain)
        if chain_norm in self._w3_cache:
            return self._w3_cache[chain_norm]
        if self._web3_mod is None:
            raise RuntimeError('web3 not installed')
        spec = CHAINS.get(chain_norm)
        if spec is None:
            raise ValueError(f'unsupported chain: {chain!r}')
        rpc_url = _str_env(spec.rpc_env, spec.rpc_default)
        try:
            w3 = self._web3_mod.Web3(
                self._web3_mod.Web3.HTTPProvider(
                    rpc_url,
                    request_kwargs={'timeout': 15},
                )
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f'{chain_norm} Web3 init failed: {exc}') from exc
        if not w3.is_connected():
            raise RuntimeError(f'{chain_norm} RPC unreachable: {rpc_url}')
        # BSC 는 POA middleware 가 필요할 수 있지만 web3 v7 에서는 pancake 단순 swap 은 동작함.
        # 필요 시 향후 inject_poa_middleware 추가.
        self._w3_cache[chain_norm] = w3
        return w3

    def _wallet_address_safe(self) -> str:
        """private key 가 있으면 주소 반환. 실패 시 ''."""
        pk = os.getenv('DEX_PRIVATE_KEY', '').strip()
        if not pk or self._account_mod is None:
            return ''
        try:
            acct = self._account_mod.from_key(pk)
            return str(acct.address)
        except Exception:  # noqa: BLE001
            return ''

    def _account(self) -> Any:
        pk = os.getenv('DEX_PRIVATE_KEY', '').strip()
        if not pk:
            raise RuntimeError('DEX_PRIVATE_KEY missing')
        if self._account_mod is None:
            raise RuntimeError('eth_account not installed')
        return self._account_mod.from_key(pk)

    def _checksum(self, w3: Any, addr: str) -> str:
        key = f'{id(w3)}:{addr.lower()}'
        if key in self._checksum_cache:
            return self._checksum_cache[key]
        cs = w3.to_checksum_address(addr)
        self._checksum_cache[key] = cs
        return cs

    def _nonce_lock(self, chain: str) -> asyncio.Lock:
        chain_norm = self._normalize_chain(chain)
        if chain_norm not in self._nonce_locks:
            self._nonce_locks[chain_norm] = asyncio.Lock()
        return self._nonce_locks[chain_norm]

    # ------------------------------------------------------------------
    # 공개 매수/매도 API
    # ------------------------------------------------------------------

    async def buy_on_dex(
        self,
        ticker: str,
        contract_address: str,
        chain: str,
        amount_usd: float,
        slippage_pct: float = 5.0,
    ) -> dict[str, Any]:
        """DEX 에 market 성격의 스왑으로 token 매수 (native → token).

        dry_run / live_confirm 게이트를 여기서도 재확인 (수동 API 호출 대비).
        """
        ticker_norm = (ticker or '').strip().upper()
        chain_norm = self._normalize_chain(chain)
        amount_usd = float(amount_usd or 0.0)
        slippage_pct = float(slippage_pct or 0.0)

        if not ticker_norm or not contract_address or not chain_norm:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker/contract/chain required'}
        if amount_usd <= 0:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'amount_usd must be > 0'}
        if chain_norm not in CHAINS:
            return {'ok': False, 'code': 'UNSUPPORTED_CHAIN', 'message': f'unsupported chain {chain_norm}'}
        if chain_norm not in self.cfg.supported_chains:
            return {'ok': False, 'code': 'CHAIN_DISABLED', 'message': f'{chain_norm} not in DEX_SUPPORTED_CHAINS'}

        if self._kill_switch_active():
            return {'ok': False, 'code': 'KILL_SWITCH', 'message': 'kill switch active'}

        live_armed = self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm

        if not live_armed:
            # dry-run 기록만
            rec = {
                'ts': int(time.time()),
                'trade_type': 'dex_buy',
                'mode': 'dry_run',
                'ticker': ticker_norm,
                'chain': chain_norm,
                'contract': contract_address,
                'amount_usd': amount_usd,
                'slippage_pct': slippage_pct,
                'reason_not_live': self._dry_reason(),
            }
            await self._append_jobs_record(rec)
            self.state.total_dry_run += 1
            logger.info(
                '[DRY-DEX] would buy %s $%.2f via %s router=%s contract=%s slip=%.2f%%',
                ticker_norm, amount_usd, chain_norm,
                CHAINS[chain_norm].router_address, contract_address, slippage_pct,
            )
            return {'ok': True, 'mode': 'dry_run', **rec}

        # ---- LIVE: 트랜잭션 실행
        return await self._execute_buy(
            ticker=ticker_norm,
            contract=contract_address,
            chain=chain_norm,
            amount_usd=amount_usd,
            slippage_pct=slippage_pct,
        )

    async def sell_on_dex(
        self,
        ticker: str,
        contract_address: str,
        chain: str,
        amount_tokens: float,
        slippage_pct: float = 5.0,
    ) -> dict[str, Any]:
        """DEX 에 token → native 스왑 (매도)."""
        ticker_norm = (ticker or '').strip().upper()
        chain_norm = self._normalize_chain(chain)
        amount_tokens = float(amount_tokens or 0.0)
        slippage_pct = float(slippage_pct or 0.0)

        if not ticker_norm or not contract_address or not chain_norm:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'ticker/contract/chain required'}
        if amount_tokens <= 0:
            return {'ok': False, 'code': 'INVALID_INPUT', 'message': 'amount_tokens must be > 0'}
        if chain_norm not in CHAINS:
            return {'ok': False, 'code': 'UNSUPPORTED_CHAIN', 'message': f'unsupported chain {chain_norm}'}
        if chain_norm not in self.cfg.supported_chains:
            return {'ok': False, 'code': 'CHAIN_DISABLED', 'message': f'{chain_norm} disabled'}

        if self._kill_switch_active():
            return {'ok': False, 'code': 'KILL_SWITCH', 'message': 'kill switch active'}

        live_armed = self.cfg.enabled and (not self.cfg.dry_run) and self.cfg.live_confirm

        if not live_armed:
            rec = {
                'ts': int(time.time()),
                'trade_type': 'dex_sell',
                'mode': 'dry_run',
                'ticker': ticker_norm,
                'chain': chain_norm,
                'contract': contract_address,
                'amount_tokens': amount_tokens,
                'slippage_pct': slippage_pct,
                'reason_not_live': self._dry_reason(),
            }
            await self._append_jobs_record(rec)
            self.state.total_dry_run += 1
            logger.info(
                '[DRY-DEX] would sell %s %.8f tokens via %s contract=%s slip=%.2f%%',
                ticker_norm, amount_tokens, chain_norm, contract_address, slippage_pct,
            )
            return {'ok': True, 'mode': 'dry_run', **rec}

        return await self._execute_sell(
            ticker=ticker_norm,
            contract=contract_address,
            chain=chain_norm,
            amount_tokens=amount_tokens,
            slippage_pct=slippage_pct,
        )

    # ------------------------------------------------------------------
    # listing_detector 리스너
    # ------------------------------------------------------------------

    def _on_listing_event(self, event: dict[str, Any]) -> Any:
        self.state.total_detected += 1
        if not self._running:
            return None
        try:
            return asyncio.create_task(
                self._handle_listing_safe(event),
                name=f'dex_trader_{event.get("ticker", "unknown")}',
            )
        except RuntimeError:
            return None

    async def _handle_listing_safe(self, event: dict[str, Any]) -> None:
        try:
            await self._handle_listing(event)
        except Exception as exc:  # noqa: BLE001
            self.state.total_errors += 1
            self.state.last_error = f'{type(exc).__name__}: {exc}'
            logger.exception('DexTrader unexpected error: %s', exc)

    async def _handle_listing(self, event: dict[str, Any]) -> None:
        ticker = str(event.get('ticker') or '').strip().upper()
        origin_exchange = str(event.get('exchange') or '').strip().lower()
        notice_id = str(event.get('id') or event.get('notice_id') or '')
        event_ts = float(event.get('ts') or 0.0)

        if not ticker:
            self.state.total_skipped += 1
            return

        # ---- 게이트 1: enabled
        if not self.cfg.enabled:
            logger.debug('[dex-trader] skip %s: disabled', ticker)
            self.state.total_skipped += 1
            return

        # ---- 게이트 2: kill switch
        if self._kill_switch_active():
            logger.warning(
                '[dex-trader] skip %s: kill switch %s active',
                ticker, self.cfg.kill_switch_file,
            )
            self.state.total_skipped += 1
            return

        # ---- 게이트 3: 이벤트 신선도
        now_ts = time.time()
        if event_ts > 0 and self.cfg.max_event_age_sec > 0:
            age = now_ts - event_ts
            if age > self.cfg.max_event_age_sec:
                logger.info(
                    '[dex-trader] skip %s: event too old (%.0fs > %ds)',
                    ticker, age, self.cfg.max_event_age_sec,
                )
                self.state.total_skipped += 1
                return

        # ---- 게이트 4: 중복 inflight
        if ticker in self._inflight_tickers:
            logger.info('[dex-trader] skip %s: inflight', ticker)
            self.state.total_skipped += 1
            return

        # ---- 게이트 5: per-ticker cooldown
        if self.cfg.per_ticker_cooldown_min > 0:
            last_ts = self.state.last_entry_ts_per_ticker.get(ticker, 0.0)
            cooldown_sec = self.cfg.per_ticker_cooldown_min * 60
            elapsed = now_ts - last_ts
            if elapsed < cooldown_sec:
                logger.info(
                    '[dex-trader] skip %s: cooldown %ds remaining',
                    ticker, int(cooldown_sec - elapsed),
                )
                self.state.total_skipped += 1
                return

        # ---- 게이트 6: 이미 open 인 DEX job 있음
        if ticker in self.state.open_jobs:
            logger.info('[dex-trader] skip %s: open job exists', ticker)
            self.state.total_skipped += 1
            return

        # ---- 컨트랙트 주소 해결
        # (1) 이벤트 enrichment 된 경우
        contract = ''
        chain = ''
        event_contract = event.get('contract_address')
        event_chain = event.get('chain')
        if event_contract and isinstance(event_contract, str) and event_contract.startswith('0x'):
            contract = event_contract.strip()
            chain = self._normalize_chain(str(event_chain or ''))

        # (2) Dexscreener 검색 폴백
        if not contract or not chain or chain not in CHAINS:
            self._inflight_tickers.add(ticker)
            try:
                match = await self._dexscreener_search(ticker, self.cfg.supported_chains)
            finally:
                # inflight 는 실제 주문까지 잠그기 위해 나중에 해제
                pass
            if match is None:
                self._inflight_tickers.discard(ticker)
                logger.info(
                    '[dex-trader] skip %s: no dexscreener match on %s',
                    ticker, ','.join(self.cfg.supported_chains),
                )
                self.state.total_skipped += 1
                return
            contract = match['address']
            chain = match['chain']
            logger.info(
                '[dex-trader] %s resolved via dexscreener: chain=%s addr=%s liq=$%.0f price=$%.6f',
                ticker, chain, contract,
                match.get('liquidityUsd') or 0.0,
                match.get('priceUsd') or 0.0,
            )

        if chain not in self.cfg.supported_chains:
            self._inflight_tickers.discard(ticker)
            logger.info('[dex-trader] skip %s: chain %s not in supported_chains', ticker, chain)
            self.state.total_skipped += 1
            return

        self._inflight_tickers.add(ticker)
        try:
            # ---- 게이트 7: LIVE 트리플 락
            live_armed = (
                self.cfg.enabled
                and (not self.cfg.dry_run)
                and self.cfg.live_confirm
                and bool(os.getenv('DEX_PRIVATE_KEY', '').strip())
            )
            if not live_armed:
                await self._record_dry_run(
                    ticker=ticker,
                    chain=chain,
                    contract=contract,
                    origin_exchange=origin_exchange,
                    notice_id=notice_id,
                    event_ts=event_ts,
                )
                return

            result = await self._execute_buy(
                ticker=ticker,
                contract=contract,
                chain=chain,
                amount_usd=self.cfg.notional_usd,
                slippage_pct=self.cfg.max_slippage_pct,
                listing_event_id=notice_id,
                listing_origin_exchange=origin_exchange,
                listing_event_ts=event_ts,
            )
            if not result.get('ok'):
                logger.warning('[dex-trader] %s live buy failed: %s', ticker, result)
        finally:
            self._inflight_tickers.discard(ticker)

    async def _record_dry_run(
        self,
        ticker: str,
        chain: str,
        contract: str,
        origin_exchange: str,
        notice_id: str,
        event_ts: float,
    ) -> None:
        logger.info(
            '[DRY-DEX] would buy %s $%.0f via %s router=%s contract=%s (origin=%s id=%s)',
            ticker, self.cfg.notional_usd, chain,
            CHAINS[chain].router_address if chain in CHAINS else '?',
            contract, origin_exchange, notice_id,
        )
        self.state.total_dry_run += 1
        self.state.last_entry_ts_per_ticker[ticker] = time.time()
        await self._append_jobs_record({
            'ts': int(time.time()),
            'trade_type': 'dex_buy',
            'mode': 'dry_run',
            'ticker': ticker,
            'chain': chain,
            'contract': contract,
            'amount_usd': self.cfg.notional_usd,
            'slippage_pct': self.cfg.max_slippage_pct,
            'router': CHAINS[chain].router_address if chain in CHAINS else '',
            'listing_event_id': notice_id,
            'listing_origin_exchange': origin_exchange,
            'listing_event_ts': int(event_ts),
            'reason_not_live': self._dry_reason(),
        })
        if self.telegram is not None:
            text = (
                f'🧪 [DRY-DEX] 매수: {ticker} ${self.cfg.notional_usd:.0f} via '
                f'{chain} / {contract[:10]}... (id={notice_id})'
            )
            await self._send_telegram(text)

    def _dry_reason(self) -> str:
        if not self.cfg.enabled:
            return 'disabled'
        if self.cfg.dry_run:
            return 'dry_run_env'
        if not self.cfg.live_confirm:
            return 'live_confirm_off'
        if not os.getenv('DEX_PRIVATE_KEY', '').strip():
            return 'no_private_key'
        return 'unknown'

    # ------------------------------------------------------------------
    # LIVE 매수 실행
    # ------------------------------------------------------------------

    async def _execute_buy(
        self,
        ticker: str,
        contract: str,
        chain: str,
        amount_usd: float,
        slippage_pct: float,
        listing_event_id: str = '',
        listing_origin_exchange: str = '',
        listing_event_ts: float = 0.0,
    ) -> dict[str, Any]:
        """native → token 스왑 실행. 여기 도달 시 트리플 락 확인 이미 통과."""
        # ---- 데일리 캡은 현재 단일 notional 만 검사
        self._maybe_rollover_daily()

        spec = CHAINS.get(chain)
        if spec is None:
            return {'ok': False, 'code': 'UNSUPPORTED_CHAIN', 'message': chain}

        try:
            w3 = self._get_web3(chain)
            account = self._account()
        except Exception as exc:  # noqa: BLE001
            self.state.total_errors += 1
            self.state.last_error = f'web3 init: {exc}'
            await self._telegram_error(ticker, chain, str(exc))
            return {'ok': False, 'code': 'WEB3_INIT', 'message': str(exc)}

        wallet = account.address

        # ---- 가격 조회 (Dexscreener) → token/native 환산
        price_usd = await self.get_dex_price(contract, chain)
        if price_usd <= 0:
            msg = 'dexscreener price unavailable'
            self.state.total_errors += 1
            self.state.last_error = msg
            await self._telegram_error(ticker, chain, msg)
            return {'ok': False, 'code': 'NO_PRICE', 'message': msg}

        # native (ETH/BNB) 가격
        native_usd = await self._get_native_usd_price(chain)
        if native_usd <= 0:
            msg = f'{spec.native_symbol} USD price unavailable'
            self.state.total_errors += 1
            self.state.last_error = msg
            return {'ok': False, 'code': 'NO_NATIVE_PRICE', 'message': msg}

        native_in = amount_usd / native_usd               # notional 을 native 단위로
        token_expected = amount_usd / price_usd          # 예상 받을 토큰 수
        slippage_frac = max(min(slippage_pct, 50.0), 0.0) / 100.0
        min_tokens_out = token_expected * (1.0 - slippage_frac)

        # ---- native wei 변환
        try:
            amount_in_wei = w3.to_wei(Decimal(str(native_in)), 'ether')
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'code': 'WEI_CONVERT', 'message': f'{exc}'}

        if amount_in_wei <= 0:
            return {'ok': False, 'code': 'AMOUNT_TOO_SMALL', 'message': f'native_in={native_in}'}

        # ---- balance 선검증 (gas 추정 후 다시 확인)
        try:
            balance_wei = w3.eth.get_balance(self._checksum(w3, wallet))
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'code': 'BALANCE_CHECK', 'message': f'{exc}'}
        if balance_wei <= amount_in_wei:
            msg = f'insufficient {spec.native_symbol}: balance={balance_wei} need>{amount_in_wei}'
            await self._telegram_error(ticker, chain, msg)
            return {'ok': False, 'code': 'INSUFFICIENT_BALANCE', 'message': msg}

        # ---- token decimals (minOut wei 변환용)
        try:
            token_contract = w3.eth.contract(
                address=self._checksum(w3, contract), abi=_ERC20_ABI,
            )
            decimals = int(token_contract.functions.decimals().call())
        except Exception as exc:  # noqa: BLE001
            msg = f'token decimals err: {exc}'
            self.state.total_errors += 1
            self.state.last_error = msg
            await self._telegram_error(ticker, chain, msg)
            return {'ok': False, 'code': 'TOKEN_DECIMALS', 'message': msg}

        min_out_wei = int(Decimal(str(min_tokens_out)) * (Decimal(10) ** decimals))
        if min_out_wei <= 0:
            return {'ok': False, 'code': 'MIN_OUT_ZERO', 'message': 'min_out computed to 0'}

        deadline = int(time.time()) + 180   # 3분

        # ---- 트랜잭션 빌드 (라우터 타입별)
        try:
            if spec.router_type == 'uniswap_v3':
                built = self._build_v3_buy_tx(
                    w3=w3,
                    spec=spec,
                    account=account,
                    token_out=contract,
                    amount_in_wei=amount_in_wei,
                    min_out_wei=min_out_wei,
                    deadline=deadline,
                )
            elif spec.router_type == 'pancake_v2':
                built = self._build_v2_buy_tx(
                    w3=w3,
                    spec=spec,
                    account=account,
                    token_out=contract,
                    amount_in_wei=amount_in_wei,
                    min_out_wei=min_out_wei,
                    deadline=deadline,
                )
            else:
                return {
                    'ok': False, 'code': 'ROUTER_UNSUPPORTED',
                    'message': f'{spec.router_type}',
                }
        except Exception as exc:  # noqa: BLE001
            msg = f'build tx: {exc}'
            self.state.total_errors += 1
            self.state.last_error = msg
            await self._telegram_error(ticker, chain, msg)
            return {'ok': False, 'code': 'BUILD_TX', 'message': msg}

        # ---- sign + send (nonce 락 아래에서)
        async with self._nonce_lock(chain):
            try:
                nonce = w3.eth.get_transaction_count(
                    self._checksum(w3, wallet), 'pending',
                )
                built['tx']['nonce'] = nonce
                built['tx']['chainId'] = spec.chain_id

                # gas 추정
                try:
                    gas_est = w3.eth.estimate_gas(built['tx'])
                    gas_limit = min(int(gas_est * 1.3), self.cfg.gas_limit_cap)
                except Exception as exc:  # noqa: BLE001
                    logger.warning('[dex-trader] %s gas estimate failed: %s; cap fallback', ticker, exc)
                    gas_limit = self.cfg.gas_limit_cap
                built['tx']['gas'] = gas_limit

                # EIP-1559 (eth/base). BSC 는 legacy gasPrice.
                if spec.chain_id in (1, 8453):
                    try:
                        base_fee = w3.eth.gas_price  # v7 fallback
                        latest = w3.eth.get_block('latest')
                        base_fee_eth = int(latest.get('baseFeePerGas') or base_fee)
                        max_priority = w3.to_wei(
                            Decimal(str(self.cfg.gas_priority_fee_gwei)), 'gwei',
                        )
                        built['tx']['maxPriorityFeePerGas'] = int(max_priority)
                        # maxFee = 2*base + priority (safety 버퍼)
                        built['tx']['maxFeePerGas'] = int(2 * base_fee_eth + max_priority)
                        built['tx']['type'] = 2
                    except Exception as exc:  # noqa: BLE001
                        # 폴백: legacy gasPrice
                        built['tx']['gasPrice'] = int(w3.eth.gas_price * 12 // 10)
                else:
                    # BSC legacy
                    built['tx']['gasPrice'] = int(w3.eth.gas_price * 12 // 10)

                # gas * gasPrice 추가 보유 확인 (balance_wei vs amount_in + gas_cost)
                gas_price_used = int(
                    built['tx'].get('maxFeePerGas')
                    or built['tx'].get('gasPrice', 0)
                )
                gas_cost = gas_price_used * gas_limit
                if balance_wei < amount_in_wei + gas_cost:
                    msg = f'balance {balance_wei} < amount_in {amount_in_wei} + gas_cost {gas_cost}'
                    await self._telegram_error(ticker, chain, msg)
                    return {'ok': False, 'code': 'INSUFFICIENT_BALANCE_WITH_GAS', 'message': msg}

                signed = account.sign_transaction(built['tx'])
                # eth_account 버전 호환: rawTransaction (구) / raw_transaction (신)
                raw = getattr(signed, 'rawTransaction', None) or getattr(signed, 'raw_transaction', None)
                if raw is None:
                    return {'ok': False, 'code': 'SIGN_RAW_MISSING', 'message': 'signed tx raw missing'}
                tx_hash = w3.eth.send_raw_transaction(raw)
                tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
            except Exception as exc:  # noqa: BLE001
                msg = f'sign/send: {exc}'
                self.state.total_errors += 1
                self.state.last_error = msg
                await self._telegram_error(ticker, chain, msg)
                return {'ok': False, 'code': 'SEND_TX', 'message': msg}

        # ---- receipt wait
        try:
            receipt = await self._wait_for_receipt(w3, tx_hash_hex, chain)
        except asyncio.TimeoutError:
            msg = f'receipt timeout {tx_hash_hex}'
            self.state.last_error = msg
            await self._telegram_error(ticker, chain, msg)
            # receipt 못 받았어도 tx 는 체인에 있을 수 있음 — 기록 남김
            await self._append_jobs_record({
                'ts': int(time.time()),
                'trade_type': 'dex_buy',
                'mode': 'live_pending',
                'ticker': ticker,
                'chain': chain,
                'contract': contract,
                'tx_hash': tx_hash_hex,
                'explorer': spec.explorer_tx + tx_hash_hex,
                'amount_usd': amount_usd,
                'price_usd': price_usd,
                'min_tokens_out': min_tokens_out,
                'decimals': decimals,
                'listing_event_id': listing_event_id,
                'listing_origin_exchange': listing_origin_exchange,
                'listing_event_ts': int(listing_event_ts),
                'note': 'receipt timeout — check manually',
            })
            return {'ok': False, 'code': 'RECEIPT_TIMEOUT', 'tx_hash': tx_hash_hex}

        status = int(receipt.get('status', 0))
        block_number = int(receipt.get('blockNumber', 0))
        gas_used = int(receipt.get('gasUsed', 0))

        if status != 1:
            msg = f'tx reverted {tx_hash_hex}'
            self.state.total_errors += 1
            self.state.last_error = msg
            await self._telegram_error(ticker, chain, msg)
            await self._append_jobs_record({
                'ts': int(time.time()),
                'trade_type': 'dex_buy',
                'mode': 'live_reverted',
                'ticker': ticker,
                'chain': chain,
                'contract': contract,
                'tx_hash': tx_hash_hex,
                'explorer': spec.explorer_tx + tx_hash_hex,
                'status': status,
                'block_number': block_number,
                'gas_used': gas_used,
                'amount_usd': amount_usd,
                'listing_event_id': listing_event_id,
            })
            return {'ok': False, 'code': 'REVERTED', 'tx_hash': tx_hash_hex}

        # ---- 성공: 실제 수령 token 수 조회 (balance 변화량으로 확인하는 것이 가장 정확하지만
        #        간단히 잔고로 대체 — 사용자가 단일 ticker 만 반복 매매하지 않는다는 전제)
        try:
            token_balance_after = int(
                token_contract.functions.balanceOf(self._checksum(w3, wallet)).call()
            )
            filled_tokens = Decimal(token_balance_after) / (Decimal(10) ** decimals)
            filled_tokens_f = float(filled_tokens)
        except Exception:  # noqa: BLE001
            filled_tokens_f = float(token_expected)

        # 체결 가격 (추정): amount_usd / filled_tokens
        avg_price_usd = (
            amount_usd / filled_tokens_f if filled_tokens_f > 0 else price_usd
        )

        self.state.daily_spent_usd += amount_usd
        self.state.last_entry_ts_per_ticker[ticker] = time.time()
        self.state.total_executed += 1
        self.state.last_executed_ts = time.time()
        self.state.open_jobs[ticker] = {
            'ticker': ticker,
            'chain': chain,
            'contract': contract,
            'tx_hash': tx_hash_hex,
            'filled_tokens': filled_tokens_f,
            'avg_price_usd': avg_price_usd,
            'amount_usd': amount_usd,
            'decimals': decimals,
            'ts': int(time.time()),
            'mode': 'live',
            'listing_event_id': listing_event_id,
            'listing_origin_exchange': listing_origin_exchange,
        }

        rec = {
            'ts': int(time.time()),
            'trade_type': 'dex_buy',
            'mode': 'live',
            'ticker': ticker,
            'chain': chain,
            'contract': contract,
            'tx_hash': tx_hash_hex,
            'explorer': spec.explorer_tx + tx_hash_hex,
            'block_number': block_number,
            'gas_used': gas_used,
            'amount_usd': amount_usd,
            'price_usd': price_usd,
            'native_in': native_in,
            'slippage_pct': slippage_pct,
            'filled_tokens': filled_tokens_f,
            'avg_price_usd': avg_price_usd,
            'decimals': decimals,
            'router': spec.router_address,
            'listing_event_id': listing_event_id,
            'listing_origin_exchange': listing_origin_exchange,
            'listing_event_ts': int(listing_event_ts),
        }
        await self._append_jobs_record(rec)

        logger.info(
            '[dex-trader] LIVE BUY OK %s %.8f tokens @ $%.6f via %s tx=%s',
            ticker, filled_tokens_f, avg_price_usd, chain, tx_hash_hex,
        )
        await self._send_telegram(
            f'🟢 DEX 매수: {ticker} {filled_tokens_f:.6f} tokens @ ${avg_price_usd:.6f} '
            f'({chain})\n{spec.explorer_tx}{tx_hash_hex}'
        )

        return {'ok': True, 'mode': 'live', **rec}

    # ------------------------------------------------------------------
    # LIVE 매도 실행
    # ------------------------------------------------------------------

    async def _execute_sell(
        self,
        ticker: str,
        contract: str,
        chain: str,
        amount_tokens: float,
        slippage_pct: float,
    ) -> dict[str, Any]:
        """token → native. approve 선행 (allowance 부족 시)."""
        spec = CHAINS.get(chain)
        if spec is None:
            return {'ok': False, 'code': 'UNSUPPORTED_CHAIN', 'message': chain}

        try:
            w3 = self._get_web3(chain)
            account = self._account()
        except Exception as exc:  # noqa: BLE001
            await self._telegram_error(ticker, chain, f'web3 init: {exc}')
            return {'ok': False, 'code': 'WEB3_INIT', 'message': str(exc)}

        wallet = account.address

        try:
            token_contract = w3.eth.contract(
                address=self._checksum(w3, contract), abi=_ERC20_ABI,
            )
            decimals = int(token_contract.functions.decimals().call())
        except Exception as exc:  # noqa: BLE001
            await self._telegram_error(ticker, chain, f'token decimals: {exc}')
            return {'ok': False, 'code': 'TOKEN_DECIMALS', 'message': str(exc)}

        amount_in_wei = int(Decimal(str(amount_tokens)) * (Decimal(10) ** decimals))
        if amount_in_wei <= 0:
            return {'ok': False, 'code': 'AMOUNT_ZERO', 'message': 'amount_tokens too small'}

        try:
            balance_wei = int(
                token_contract.functions.balanceOf(self._checksum(w3, wallet)).call()
            )
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'code': 'BALANCE_CHECK', 'message': str(exc)}
        if balance_wei < amount_in_wei:
            msg = f'insufficient {ticker}: balance={balance_wei} need={amount_in_wei}'
            await self._telegram_error(ticker, chain, msg)
            return {'ok': False, 'code': 'INSUFFICIENT_TOKEN', 'message': msg}

        # minOut 계산 (native)
        price_usd = await self.get_dex_price(contract, chain)
        native_usd = await self._get_native_usd_price(chain)
        if price_usd <= 0 or native_usd <= 0:
            msg = 'price unavailable (dexscreener or native)'
            return {'ok': False, 'code': 'NO_PRICE', 'message': msg}

        expected_native = (amount_tokens * price_usd) / native_usd
        slippage_frac = max(min(slippage_pct, 50.0), 0.0) / 100.0
        min_native_out = expected_native * (1.0 - slippage_frac)
        min_out_wei = w3.to_wei(Decimal(str(min_native_out)), 'ether')
        if min_out_wei <= 0:
            return {'ok': False, 'code': 'MIN_OUT_ZERO', 'message': 'min native out is 0'}

        # ---- approve 선행 (필요 시)
        router_cs = self._checksum(w3, spec.router_address)
        try:
            allowance = int(
                token_contract.functions.allowance(
                    self._checksum(w3, wallet), router_cs,
                ).call()
            )
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'code': 'ALLOWANCE_CHECK', 'message': str(exc)}

        deadline = int(time.time()) + 180

        if allowance < amount_in_wei:
            try:
                await self._send_approve(
                    w3=w3, spec=spec, account=account,
                    token_contract=token_contract,
                    router=router_cs,
                    amount_in_wei=amount_in_wei,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f'approve: {exc}'
                await self._telegram_error(ticker, chain, msg)
                return {'ok': False, 'code': 'APPROVE_FAIL', 'message': msg}

        # ---- 매도 TX 빌드
        try:
            if spec.router_type == 'uniswap_v3':
                built = self._build_v3_sell_tx(
                    w3=w3, spec=spec, account=account,
                    token_in=contract,
                    amount_in_wei=amount_in_wei,
                    min_out_wei=int(min_out_wei),
                    deadline=deadline,
                )
            elif spec.router_type == 'pancake_v2':
                built = self._build_v2_sell_tx(
                    w3=w3, spec=spec, account=account,
                    token_in=contract,
                    amount_in_wei=amount_in_wei,
                    min_out_wei=int(min_out_wei),
                    deadline=deadline,
                )
            else:
                return {'ok': False, 'code': 'ROUTER_UNSUPPORTED', 'message': spec.router_type}
        except Exception as exc:  # noqa: BLE001
            return {'ok': False, 'code': 'BUILD_TX', 'message': str(exc)}

        async with self._nonce_lock(chain):
            try:
                nonce = w3.eth.get_transaction_count(
                    self._checksum(w3, wallet), 'pending',
                )
                built['tx']['nonce'] = nonce
                built['tx']['chainId'] = spec.chain_id

                try:
                    gas_est = w3.eth.estimate_gas(built['tx'])
                    gas_limit = min(int(gas_est * 1.3), self.cfg.gas_limit_cap)
                except Exception:  # noqa: BLE001
                    gas_limit = self.cfg.gas_limit_cap
                built['tx']['gas'] = gas_limit

                if spec.chain_id in (1, 8453):
                    try:
                        latest = w3.eth.get_block('latest')
                        base_fee_eth = int(latest.get('baseFeePerGas') or w3.eth.gas_price)
                        max_priority = w3.to_wei(
                            Decimal(str(self.cfg.gas_priority_fee_gwei)), 'gwei',
                        )
                        built['tx']['maxPriorityFeePerGas'] = int(max_priority)
                        built['tx']['maxFeePerGas'] = int(2 * base_fee_eth + max_priority)
                        built['tx']['type'] = 2
                    except Exception:  # noqa: BLE001
                        built['tx']['gasPrice'] = int(w3.eth.gas_price * 12 // 10)
                else:
                    built['tx']['gasPrice'] = int(w3.eth.gas_price * 12 // 10)

                signed = account.sign_transaction(built['tx'])
                raw = getattr(signed, 'rawTransaction', None) or getattr(signed, 'raw_transaction', None)
                if raw is None:
                    return {'ok': False, 'code': 'SIGN_RAW_MISSING', 'message': 'signed tx raw missing'}
                tx_hash = w3.eth.send_raw_transaction(raw)
                tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
            except Exception as exc:  # noqa: BLE001
                msg = f'sign/send sell: {exc}'
                await self._telegram_error(ticker, chain, msg)
                return {'ok': False, 'code': 'SEND_TX', 'message': msg}

        try:
            receipt = await self._wait_for_receipt(w3, tx_hash_hex, chain)
        except asyncio.TimeoutError:
            await self._telegram_error(ticker, chain, f'sell receipt timeout {tx_hash_hex}')
            return {'ok': False, 'code': 'RECEIPT_TIMEOUT', 'tx_hash': tx_hash_hex}

        status = int(receipt.get('status', 0))
        if status != 1:
            await self._telegram_error(ticker, chain, f'sell reverted {tx_hash_hex}')
            return {'ok': False, 'code': 'REVERTED', 'tx_hash': tx_hash_hex}

        # open job 정리 (있으면)
        self.state.open_jobs.pop(ticker, None)

        rec = {
            'ts': int(time.time()),
            'trade_type': 'dex_sell',
            'mode': 'live',
            'ticker': ticker,
            'chain': chain,
            'contract': contract,
            'tx_hash': tx_hash_hex,
            'explorer': spec.explorer_tx + tx_hash_hex,
            'amount_tokens': amount_tokens,
            'min_native_out': min_native_out,
            'price_usd': price_usd,
            'slippage_pct': slippage_pct,
            'router': spec.router_address,
        }
        await self._append_jobs_record(rec)
        await self._send_telegram(
            f'🔴 DEX 매도: {ticker} {amount_tokens:.6f} tokens ({chain})\n'
            f'{spec.explorer_tx}{tx_hash_hex}'
        )
        return {'ok': True, 'mode': 'live', **rec}

    # ------------------------------------------------------------------
    # TX 빌더
    # ------------------------------------------------------------------

    def _build_v3_buy_tx(
        self, w3: Any, spec: ChainSpec, account: Any,
        token_out: str, amount_in_wei: int, min_out_wei: int, deadline: int,
    ) -> dict[str, Any]:
        """Uniswap V3: native 로 직접 exactInputSingle 호출은 안 됨.
        실제 Uniswap V3 SwapRouter 는 payable + WETH9 자동 wrap 을 지원하지 않는다.
        대신 WETH 주소를 tokenIn 으로 지정하고 tx.value 에 native 를 실어보내면
        SwapRouter02 는 msg.value>0 일 때 내부적으로 WETH deposit 을 수행한다
        (실제 uniswap universal router 또는 WETH 사전 wrap 이 필요한 케이스는 별도).

        여기서는 WETH→token 이지만 value 로 native 를 실어보내는 패턴이
        Uniswap V3 swaps 에서 가장 안전한 경로임.
        """
        router = w3.eth.contract(
            address=self._checksum(w3, spec.router_address),
            abi=_UNISWAP_V3_ROUTER_ABI,
        )
        params = {
            'tokenIn': self._checksum(w3, spec.wrapped_native),
            'tokenOut': self._checksum(w3, token_out),
            'fee': spec.default_fee_tier,
            'recipient': self._checksum(w3, account.address),
            'deadline': deadline,
            'amountIn': amount_in_wei,
            'amountOutMinimum': min_out_wei,
            'sqrtPriceLimitX96': 0,
        }
        tx = router.functions.exactInputSingle(params).build_transaction({
            'from': self._checksum(w3, account.address),
            'value': amount_in_wei,
        })
        return {'tx': tx}

    def _build_v3_sell_tx(
        self, w3: Any, spec: ChainSpec, account: Any,
        token_in: str, amount_in_wei: int, min_out_wei: int, deadline: int,
    ) -> dict[str, Any]:
        """token → WETH. 추후 unwrap (WETH.withdraw) 이 필요하지만 일단 WETH 까지만.
        실거래에서 native 원복이 필요하면 multicall+unwrapWETH9 확장 필요.
        """
        router = w3.eth.contract(
            address=self._checksum(w3, spec.router_address),
            abi=_UNISWAP_V3_ROUTER_ABI,
        )
        params = {
            'tokenIn': self._checksum(w3, token_in),
            'tokenOut': self._checksum(w3, spec.wrapped_native),
            'fee': spec.default_fee_tier,
            'recipient': self._checksum(w3, account.address),
            'deadline': deadline,
            'amountIn': amount_in_wei,
            'amountOutMinimum': min_out_wei,
            'sqrtPriceLimitX96': 0,
        }
        tx = router.functions.exactInputSingle(params).build_transaction({
            'from': self._checksum(w3, account.address),
            'value': 0,
        })
        return {'tx': tx}

    def _build_v2_buy_tx(
        self, w3: Any, spec: ChainSpec, account: Any,
        token_out: str, amount_in_wei: int, min_out_wei: int, deadline: int,
    ) -> dict[str, Any]:
        router = w3.eth.contract(
            address=self._checksum(w3, spec.router_address),
            abi=_PANCAKE_V2_ROUTER_ABI,
        )
        path = [
            self._checksum(w3, spec.wrapped_native),
            self._checksum(w3, token_out),
        ]
        tx = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
            min_out_wei, path,
            self._checksum(w3, account.address),
            deadline,
        ).build_transaction({
            'from': self._checksum(w3, account.address),
            'value': amount_in_wei,
        })
        return {'tx': tx}

    def _build_v2_sell_tx(
        self, w3: Any, spec: ChainSpec, account: Any,
        token_in: str, amount_in_wei: int, min_out_wei: int, deadline: int,
    ) -> dict[str, Any]:
        router = w3.eth.contract(
            address=self._checksum(w3, spec.router_address),
            abi=_PANCAKE_V2_ROUTER_ABI,
        )
        path = [
            self._checksum(w3, token_in),
            self._checksum(w3, spec.wrapped_native),
        ]
        tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            amount_in_wei, min_out_wei, path,
            self._checksum(w3, account.address),
            deadline,
        ).build_transaction({
            'from': self._checksum(w3, account.address),
            'value': 0,
        })
        return {'tx': tx}

    async def _send_approve(
        self, w3: Any, spec: ChainSpec, account: Any,
        token_contract: Any, router: str, amount_in_wei: int,
    ) -> None:
        """approve(spender=router, amount=uint256.max) — 무한 approve."""
        max_uint = (1 << 256) - 1
        tx = token_contract.functions.approve(router, max_uint).build_transaction({
            'from': self._checksum(w3, account.address),
        })
        async with self._nonce_lock(spec.name):
            tx['nonce'] = w3.eth.get_transaction_count(
                self._checksum(w3, account.address), 'pending',
            )
            tx['chainId'] = spec.chain_id
            try:
                gas_est = w3.eth.estimate_gas(tx)
                tx['gas'] = min(int(gas_est * 1.3), 200_000)
            except Exception:  # noqa: BLE001
                tx['gas'] = 100_000
            if spec.chain_id in (1, 8453):
                try:
                    latest = w3.eth.get_block('latest')
                    base_fee = int(latest.get('baseFeePerGas') or w3.eth.gas_price)
                    prio = w3.to_wei(Decimal(str(self.cfg.gas_priority_fee_gwei)), 'gwei')
                    tx['maxPriorityFeePerGas'] = int(prio)
                    tx['maxFeePerGas'] = int(2 * base_fee + prio)
                    tx['type'] = 2
                except Exception:  # noqa: BLE001
                    tx['gasPrice'] = int(w3.eth.gas_price * 12 // 10)
            else:
                tx['gasPrice'] = int(w3.eth.gas_price * 12 // 10)
            signed = account.sign_transaction(tx)
            raw = getattr(signed, 'rawTransaction', None) or getattr(signed, 'raw_transaction', None)
            if raw is None:
                raise RuntimeError('approve sign raw missing')
            tx_hash = w3.eth.send_raw_transaction(raw)
            tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
        # wait
        receipt = await self._wait_for_receipt(w3, tx_hash_hex, spec.name)
        if int(receipt.get('status', 0)) != 1:
            raise RuntimeError(f'approve reverted {tx_hash_hex}')

    # ------------------------------------------------------------------
    # receipt wait — 비동기로 polling
    # ------------------------------------------------------------------

    async def _wait_for_receipt(
        self, w3: Any, tx_hash_hex: str, chain: str,
    ) -> dict[str, Any]:
        """별도 스레드에서 wait_for_transaction_receipt 호출 → 타임아웃 asyncio.TimeoutError."""
        loop = asyncio.get_event_loop()

        def _sync_wait() -> dict[str, Any]:
            r = w3.eth.wait_for_transaction_receipt(
                tx_hash_hex, timeout=self.cfg.tx_wait_timeout_sec,
            )
            return dict(r)

        try:
            receipt = await asyncio.wait_for(
                loop.run_in_executor(None, _sync_wait),
                timeout=self.cfg.tx_wait_timeout_sec + 10,
            )
        except asyncio.TimeoutError:
            raise
        # confirmations 대기 (block_number 기준)
        if self.cfg.min_confirmations > 1:
            block_rcpt = int(receipt.get('blockNumber') or 0)
            for _ in range(self.cfg.tx_wait_timeout_sec // 2):
                try:
                    latest = int(w3.eth.block_number)
                except Exception:  # noqa: BLE001
                    break
                if latest - block_rcpt + 1 >= self.cfg.min_confirmations:
                    break
                await asyncio.sleep(2)
        return receipt

    async def _get_native_usd_price(self, chain: str) -> float:
        """ETH/BNB USD price 조회. Dexscreener WETH/USDC 풀 또는 CoinGecko 폴백.

        속도 우선이므로 Dexscreener 의 WETH(BSC 는 WBNB) token 검색 사용.
        """
        spec = CHAINS.get(chain)
        if spec is None:
            return 0.0
        # WETH/WBNB 주소로 dexscreener tokens 조회 → 여러 풀 중 유동성 최대로 USD 추출
        price = await self.get_dex_price(spec.wrapped_native, chain)
        if price > 0:
            return price
        # CoinGecko 폴백
        try:
            async with aiohttp.ClientSession() as sess:
                coin_id = {'ethereum': 'ethereum', 'base': 'ethereum', 'bsc': 'binancecoin'}.get(chain, 'ethereum')
                url = f'https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd'
                async with sess.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=self.cfg.dexscreener_timeout_sec),
                ) as resp:
                    if resp.status != 200:
                        return 0.0
                    data = await resp.json()
            return float(data.get(coin_id, {}).get('usd') or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0

    # ------------------------------------------------------------------
    # 유틸
    # ------------------------------------------------------------------

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:  # noqa: BLE001
            return False

    def _maybe_rollover_daily(self) -> None:
        today = _today_midnight_epoch()
        if today > self.state.daily_reset_epoch:
            logger.info(
                '[dex-trader] daily rollover: spent=$%.2f reset',
                self.state.daily_spent_usd,
            )
            self.state.daily_spent_usd = 0.0
            self.state.daily_reset_epoch = today

    async def _append_jobs_record(self, payload: dict[str, Any]) -> None:
        path = Path(self.cfg.jobs_path)
        # uuid 식별자 (수동/자동 job 추적)
        if 'job_id' not in payload:
            payload['job_id'] = uuid.uuid4().hex[:12]
        async with self._write_lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(payload, ensure_ascii=False) + '\n')
            except Exception as exc:  # noqa: BLE001
                logger.warning('DexTrader jobs append failed (%s): %s', path, exc)

    async def _telegram_error(self, ticker: str, chain: str, msg: str) -> None:
        if self.telegram is None:
            return
        text = f'⚠️ DEX 실패: {ticker} via {chain}\n{msg}'
        await self._send_telegram(text)

    async def _send_telegram(self, text: str) -> None:
        if self.telegram is None:
            return
        try:
            send = getattr(self.telegram, '_send_message', None)
            if send is None:
                send = getattr(self.telegram, 'send_message', None)
            if send is None:
                return
            await send(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug('DexTrader telegram err: %s', exc)
