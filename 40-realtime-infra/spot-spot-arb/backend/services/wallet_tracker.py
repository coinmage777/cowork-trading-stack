"""WalletTracker — 온체인 지갑 모니터링 & CEX 입금 감지 → 자동 숏/알럿.

배경 (Pika_Kim + pannpunch + plusevdeal 카탈로그):
  - ELSA -34%: GSR 마켓메이커가 CEX 로 10M 토큰 이동 → 온체인에서 선감지.
  - Aria 3차 덤프: 인사이더 4지갑 추적 → 합산 덤프 소진 → +80% 리버설 LONG.
  - DOT Hyperbridge / Drift JLP drain: 해커/팀 지갑 이상 움직임 감지 → 프론트런 숏.

설계 원칙 (DEX trader 동일 방어 패턴):
  - EVM 폴링 (get_block/get_logs). 체인 별 last_block 캐시 → 재스캔 방지.
  - tx_hash + wallet 기준 dedup (메모리).
  - RPC 실패 시 지수 백오프 (30s).
  - private key 는 hedge_service 가 관리. 여기는 서명/송신 없음.
  - 트리플 락 + kill switch 로 자동 숏 경로 잠금.
  - Telegram 알림은 예외 무시 (조용히 실패).
  - 모든 경로에서 asyncio task 새로 안 쌓기 (handler 는 단일 async 루프 내부).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiohttp  # type: ignore

logger = logging.getLogger(__name__)


# ======================================================================
# env 헬퍼 (dex_trader 동일 컨벤션)
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
# 체인 상수 — dex_trader CHAINS 와 rpc env 공유
# ======================================================================


@dataclass(frozen=True)
class _ChainSpec:
    name: str
    chain_id: int
    rpc_env: str
    rpc_default: str
    explorer_tx: str


CHAIN_SPECS: dict[str, _ChainSpec] = {
    'ethereum': _ChainSpec(
        name='ethereum', chain_id=1,
        rpc_env='DEX_ETH_RPC_URL',
        rpc_default='https://eth.llamarpc.com',
        explorer_tx='https://etherscan.io/tx/',
    ),
    'base': _ChainSpec(
        name='base', chain_id=8453,
        rpc_env='DEX_BASE_RPC_URL',
        rpc_default='https://mainnet.base.org',
        explorer_tx='https://basescan.org/tx/',
    ),
    'bsc': _ChainSpec(
        name='bsc', chain_id=56,
        rpc_env='DEX_BSC_RPC_URL',
        rpc_default='https://bsc-dataseed.binance.org',
        explorer_tx='https://bscscan.com/tx/',
    ),
    'arbitrum': _ChainSpec(
        name='arbitrum', chain_id=42161,
        rpc_env='DEX_ARB_RPC_URL',
        rpc_default='https://arb1.arbitrum.io/rpc',
        explorer_tx='https://arbiscan.io/tx/',
    ),
    'optimism': _ChainSpec(
        name='optimism', chain_id=10,
        rpc_env='DEX_OP_RPC_URL',
        rpc_default='https://mainnet.optimism.io',
        explorer_tx='https://optimistic.etherscan.io/tx/',
    ),
    'polygon': _ChainSpec(
        name='polygon', chain_id=137,
        rpc_env='DEX_POLYGON_RPC_URL',
        rpc_default='https://polygon-rpc.com',
        explorer_tx='https://polygonscan.com/tx/',
    ),
}


# ======================================================================
# CEX 핫월렛 레퍼런스 — 온체인 입금 목적지 판정용.
# (공개 라벨링된 Binance/Bybit/OKX/Bithumb/Upbit/Coinbase/Kraken 주소들 일부.)
# 사용자가 data/cex_hot_wallets.json 으로 확장/오버라이드 가능.
# ======================================================================

CEX_HOT_WALLETS: dict[str, str] = {
    # --- Binance ---
    '<EVM_ADDRESS>': 'Binance 14',
    '<EVM_ADDRESS>': 'Binance 15',
    '<EVM_ADDRESS>': 'Binance 16',
    '<EVM_ADDRESS>': 'Binance 17',
    '<EVM_ADDRESS>': 'Binance 18',
    '<EVM_ADDRESS>': 'Binance 19',
    '<EVM_ADDRESS>': 'Binance 7 (cold)',
    '<EVM_ADDRESS>': 'Binance 8 (cold)',
    # --- Bybit ---
    '<EVM_ADDRESS>': 'Bybit 1',
    '<EVM_ADDRESS>': 'Bybit 2',
    '<EVM_ADDRESS>': 'Bybit 3',
    # --- OKX ---
    '<EVM_ADDRESS>': 'OKX 1',
    '<EVM_ADDRESS>': 'OKX 2',
    '<EVM_ADDRESS>': 'OKX 3',
    '<EVM_ADDRESS>': 'OKX 4',
    # --- Bithumb (Korean CEX) ---
    '<EVM_ADDRESS>': 'Bithumb 1',
    '<EVM_ADDRESS>': 'Bithumb 2',
    '<EVM_ADDRESS>': 'Bithumb 3',
    '<EVM_ADDRESS>': 'Bithumb 4',
    # --- Upbit (Korean CEX) ---
    '<EVM_ADDRESS>': 'Upbit 1',
    '<EVM_ADDRESS>': 'Upbit 2',
    '<EVM_ADDRESS>': 'Upbit 3',
    # --- Coinbase ---
    '<EVM_ADDRESS>': 'Coinbase 1',
    '<EVM_ADDRESS>': 'Coinbase 2',
    '<EVM_ADDRESS>': 'Coinbase 3',
    # --- Kraken ---
    '<EVM_ADDRESS>': 'Kraken 1',
    '<EVM_ADDRESS>': 'Kraken 2',
    '<EVM_ADDRESS>': 'Kraken 3',
    # --- Kucoin ---
    '<EVM_ADDRESS>': 'Kucoin 1',
    '<EVM_ADDRESS>': 'Kucoin 4',
    # --- Gate ---
    '<EVM_ADDRESS>': 'Gate 1',
    # --- Bitget ---
    '<EVM_ADDRESS>': 'Bitget 1',
    '<EVM_ADDRESS>': 'Bitget 2',
}


# ======================================================================
# ERC20 Transfer 이벤트 ABI — from/to/value 파싱에만 사용
# ======================================================================

# keccak256("Transfer(address,address,uint256)")
_TRANSFER_TOPIC0 = '<PRIVATE_KEY>'


# ======================================================================
# 설정
# ======================================================================


@dataclass
class WalletTrackerConfig:
    enabled: bool = False
    dry_run: bool = True
    live_confirm: bool = False
    poll_interval_sec: int = 15
    block_lookback: int = 20
    hedge_notional_usd: float = 100.0
    hedge_leverage: int = 3
    kill_switch_file: str = 'data/KILL_WALLET'
    events_path: str = 'data/wallet_events.jsonl'
    watchlist_path: str = 'data/wallet_watchlist.json'
    cex_overrides_path: str = 'data/cex_hot_wallets.json'
    supported_chains: list[str] = field(
        default_factory=lambda: [
            'ethereum', 'base', 'bsc', 'arbitrum', 'optimism', 'polygon',
        ]
    )
    rpc_backoff_sec: int = 30
    # DEX pool 덤프 감지 — dex_dump_detector 액션
    dex_dump_drop_pct: float = 30.0
    max_event_cache: int = 1024

    @classmethod
    def load(cls) -> 'WalletTrackerConfig':
        return cls(
            enabled=_bool_env('WALLET_TRACKER_ENABLED', False),
            dry_run=_bool_env('WALLET_TRACKER_DRY_RUN', True),
            live_confirm=_bool_env('WALLET_TRACKER_LIVE_CONFIRM', False),
            poll_interval_sec=max(_int_env('WALLET_TRACKER_POLL_INTERVAL_SEC', 15), 5),
            block_lookback=max(_int_env('WALLET_TRACKER_BLOCK_LOOKBACK', 20), 1),
            hedge_notional_usd=max(_float_env('WALLET_HEDGE_NOTIONAL_USD', 100.0), 0.0),
            hedge_leverage=max(_int_env('WALLET_HEDGE_LEVERAGE', 3), 1),
            kill_switch_file=_str_env('WALLET_KILL_SWITCH_FILE', 'data/KILL_WALLET'),
            events_path=_str_env('WALLET_TRACKER_EVENTS_PATH', 'data/wallet_events.jsonl'),
            watchlist_path=_str_env('WALLET_TRACKER_WATCHLIST_PATH', 'data/wallet_watchlist.json'),
            cex_overrides_path=_str_env('WALLET_TRACKER_CEX_OVERRIDES', 'data/cex_hot_wallets.json'),
            supported_chains=_csv_env(
                'WALLET_TRACKER_CHAINS',
                ['ethereum', 'base', 'bsc', 'arbitrum', 'optimism', 'polygon'],
            ),
            rpc_backoff_sec=max(_int_env('WALLET_TRACKER_RPC_BACKOFF_SEC', 30), 5),
            dex_dump_drop_pct=max(_float_env('WALLET_TRACKER_DEX_DUMP_DROP_PCT', 30.0), 1.0),
            max_event_cache=max(_int_env('WALLET_TRACKER_EVENT_CACHE', 1024), 128),
        )


@dataclass
class _WalletState:
    last_block: dict[str, int] = field(default_factory=dict)         # chain -> last scanned block
    rpc_next_ok_ts: dict[str, float] = field(default_factory=dict)   # chain -> backoff resume ts
    seen_tx_keys: deque = field(default_factory=lambda: deque(maxlen=1024))
    seen_set: set = field(default_factory=set)
    events: deque = field(default_factory=lambda: deque(maxlen=200))  # ring buffer (in-memory)
    total_polls: int = 0
    total_detections: int = 0
    total_auto_shorts: int = 0
    total_dry_run: int = 0
    total_errors: int = 0
    last_error: str = ''
    last_poll_ts: float = 0.0
    last_detect_ts: float = 0.0


# ======================================================================
# WalletTracker
# ======================================================================


class WalletTracker:
    """온체인 지갑 모니터. 감시 주소 → CEX 핫월렛 송금 감지 → 자동 숏/알럿.

    사용:
        tracker = WalletTracker(
            dex_trader=dex_trader,
            hedge_service=hedge_trade_service,
            telegram_service=telegram,
        )
        await tracker.start()
        ...
        await tracker.stop()
    """

    def __init__(
        self,
        dex_trader: Any = None,
        hedge_service: Any = None,
        telegram_service: Any = None,
        cfg: Optional[WalletTrackerConfig] = None,
    ) -> None:
        self.dex_trader = dex_trader
        self.hedge = hedge_service
        self.telegram = telegram_service
        self.cfg = cfg or WalletTrackerConfig.load()
        self.state = _WalletState()
        # dedup ring buffer 사이즈 재조정
        self.state.seen_tx_keys = deque(maxlen=self.cfg.max_event_cache)
        self.state.events = deque(maxlen=max(200, self.cfg.max_event_cache // 4))

        self._running: bool = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._write_lock = asyncio.Lock()
        self._w3_cache: dict[str, Any] = {}
        self._web3_mod: Any = None
        self._watchlist: dict[str, dict[str, Any]] = {}
        self._cex_wallets: dict[str, str] = {}
        # per-wallet 최근 알림 ts (스팸 방지)
        self._last_fire_ts: dict[str, float] = {}
        self._fire_cooldown_sec: int = 300
        # dex pool baseline price (dex_dump_detector)
        self._dex_price_baseline: dict[str, float] = {}

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        try:
            Path(self.cfg.events_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning('WalletTracker events_path prep failed: %s', exc)

        # web3 lazy load — 실패해도 상태만 기록, 폴링 태스크는 no-op 으로 돌아감
        try:
            import web3 as _web3_mod                 # type: ignore
            self._web3_mod = _web3_mod
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                'WalletTracker: web3 unavailable (%s) — polling disabled',
                exc,
            )

        # watchlist + cex override 로드
        self._load_watchlist()
        self._load_cex_overrides()

        if not self.cfg.enabled:
            logger.info(
                'WalletTracker started (disabled via WALLET_TRACKER_ENABLED=false)',
            )
            return

        if self._web3_mod is None:
            logger.warning('WalletTracker: web3 missing — no polling task started')
            return

        # 체인별 폴링 태스크 기동
        for chain in self.cfg.supported_chains:
            chain_norm = chain.strip().lower()
            if chain_norm not in CHAIN_SPECS:
                logger.warning('[wallet-tracker] unsupported chain: %s', chain_norm)
                continue
            task = asyncio.create_task(
                self._poll_chain_loop(chain_norm),
                name=f'wallet_tracker_{chain_norm}',
            )
            self._tasks.append(task)

        logger.info(
            'WalletTracker started (chains=%s watchlist=%d cex=%d dry_run=%s live_confirm=%s)',
            ','.join(self.cfg.supported_chains),
            len(self._watchlist), len(self._cex_wallets),
            self.cfg.dry_run, self.cfg.live_confirm,
        )

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info('WalletTracker stopped')

    # ------------------------------------------------------------------
    # 상태 조회 + API
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and self.hedge is not None
        )
        return {
            'enabled': self.cfg.enabled,
            'dry_run': self.cfg.dry_run,
            'live_confirm': self.cfg.live_confirm,
            'live_armed': live_armed,
            'kill_switch_active': self._kill_switch_active(),
            'web3_available': self._web3_mod is not None,
            'supported_chains': list(self.cfg.supported_chains),
            'watchlist_size': len(self._watchlist),
            'cex_wallets_size': len(self._cex_wallets),
            'poll_interval_sec': self.cfg.poll_interval_sec,
            'block_lookback': self.cfg.block_lookback,
            'hedge_notional_usd': self.cfg.hedge_notional_usd,
            'hedge_leverage': self.cfg.hedge_leverage,
            'last_block': dict(self.state.last_block),
            'total_polls': self.state.total_polls,
            'total_detections': self.state.total_detections,
            'total_auto_shorts': self.state.total_auto_shorts,
            'total_dry_run': self.state.total_dry_run,
            'total_errors': self.state.total_errors,
            'last_error': self.state.last_error,
            'last_poll_ts': self.state.last_poll_ts,
            'last_detect_ts': self.state.last_detect_ts,
            'events_path': self.cfg.events_path,
            'watchlist_path': self.cfg.watchlist_path,
            'kill_switch_file': self.cfg.kill_switch_file,
        }

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        # 메모리 ring 우선 (빠름). 없으면 파일 tail.
        mem = list(self.state.events)
        if mem:
            return list(reversed(mem))[:limit]
        path = Path(self.cfg.events_path)
        if not path.exists():
            return []
        try:
            with path.open('r', encoding='utf-8') as f:
                lines = f.readlines()
        except Exception as exc:  # noqa: BLE001
            logger.debug('WalletTracker recent_events read failed: %s', exc)
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

    def watchlist(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._watchlist.items()}

    def add_watch(
        self,
        label: str,
        address: str,
        chains: list[str] | None = None,
        action_on_cex_deposit: str = 'alert',
        token_filter: list[str] | None = None,
    ) -> dict[str, Any]:
        label = str(label or '').strip()
        address = str(address or '').strip()
        if not label or not address or not address.startswith('0x') or len(address) != 42:
            return {'ok': False, 'code': 'INVALID_INPUT',
                    'message': 'label + 0x-prefixed 42 char address required'}
        chains_norm = [c.strip().lower() for c in (chains or []) if c.strip()]
        chains_norm = [c for c in chains_norm if c in CHAIN_SPECS]
        if not chains_norm:
            chains_norm = list(self.cfg.supported_chains)
        action = (action_on_cex_deposit or 'alert').strip().lower()
        if action not in {'alert', 'short_hedge', 'dex_dump_detector'}:
            return {'ok': False, 'code': 'INVALID_INPUT',
                    'message': 'action must be alert / short_hedge / dex_dump_detector'}
        entry: dict[str, Any] = {
            'address': address.lower(),
            'chains': chains_norm,
            'label': label,
            'action_on_cex_deposit': action,
        }
        if token_filter:
            entry['token_filter'] = [t.strip().upper() for t in token_filter if t]
        self._watchlist[label] = entry
        self._save_watchlist()
        return {'ok': True, 'label': label, 'entry': entry}

    def remove_watch(self, label: str) -> dict[str, Any]:
        label = str(label or '').strip()
        if label in self._watchlist:
            del self._watchlist[label]
            self._save_watchlist()
            return {'ok': True, 'label': label}
        return {'ok': False, 'code': 'NOT_FOUND', 'message': f'label {label!r} not in watchlist'}

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------

    def _kill_switch_active(self) -> bool:
        try:
            return Path(self.cfg.kill_switch_file).exists()
        except Exception:  # noqa: BLE001
            return False

    def _load_watchlist(self) -> None:
        path = Path(self.cfg.watchlist_path)
        if not path.exists():
            # scaffold default — mock 주소. 사용자가 수동 교체.
            default: dict[str, dict[str, Any]] = {
                'gsr_market_maker': {
                    'address': '<EVM_ADDRESS>',
                    'chains': ['ethereum', 'base'],
                    'label': 'GSR MM (placeholder)',
                    'action_on_cex_deposit': 'short_hedge',
                },
                'aria_insider_1': {
                    'address': '<EVM_ADDRESS>',
                    'chains': ['base'],
                    'label': 'Aria Insider 1 (placeholder)',
                    'token_filter': ['ARIA'],
                    'action_on_cex_deposit': 'dex_dump_detector',
                },
                'aria_insider_2': {
                    'address': '<EVM_ADDRESS>',
                    'chains': ['base'],
                    'label': 'Aria Insider 2 (placeholder)',
                    'token_filter': ['ARIA'],
                    'action_on_cex_deposit': 'alert',
                },
                'hyperbridge_exploiter': {
                    'address': '<EVM_ADDRESS>',
                    'chains': ['ethereum', 'arbitrum'],
                    'label': 'Hyperbridge Exploit Wallet (placeholder)',
                    'action_on_cex_deposit': 'short_hedge',
                },
                'drift_jlp_drainer': {
                    'address': '<EVM_ADDRESS>',
                    'chains': ['arbitrum'],
                    'label': 'Drift JLP Drainer (placeholder)',
                    'action_on_cex_deposit': 'alert',
                },
            }
            self._watchlist = default
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('w', encoding='utf-8') as f:
                    json.dump(default, f, indent=2, ensure_ascii=False)
                logger.info(
                    'WalletTracker: scaffolded default watchlist at %s (%d entries — replace placeholders)',
                    path, len(default),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning('WalletTracker watchlist scaffold write failed: %s', exc)
            return
        try:
            with path.open('r', encoding='utf-8') as f:
                data = json.load(f) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning('WalletTracker watchlist load failed: %s', exc)
            self._watchlist = {}
            return
        if not isinstance(data, dict):
            self._watchlist = {}
            return
        cleaned: dict[str, dict[str, Any]] = {}
        for key, entry in data.items():
            if not isinstance(entry, dict):
                continue
            addr = str(entry.get('address') or '').strip().lower()
            if not addr.startswith('0x') or len(addr) != 42:
                continue
            chains = entry.get('chains') or []
            chains_norm = [
                c.strip().lower() for c in chains
                if isinstance(c, str) and c.strip().lower() in CHAIN_SPECS
            ]
            if not chains_norm:
                chains_norm = list(self.cfg.supported_chains)
            cleaned[str(key)] = {
                'address': addr,
                'chains': chains_norm,
                'label': str(entry.get('label') or key),
                'action_on_cex_deposit': str(
                    entry.get('action_on_cex_deposit') or 'alert'
                ).strip().lower(),
                'token_filter': [
                    str(t).upper() for t in (entry.get('token_filter') or [])
                    if isinstance(t, str)
                ],
            }
        self._watchlist = cleaned

    def _save_watchlist(self) -> None:
        path = Path(self.cfg.watchlist_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('w', encoding='utf-8') as f:
                json.dump(self._watchlist, f, indent=2, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning('WalletTracker watchlist save failed: %s', exc)

    def _load_cex_overrides(self) -> None:
        self._cex_wallets = {k.lower(): v for k, v in CEX_HOT_WALLETS.items()}
        path = Path(self.cfg.cex_overrides_path)
        if not path.exists():
            return
        try:
            with path.open('r', encoding='utf-8') as f:
                data = json.load(f) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning('WalletTracker cex override load failed: %s', exc)
            return
        if isinstance(data, dict):
            for k, v in data.items():
                if not isinstance(k, str) or not k.startswith('0x'):
                    continue
                self._cex_wallets[k.lower()] = str(v or 'CEX')

    def _get_web3(self, chain: str) -> Any:
        chain_norm = chain.strip().lower()
        if chain_norm in self._w3_cache:
            return self._w3_cache[chain_norm]
        if self._web3_mod is None:
            raise RuntimeError('web3 not installed')
        spec = CHAIN_SPECS.get(chain_norm)
        if spec is None:
            raise ValueError(f'unsupported chain: {chain!r}')
        rpc_url = _str_env(spec.rpc_env, spec.rpc_default)
        w3 = self._web3_mod.Web3(
            self._web3_mod.Web3.HTTPProvider(
                rpc_url,
                request_kwargs={'timeout': 15},
            )
        )
        if not w3.is_connected():
            raise RuntimeError(f'{chain_norm} RPC unreachable: {rpc_url}')
        self._w3_cache[chain_norm] = w3
        return w3

    # ------------------------------------------------------------------
    # 폴링 루프 (체인별)
    # ------------------------------------------------------------------

    async def _poll_chain_loop(self, chain: str) -> None:
        logger.info('[wallet-tracker] %s poll loop start', chain)
        while self._running:
            try:
                # kill switch 는 실행 차단만, 폴링은 계속 (상태 유지)
                now = time.time()
                backoff_until = self.state.rpc_next_ok_ts.get(chain, 0.0)
                if now < backoff_until:
                    await asyncio.sleep(min(5.0, backoff_until - now))
                    continue

                await self._poll_chain_once(chain)
                self.state.total_polls += 1
                self.state.last_poll_ts = time.time()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.state.total_errors += 1
                self.state.last_error = f'{chain}: {type(exc).__name__}: {exc}'
                logger.warning(
                    '[wallet-tracker] %s poll error — backoff %ds: %s',
                    chain, self.cfg.rpc_backoff_sec, exc,
                )
                self.state.rpc_next_ok_ts[chain] = time.time() + self.cfg.rpc_backoff_sec

            try:
                await asyncio.sleep(self.cfg.poll_interval_sec)
            except asyncio.CancelledError:
                break
        logger.info('[wallet-tracker] %s poll loop end', chain)

    async def _poll_chain_once(self, chain: str) -> None:
        # 이 체인에서 감시하는 지갑 주소들 추출
        watched = {
            label: entry for label, entry in self._watchlist.items()
            if chain in (entry.get('chains') or [])
            and str(entry.get('address') or '').startswith('0x')
            # placeholder 00..00 / 00..01 주소는 스킵
            and int(entry['address'], 16) >= 16
        }
        if not watched:
            return

        # web3 호출 (동기) → 별도 쓰레드로
        try:
            w3 = await asyncio.to_thread(self._get_web3, chain)
        except Exception as exc:
            raise RuntimeError(f'web3 init failed: {exc}') from exc

        latest_block = await asyncio.to_thread(lambda: int(w3.eth.block_number))
        last_seen = self.state.last_block.get(chain, latest_block - self.cfg.block_lookback)
        # 첫 실행 — 과거 lookback 만큼만 스캔, 무한 백스캔 방지
        if last_seen <= 0 or (latest_block - last_seen) > self.cfg.block_lookback * 50:
            last_seen = max(0, latest_block - self.cfg.block_lookback)

        if latest_block <= last_seen:
            return

        from_block = last_seen + 1
        to_block = latest_block

        # 감시 주소 set (lowercase)
        watched_addrs = {entry['address'].lower() for entry in watched.values()}
        # label lookup by address
        label_by_addr: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for label, entry in watched.items():
            addr = entry['address'].lower()
            label_by_addr.setdefault(addr, []).append((label, entry))

        # ---- (1) Native (ETH/BNB) 전송 감지 — 블록 단위 스캔
        #       대량 체인에서 비싸지만 lookback 이 작으면 OK.
        try:
            await self._scan_native_transfers(
                chain=chain, w3=w3,
                from_block=from_block, to_block=to_block,
                watched_addrs=watched_addrs,
                label_by_addr=label_by_addr,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('[wallet-tracker] %s native scan err: %s', chain, exc)

        # ---- (2) ERC20 Transfer 로그 스캔 — topics[1] = from 주소 매칭
        try:
            await self._scan_erc20_transfers(
                chain=chain, w3=w3,
                from_block=from_block, to_block=to_block,
                watched_addrs=watched_addrs,
                label_by_addr=label_by_addr,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('[wallet-tracker] %s erc20 scan err: %s', chain, exc)

        self.state.last_block[chain] = to_block

    async def _scan_native_transfers(
        self,
        chain: str,
        w3: Any,
        from_block: int,
        to_block: int,
        watched_addrs: set[str],
        label_by_addr: dict[str, list[tuple[str, dict[str, Any]]]],
    ) -> None:
        # block by block — lookback 작으니 부담 적음.
        for bn in range(from_block, to_block + 1):
            try:
                block = await asyncio.to_thread(
                    lambda n=bn: w3.eth.get_block(n, full_transactions=True)
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug('[wallet-tracker] %s get_block(%d) fail: %s', chain, bn, exc)
                continue
            for tx in block.get('transactions', []):
                try:
                    sender = (tx.get('from') or '').lower()
                    recipient = (tx.get('to') or '').lower()
                except Exception:  # noqa: BLE001
                    continue
                if not sender or sender not in watched_addrs:
                    continue
                if not recipient or recipient not in self._cex_wallets:
                    continue
                tx_hash = tx.get('hash')
                tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
                dedup_key = f'{chain}:{tx_hash_hex}'
                if self._seen(dedup_key):
                    continue
                self._mark_seen(dedup_key)

                value_raw = tx.get('value') or 0
                try:
                    value_eth = float(w3.from_wei(int(value_raw), 'ether'))
                except Exception:  # noqa: BLE001
                    value_eth = 0.0

                native_symbol = 'ETH' if chain in {'ethereum', 'base', 'arbitrum', 'optimism'} \
                    else ('BNB' if chain == 'bsc' else 'MATIC' if chain == 'polygon' else 'NATIVE')

                for label, entry in label_by_addr.get(sender, []):
                    await self._handle_cex_deposit(
                        chain=chain,
                        label=label,
                        entry=entry,
                        tx_hash=tx_hash_hex,
                        from_addr=sender,
                        to_addr=recipient,
                        cex_label=self._cex_wallets.get(recipient, 'CEX'),
                        token='NATIVE',
                        token_symbol=native_symbol,
                        amount=value_eth,
                        block_number=bn,
                    )

    async def _scan_erc20_transfers(
        self,
        chain: str,
        w3: Any,
        from_block: int,
        to_block: int,
        watched_addrs: set[str],
        label_by_addr: dict[str, list[tuple[str, dict[str, Any]]]],
    ) -> None:
        # topic1 (from) 은 32byte left-padded 주소. 감시 주소 set 을 topic 형식으로 변환.
        topic1_filter: list[str] = []
        for addr in watched_addrs:
            try:
                padded = '0x' + addr.lower().replace('0x', '').rjust(64, '0')
                topic1_filter.append(padded)
            except Exception:  # noqa: BLE001
                continue
        if not topic1_filter:
            return
        # web3 get_logs — topics[0] = Transfer, topics[1] in watched addrs (from)
        flt = {
            'fromBlock': from_block,
            'toBlock': to_block,
            'topics': [
                _TRANSFER_TOPIC0,
                topic1_filter,
            ],
        }
        try:
            logs = await asyncio.to_thread(lambda: w3.eth.get_logs(flt))
        except Exception as exc:  # noqa: BLE001
            logger.debug('[wallet-tracker] %s get_logs err: %s', chain, exc)
            return
        for log in logs:
            try:
                topics = log.get('topics') or []
                if len(topics) < 3:
                    continue
                t1 = topics[1]
                t2 = topics[2]
                t1_hex = t1.hex() if hasattr(t1, 'hex') else str(t1)
                t2_hex = t2.hex() if hasattr(t2, 'hex') else str(t2)
                sender = '0x' + t1_hex[-40:]
                recipient = '0x' + t2_hex[-40:]
                sender = sender.lower()
                recipient = recipient.lower()
            except Exception:  # noqa: BLE001
                continue
            if sender not in watched_addrs:
                continue
            if recipient not in self._cex_wallets:
                continue
            tx_hash = log.get('transactionHash')
            tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
            log_index = log.get('logIndex', 0)
            try:
                log_index = int(log_index.hex(), 16) if hasattr(log_index, 'hex') else int(log_index)
            except Exception:  # noqa: BLE001
                log_index = 0
            dedup_key = f'{chain}:{tx_hash_hex}:{log_index}'
            if self._seen(dedup_key):
                continue
            self._mark_seen(dedup_key)

            token_addr = (log.get('address') or '').lower()
            data_raw = log.get('data') or '0x0'
            try:
                data_hex = data_raw.hex() if hasattr(data_raw, 'hex') else str(data_raw)
                amount_raw = int(data_hex, 16) if data_hex else 0
            except Exception:  # noqa: BLE001
                amount_raw = 0

            # decimals / symbol lookup (best effort — 실패 시 raw 사용)
            token_symbol, decimals = await self._lookup_erc20_meta(w3, token_addr)
            try:
                amount = float(amount_raw) / (10 ** decimals) if decimals > 0 else float(amount_raw)
            except Exception:  # noqa: BLE001
                amount = 0.0

            block_number = log.get('blockNumber', 0)
            try:
                block_number = int(block_number.hex(), 16) if hasattr(block_number, 'hex') \
                    else int(block_number)
            except Exception:  # noqa: BLE001
                block_number = 0

            for label, entry in label_by_addr.get(sender, []):
                # token_filter 가 있으면 symbol 매치 필수
                token_filter = entry.get('token_filter') or []
                if token_filter and token_symbol.upper() not in {t.upper() for t in token_filter}:
                    continue
                await self._handle_cex_deposit(
                    chain=chain,
                    label=label,
                    entry=entry,
                    tx_hash=tx_hash_hex,
                    from_addr=sender,
                    to_addr=recipient,
                    cex_label=self._cex_wallets.get(recipient, 'CEX'),
                    token=token_addr,
                    token_symbol=token_symbol,
                    amount=amount,
                    block_number=block_number,
                )

    async def _lookup_erc20_meta(self, w3: Any, token_addr: str) -> tuple[str, int]:
        """토큰 decimals + symbol 읽기. 실패 시 ('UNKNOWN', 18) 폴백."""
        if not token_addr or not token_addr.startswith('0x'):
            return 'UNKNOWN', 18
        try:
            cs = w3.to_checksum_address(token_addr)
        except Exception:  # noqa: BLE001
            return 'UNKNOWN', 18
        abi = [
            {'constant': True, 'inputs': [], 'name': 'decimals',
             'outputs': [{'name': '', 'type': 'uint8'}], 'type': 'function'},
            {'constant': True, 'inputs': [], 'name': 'symbol',
             'outputs': [{'name': '', 'type': 'string'}], 'type': 'function'},
        ]
        try:
            contract = w3.eth.contract(address=cs, abi=abi)
        except Exception:  # noqa: BLE001
            return 'UNKNOWN', 18
        symbol = 'UNKNOWN'
        decimals = 18
        try:
            symbol = await asyncio.to_thread(lambda: contract.functions.symbol().call())
        except Exception:  # noqa: BLE001
            pass
        try:
            decimals = int(await asyncio.to_thread(lambda: contract.functions.decimals().call()))
        except Exception:  # noqa: BLE001
            decimals = 18
        return str(symbol or 'UNKNOWN'), decimals

    # ------------------------------------------------------------------
    # CEX 입금 이벤트 핸들링 (알림 + 자동 액션)
    # ------------------------------------------------------------------

    async def _handle_cex_deposit(
        self,
        chain: str,
        label: str,
        entry: dict[str, Any],
        tx_hash: str,
        from_addr: str,
        to_addr: str,
        cex_label: str,
        token: str,
        token_symbol: str,
        amount: float,
        block_number: int,
    ) -> None:
        # fire 쿨다운 (같은 label 로 연속 발사 방지)
        now = time.time()
        last_fire = self._last_fire_ts.get(label, 0.0)
        if now - last_fire < self._fire_cooldown_sec:
            logger.debug(
                '[wallet-tracker] %s fire-cooldown active — skip %s tx=%s',
                label, cex_label, tx_hash,
            )
            return

        action = str(entry.get('action_on_cex_deposit') or 'alert').lower()
        display_label = str(entry.get('label') or label)
        spec = CHAIN_SPECS.get(chain)
        explorer = f'{spec.explorer_tx}{tx_hash}' if spec else tx_hash

        self.state.total_detections += 1
        self.state.last_detect_ts = now

        event: dict[str, Any] = {
            'ts': int(now),
            'event_type': 'wallet_cex_deposit',
            'chain': chain,
            'label_key': label,
            'label': display_label,
            'from': from_addr,
            'to': to_addr,
            'cex_label': cex_label,
            'token_address': token,
            'token_symbol': token_symbol,
            'amount': amount,
            'tx_hash': tx_hash,
            'explorer': explorer,
            'block_number': block_number,
            'action': action,
        }

        logger.info(
            '[wallet-tracker] DETECT %s → %s (%s): %s %.4f %s tx=%s',
            display_label, cex_label, chain, token_symbol, amount, chain, tx_hash,
        )

        # Telegram 알림 (best effort)
        try:
            await self._send_telegram(
                f'🚨 WALLET ALERT\n'
                f'{display_label} → {cex_label} ({chain})\n'
                f'amount: {amount:.4f} {token_symbol}\n'
                f'action: {action}\n'
                f'{explorer}'
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug('wallet-tracker telegram err: %s', exc)

        # 자동 액션 분기
        try:
            if action == 'short_hedge':
                await self._maybe_auto_short(event)
            elif action == 'dex_dump_detector':
                await self._maybe_dump_detect(event)
            else:
                # alert-only — Telegram 발송 외 작업 없음
                pass
        except Exception as exc:  # noqa: BLE001
            self.state.total_errors += 1
            self.state.last_error = f'action err: {type(exc).__name__}: {exc}'
            logger.warning('[wallet-tracker] action %s failed: %s', action, exc)

        # 파일/메모리 로깅
        await self._append_event(event)
        self._last_fire_ts[label] = now

    async def _maybe_auto_short(self, event: dict[str, Any]) -> None:
        """token_symbol 이 perp 으로 지원되면 자동 숏 오픈 (triple-lock)."""
        live_armed = (
            self.cfg.enabled
            and (not self.cfg.dry_run)
            and self.cfg.live_confirm
            and not self._kill_switch_active()
            and self.hedge is not None
        )
        symbol = str(event.get('token_symbol') or '').upper().strip()
        if not symbol or symbol in {'UNKNOWN', 'NATIVE'}:
            logger.info('[wallet-tracker] auto-short skip: no ticker symbol')
            return

        if not live_armed:
            self.state.total_dry_run += 1
            dry_rec = {
                **event,
                'auto_action': 'short_hedge',
                'mode': 'dry_run',
                'reason_not_live': self._dry_reason(),
            }
            logger.info(
                '[DRY-WALLET] would short %s $%.0f x%d via hedge_service (reason=%s)',
                symbol, self.cfg.hedge_notional_usd, self.cfg.hedge_leverage,
                dry_rec['reason_not_live'],
            )
            await self._append_event(dry_rec)
            return

        # 실제 주문 — hedge.enter() 재사용. futures_exchange 는 설정 하드코딩 대신
        # 첫 번째 지원 거래소 시도 (binance → bybit).
        attempts = _csv_env('WALLET_HEDGE_EXCHANGES', ['binance', 'bybit'])
        for fx in attempts:
            try:
                res = await self.hedge.enter(
                    ticker=symbol,
                    futures_exchange=fx,
                    nominal_usd=self.cfg.hedge_notional_usd,
                    leverage=self.cfg.hedge_leverage,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    '[wallet-tracker] auto-short %s %s err: %s',
                    symbol, fx, exc,
                )
                continue
            if isinstance(res, dict) and res.get('ok'):
                self.state.total_auto_shorts += 1
                exec_rec = {
                    **event,
                    'auto_action': 'short_hedge',
                    'mode': 'live',
                    'futures_exchange': fx,
                    'hedge_result': {
                        k: v for k, v in res.items()
                        if k in {'ok', 'code', 'message', 'job_id', 'ticker'}
                    },
                }
                await self._append_event(exec_rec)
                logger.info(
                    '[wallet-tracker] AUTO-SHORT %s on %s: ok=%s',
                    symbol, fx, res.get('ok'),
                )
                return
            else:
                logger.info(
                    '[wallet-tracker] auto-short %s on %s rejected: %s',
                    symbol, fx, (res or {}).get('code'),
                )
        # 모든 거래소 실패
        fail_rec = {
            **event,
            'auto_action': 'short_hedge',
            'mode': 'live_failed',
            'message': 'all futures exchanges rejected',
        }
        await self._append_event(fail_rec)

    async def _maybe_dump_detect(self, event: dict[str, Any]) -> None:
        """DEX 풀에서 토큰 가격 급락 감지 → LONG 시그널 (Aria 3차 리버설 패턴).

        dex_trader.get_dex_price 가 있으면 재사용. baseline 대비 N% 이상 하락 시 emit.
        직접 포지션 안 열고 event 에만 기록 — 수동 승인 LONG 용.
        """
        if self.dex_trader is None or not hasattr(self.dex_trader, 'get_dex_price'):
            logger.debug('[wallet-tracker] dex_dump_detector: dex_trader unavailable')
            return
        symbol = str(event.get('token_symbol') or '').upper()
        token_addr = event.get('token_address') or ''
        if not token_addr or token_addr in {'NATIVE', 'UNKNOWN'}:
            return
        try:
            price = await self.dex_trader.get_dex_price(token_addr, event.get('chain', ''))
        except Exception as exc:  # noqa: BLE001
            logger.debug('[wallet-tracker] dex_dump_detector price err: %s', exc)
            return
        if price <= 0:
            return
        baseline_key = f'{event.get("chain")}:{token_addr}'
        baseline = self._dex_price_baseline.get(baseline_key)
        if baseline is None or baseline <= 0:
            self._dex_price_baseline[baseline_key] = price
            return
        drop_pct = (baseline - price) / baseline * 100.0
        if drop_pct >= self.cfg.dex_dump_drop_pct:
            signal_rec = {
                **event,
                'auto_action': 'dex_dump_detector',
                'dex_signal': 'bottom_long_candidate',
                'baseline_price': baseline,
                'current_price': price,
                'drop_pct': drop_pct,
            }
            logger.info(
                '[wallet-tracker] DUMP BOTTOM %s: baseline=%.6f cur=%.6f drop=%.1f%% → LONG candidate',
                symbol, baseline, price, drop_pct,
            )
            try:
                await self._send_telegram(
                    f'📉➡️📈 DUMP BOTTOM {symbol}\n'
                    f'baseline ${baseline:.6f} → ${price:.6f} ({drop_pct:.1f}%)\n'
                    f'LONG candidate — review manually',
                    alert_key='dump_bottom',
                )
            except Exception:  # noqa: BLE001
                pass
            await self._append_event(signal_rec)
            # baseline 재설정 (중복 시그널 방지)
            self._dex_price_baseline[baseline_key] = price
        else:
            # baseline 을 서서히 갱신 (EMA 비슷하게 — 최대가 기준)
            if price > baseline:
                self._dex_price_baseline[baseline_key] = price

    # ------------------------------------------------------------------
    # Telegram + persistence helpers
    # ------------------------------------------------------------------

    async def _send_telegram(self, text: str, alert_key: str | None = None) -> None:
        if self.telegram is None:
            return
        # TelegramAlertService 는 _send_message(text, alert_key=...) 를 지원.
        for method_name in ('_send_message', 'send_message', 'send_text', 'notify'):
            method = getattr(self.telegram, method_name, None)
            if method is None:
                continue
            try:
                try:
                    res = method(text, alert_key=alert_key)
                except TypeError:
                    res = method(text)
                if asyncio.iscoroutine(res):
                    await res
                return
            except Exception as exc:  # noqa: BLE001
                logger.debug('telegram.%s err: %s', method_name, exc)
                continue

    async def _append_event(self, rec: dict[str, Any]) -> None:
        # 메모리 ring
        self.state.events.append(rec)
        # 파일 append — write_lock
        async with self._write_lock:
            try:
                path = Path(self.cfg.events_path)
                path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(rec, ensure_ascii=False) + '\n'
                with path.open('a', encoding='utf-8') as f:
                    f.write(line)
            except Exception as exc:  # noqa: BLE001
                logger.debug('wallet-tracker append event err: %s', exc)

    def _seen(self, key: str) -> bool:
        return key in self.state.seen_set

    def _mark_seen(self, key: str) -> None:
        if key in self.state.seen_set:
            return
        # deque 가 maxlen 에 도달해 왼쪽을 버리면 set 도 동기화
        if len(self.state.seen_tx_keys) >= (self.state.seen_tx_keys.maxlen or 1024):
            try:
                left = self.state.seen_tx_keys[0]
                self.state.seen_set.discard(left)
            except Exception:  # noqa: BLE001
                pass
        self.state.seen_tx_keys.append(key)
        self.state.seen_set.add(key)

    def _dry_reason(self) -> str:
        reasons = []
        if not self.cfg.enabled:
            reasons.append('WALLET_TRACKER_ENABLED=false')
        if self.cfg.dry_run:
            reasons.append('WALLET_TRACKER_DRY_RUN=true')
        if not self.cfg.live_confirm:
            reasons.append('WALLET_TRACKER_LIVE_CONFIRM=false')
        if self._kill_switch_active():
            reasons.append(f'kill switch {self.cfg.kill_switch_file} present')
        if self.hedge is None:
            reasons.append('hedge_service is None')
        return ','.join(reasons) if reasons else 'unknown'
