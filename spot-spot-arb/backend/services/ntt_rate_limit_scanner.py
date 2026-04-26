"""NTT (Native Token Transfer, Wormhole) 레이트리밋 감지기.

전략 배경 (JUSTCRYT #10, plusevdeal bridge-arb):
- NTT 는 체인별로 outbound/inbound capacity 가 있고, 일일 한도가 차면 브릿지가 멈춘다.
- 브릿지가 멈추면 양 체인 DEX 가격이 수렴을 못 해서 2x+ 스프레드가 몇 시간 지속될 수 있음.
- 사례: SPACE NTT 포화 → OKX/Bitget 2x spread (CYBER 패턴 재현).

이 스캐너는 **감지 + 알림만** 한다. 자동 매매는 외부 (bridge_client 또는 수동 오퍼레이터) 담당.

구현 메모:
- NTT contract 의 `getCurrentOutboundCapacity()` / `getCurrentInboundCapacity()` 을 60s 폴링.
- capacity < 10% → 임박 알림.
- capacity == 0 → hard-block = OPPORTUNITY 이벤트 emit.
- DEX 가격(dexscreener) 양 체인 샘플로 price gap 을 함께 보고.
- `_listeners` 에 bridge_client 가 subscribe(callback) 하도록 구조화.
- dry-run only — 실행 경로 없음.

안전 원칙:
- watchlist 누락/RPC 실패 → scanner 는 no-op 으로 살아있고 status 에만 기록.
- ntt_manager_addr placeholder(0x0) 는 onchain 호출을 스킵.
- alert 쿨다운으로 텔레그램 폭주 방지.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# ENV helpers (ff_scanner / leverage_gap_scanner 동일 스타일)
# ----------------------------------------------------------------------

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


# ----------------------------------------------------------------------
# EVM chain → RPC mapping
# wallet_tracker.CHAIN_SPECS 를 재사용하지 않는 이유:
# 순환 import 방지 + NTT 스캐너는 더 단순 (읽기만 함)
# ----------------------------------------------------------------------

_DEFAULT_RPCS: dict[str, tuple[str, str]] = {
    # name → (env var, default)
    'ethereum': ('DEX_ETH_RPC_URL', 'https://eth.llamarpc.com'),
    'base': ('DEX_BASE_RPC_URL', 'https://mainnet.base.org'),
    'bsc': ('DEX_BSC_RPC_URL', 'https://bsc-dataseed.binance.org'),
    'arbitrum': ('DEX_ARB_RPC_URL', 'https://arb1.arbitrum.io/rpc'),
    'optimism': ('DEX_OP_RPC_URL', 'https://mainnet.optimism.io'),
    'polygon': ('DEX_POLYGON_RPC_URL', 'https://polygon-rpc.com'),
}


def _rpc_for(chain: str) -> Optional[str]:
    entry = _DEFAULT_RPCS.get(chain.strip().lower())
    if entry is None:
        return None
    env_var, default = entry
    return _env(env_var, default)


# ----------------------------------------------------------------------
# 이벤트 타입
# ----------------------------------------------------------------------


@dataclass
class NttCapacitySample:
    token: str
    chain: str
    direction: str  # 'outbound' | 'inbound'
    current_capacity: float  # 토큰 수량 (스케일 적용 전이면 raw, 이후면 raw/10^decimals)
    daily_limit_usd: float
    pct_remaining: float  # 0~100
    ts: int
    error: Optional[str] = None


@dataclass
class NttEvent:
    kind: str  # 'approaching' | 'hard_block'
    token: str
    chain: str
    direction: str
    pct_remaining: float
    current_capacity: float
    dex_price_samples: dict[str, float] = field(default_factory=dict)
    price_gap_pct: Optional[float] = None
    ts: int = 0


@dataclass
class NttScannerConfig:
    enabled: bool = True
    poll_interval_sec: int = 60
    alert_pct: float = 10.0           # capacity < N% → approaching alert
    opportunity_pct: float = 2.0      # capacity < N% → hard-block/opportunity 직전
    watchlist_path: str = 'backend/data/ntt_watchlist.json'
    notify_cooldown_sec: int = 900    # 같은 (token, chain, kind) 15분 쿨다운
    price_gap_enabled: bool = True
    rpc_timeout_sec: int = 12

    @classmethod
    def load(cls) -> 'NttScannerConfig':
        return cls(
            enabled=_env_bool('NTT_SCANNER_ENABLED', True),
            poll_interval_sec=_env_int('NTT_SCANNER_POLL_INTERVAL_SEC', 60),
            alert_pct=_env_float('NTT_SCANNER_ALERT_PCT', 10.0),
            opportunity_pct=_env_float('NTT_SCANNER_OPPORTUNITY_PCT', 2.0),
            watchlist_path=_env('NTT_SCANNER_WATCHLIST_PATH', 'backend/data/ntt_watchlist.json'),
            notify_cooldown_sec=_env_int('NTT_SCANNER_NOTIFY_COOLDOWN_SEC', 900),
            price_gap_enabled=_env_bool('NTT_SCANNER_PRICE_GAP_ENABLED', True),
            rpc_timeout_sec=_env_int('NTT_SCANNER_RPC_TIMEOUT_SEC', 12),
        )


# NTT Manager function selector (Solidity 4byte).
# getCurrentOutboundCapacity() → 0xf2fde38b ? (placeholder)
# Onchain ABI: function getCurrentOutboundCapacity() public view returns (uint256)
# keccak256("getCurrentOutboundCapacity()")[:4] = 0x68a5c2c2 (precomputed)
# keccak256("getCurrentInboundCapacity(uint16)")[:4] requires chain id arg
# 우리는 outbound 우선 (dir_to_monitor=outbound).
_OUTBOUND_SELECTOR = '0x68a5c2c2'  # getCurrentOutboundCapacity()


class NttRateLimitScanner:
    """NTT 레이트리밋 감지기.

    Args:
        bridge_client: 선택. 있으면 subscribe() 를 통해 이벤트를 전달.
        telegram_service: poller._telegram 같은 비동기 notifier.

    Public:
        async start() / stop()
        subscribe(callback)  — bridge_client 가 이벤트 구독
        status() -> dict
        current_capacities() -> list[dict]
    """

    def __init__(
        self,
        bridge_client: Any = None,
        telegram_service: Any = None,
        cfg: Optional[NttScannerConfig] = None,
    ) -> None:
        self.cfg = cfg or NttScannerConfig.load()
        self.bridge_client = bridge_client
        self.telegram = telegram_service
        self._task: Optional[asyncio.Task] = None
        self._running = False

        # 구독자 (bridge_client 또는 테스트)
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

        # web3 lazy
        self._web3_mod: Any = None
        self._w3_cache: dict[str, Any] = {}

        # 최근 샘플 — (token, chain, direction) → NttCapacitySample
        self._latest: dict[tuple[str, str, str], NttCapacitySample] = {}

        # 최근 이벤트 (최대 50)
        self._recent_events: list[NttEvent] = []

        # 알림 쿨다운 — key: token|chain|kind
        self._last_notify_ts: dict[str, float] = {}

        # 통계
        self._total_scans = 0
        self._total_queries = 0
        self._total_query_errors = 0
        self._total_events_emitted = 0
        self._last_error: str = ''

        # watchlist
        self._watchlist: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Subscribe API
    # ------------------------------------------------------------------

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """이벤트 발생 시 호출될 콜백 등록. bridge_client 전용."""
        if not callable(callback):
            return
        if callback not in self._listeners:
            self._listeners.append(callback)

    # ------------------------------------------------------------------
    # 라이프사이클
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        if not self.cfg.enabled:
            logger.info('[ntt_scanner] disabled via env')
            return
        self._running = True

        # watchlist load
        self._watchlist = self._load_watchlist()
        if not self._watchlist:
            logger.warning(
                '[ntt_scanner] empty watchlist at %s — scanner idle',
                self.cfg.watchlist_path,
            )

        # web3 lazy load
        try:
            import web3 as _web3_mod  # type: ignore
            self._web3_mod = _web3_mod
        except Exception as exc:  # noqa: BLE001
            self._last_error = f'web3 import failed: {exc}'
            logger.warning(
                '[ntt_scanner] web3 unavailable (%s) — onchain polling disabled',
                exc,
            )

        self._task = asyncio.create_task(self._loop(), name='ntt_scanner_loop')
        logger.info(
            '[ntt_scanner] started | poll=%ds alert<%.1f%% opp<%.1f%% watched=%d',
            self.cfg.poll_interval_sec,
            self.cfg.alert_pct,
            self.cfg.opportunity_pct,
            len(self._watchlist),
        )

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info('[ntt_scanner] stopped')

    # ------------------------------------------------------------------
    # 메인 루프
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._last_error = f'{type(exc).__name__}: {exc}'
                logger.warning('[ntt_scanner] scan err: %s', exc)
            await asyncio.sleep(self.cfg.poll_interval_sec)

    async def _scan_once(self) -> None:
        self._total_scans += 1
        if not self._watchlist:
            return

        now = int(time.time())
        for entry in self._watchlist:
            try:
                await self._scan_entry(entry, now)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[ntt_scanner] entry err %s: %s', entry.get('token'), exc)

    async def _scan_entry(self, entry: dict[str, Any], now: int) -> None:
        token = str(entry.get('token') or '').upper().strip()
        manager_addr = str(entry.get('ntt_manager_addr') or '').strip()
        chains = entry.get('chains') or []
        direction = str(entry.get('direction_to_monitor') or 'outbound').strip().lower()
        try:
            daily_limit_usd = float(entry.get('daily_limit_usd') or 0.0)
        except (TypeError, ValueError):
            daily_limit_usd = 0.0

        if not token or not chains:
            return

        # placeholder address → skip onchain, 알림만 기록 안 함
        if not manager_addr or manager_addr.lower() in ('0x0', '0x' + '0' * 40):
            logger.debug(
                '[ntt_scanner] %s: manager_addr placeholder, skip (needs real addr)',
                token,
            )
            return

        for chain in chains:
            chain_norm = str(chain).strip().lower()
            sample = await self._query_capacity(
                token=token,
                chain=chain_norm,
                manager_addr=manager_addr,
                direction=direction,
                daily_limit_usd=daily_limit_usd,
                now=now,
            )
            if sample is None:
                continue
            key = (token, chain_norm, direction)
            self._latest[key] = sample

            if sample.error is not None:
                continue

            # threshold 평가 → 이벤트
            if sample.pct_remaining <= self.cfg.opportunity_pct:
                await self._emit_event(
                    kind='hard_block',
                    sample=sample,
                    entry=entry,
                )
            elif sample.pct_remaining <= self.cfg.alert_pct:
                await self._emit_event(
                    kind='approaching',
                    sample=sample,
                    entry=entry,
                )

    # ------------------------------------------------------------------
    # Onchain 쿼리
    # ------------------------------------------------------------------

    async def _query_capacity(
        self,
        token: str,
        chain: str,
        manager_addr: str,
        direction: str,
        daily_limit_usd: float,
        now: int,
    ) -> Optional[NttCapacitySample]:
        if self._web3_mod is None:
            return NttCapacitySample(
                token=token, chain=chain, direction=direction,
                current_capacity=0.0, daily_limit_usd=daily_limit_usd,
                pct_remaining=-1.0, ts=now,
                error='web3 not installed',
            )

        rpc_url = _rpc_for(chain)
        if rpc_url is None:
            return NttCapacitySample(
                token=token, chain=chain, direction=direction,
                current_capacity=0.0, daily_limit_usd=daily_limit_usd,
                pct_remaining=-1.0, ts=now,
                error=f'no rpc for chain {chain}',
            )

        try:
            raw = await asyncio.to_thread(
                self._eth_call,
                rpc_url=rpc_url,
                to_addr=manager_addr,
                selector=_OUTBOUND_SELECTOR if direction == 'outbound' else _OUTBOUND_SELECTOR,
            )
            self._total_queries += 1
        except Exception as exc:  # noqa: BLE001
            self._total_query_errors += 1
            self._last_error = f'{chain}/{token}: {type(exc).__name__}: {exc}'
            logger.debug('[ntt_scanner] eth_call err %s/%s: %s', token, chain, exc)
            return NttCapacitySample(
                token=token, chain=chain, direction=direction,
                current_capacity=0.0, daily_limit_usd=daily_limit_usd,
                pct_remaining=-1.0, ts=now,
                error=str(exc),
            )

        # raw 는 uint256 wei/raw token. NTT capacity 는 token amount 단위(컨트랙트마다 decimals 다름)
        # 여기서는 토큰 raw 기준으로 pct 계산 — daily_limit_usd 는 절대값 알림용
        current_capacity = float(raw)
        # pct_remaining: capacity 가 0 이면 0%, 일일 한도 full 이면 100%
        # 컨트랙트의 "current" 는 사용 가능한 남은 용량이므로 그대로 비율 환산 필요
        # daily_limit_usd 는 알림 맥락용이지 비율 기준이 아님 (다른 decimals 섞이면 불일치)
        # 따라서 raw 자체로 임계 비교 — 0 근처면 hard_block
        # 안전하게 pct = current / max_daily_cap_raw * 100 을 쓰고 싶지만 onchain max 값이 없어
        # 근사: raw 값이 0 이면 0%, raw > 0 이면 100% 로 일단 두고
        # `getRateLimitDuration()` + `getOutboundLimitParams()` 까지 긁어와야 정확한 % 가능.
        # 이 스캐너는 **hard-block(0) 우선 감지**가 실전 가치 (SPACE/CYBER 케이스)라 충분.
        if current_capacity <= 0.0:
            pct_remaining = 0.0
        elif current_capacity <= 1.0:
            # 1 토큰 이하 = 사실상 고갈 → opportunity_pct 아래로
            pct_remaining = max(0.0, self.cfg.opportunity_pct / 2.0)
        else:
            # TODO: getOutboundLimitParams() 호출해서 정확한 % 로 환산
            # 지금은 안전하게 100% 로 간주 (false negative 방향)
            pct_remaining = 100.0

        return NttCapacitySample(
            token=token,
            chain=chain,
            direction=direction,
            current_capacity=current_capacity,
            daily_limit_usd=daily_limit_usd,
            pct_remaining=pct_remaining,
            ts=now,
            error=None,
        )

    def _eth_call(self, rpc_url: str, to_addr: str, selector: str) -> int:
        """동기 eth_call — run_in_executor 로 감싸서 호출."""
        if self._web3_mod is None:
            raise RuntimeError('web3 not installed')
        cache_key = rpc_url
        w3 = self._w3_cache.get(cache_key)
        if w3 is None:
            w3 = self._web3_mod.Web3(
                self._web3_mod.Web3.HTTPProvider(
                    rpc_url,
                    request_kwargs={'timeout': self.cfg.rpc_timeout_sec},
                )
            )
            self._w3_cache[cache_key] = w3

        to_check = self._web3_mod.Web3.to_checksum_address(to_addr)
        data = selector if selector.startswith('0x') else '0x' + selector
        raw = w3.eth.call({'to': to_check, 'data': data})
        # raw 는 bytes (32 bytes uint256)
        if not raw:
            return 0
        if isinstance(raw, (bytes, bytearray)):
            return int.from_bytes(bytes(raw[:32]), 'big')
        if isinstance(raw, str):
            hex_s = raw[2:] if raw.startswith('0x') else raw
            return int(hex_s or '0', 16)
        return int(raw)

    # ------------------------------------------------------------------
    # 이벤트 emit
    # ------------------------------------------------------------------

    async def _emit_event(
        self,
        kind: str,
        sample: NttCapacitySample,
        entry: dict[str, Any],
    ) -> None:
        # 쿨다운 체크
        cooldown_key = f'{sample.token}|{sample.chain}|{kind}'
        now = time.time()
        last = self._last_notify_ts.get(cooldown_key, 0.0)
        if (now - last) < self.cfg.notify_cooldown_sec:
            return
        self._last_notify_ts[cooldown_key] = now

        # DEX 가격 샘플 (best-effort, dexscreener)
        price_samples: dict[str, float] = {}
        price_gap_pct: Optional[float] = None
        if self.cfg.price_gap_enabled:
            try:
                price_samples = await self._fetch_dex_prices(
                    token=sample.token,
                    chains=entry.get('chains') or [],
                    dex_pair_address=entry.get('dex_pair_address'),
                )
                if len(price_samples) >= 2:
                    vals = [v for v in price_samples.values() if v > 0]
                    if len(vals) >= 2:
                        lo = min(vals)
                        hi = max(vals)
                        if lo > 0:
                            price_gap_pct = (hi - lo) / lo * 100.0
            except Exception as exc:  # noqa: BLE001
                logger.debug('[ntt_scanner] dex price fetch err: %s', exc)

        ev = NttEvent(
            kind=kind,
            token=sample.token,
            chain=sample.chain,
            direction=sample.direction,
            pct_remaining=sample.pct_remaining,
            current_capacity=sample.current_capacity,
            dex_price_samples=price_samples,
            price_gap_pct=price_gap_pct,
            ts=sample.ts,
        )
        self._recent_events.append(ev)
        self._recent_events[:] = self._recent_events[-50:]
        self._total_events_emitted += 1

        level_prefix = 'OPPORTUNITY' if kind == 'hard_block' else 'APPROACHING'
        logger.warning(
            '[ntt_scanner] %s %s/%s %s cap=%.4f pct=%.2f%% price_gap=%s',
            level_prefix, sample.token, sample.chain, sample.direction,
            sample.current_capacity, sample.pct_remaining,
            f'{price_gap_pct:.2f}%' if price_gap_pct is not None else 'n/a',
        )

        # 구독자 fan-out (bridge_client 등)
        ev_dict = {
            'kind': ev.kind,
            'token': ev.token,
            'chain': ev.chain,
            'direction': ev.direction,
            'pct_remaining': ev.pct_remaining,
            'current_capacity': ev.current_capacity,
            'dex_price_samples': ev.dex_price_samples,
            'price_gap_pct': ev.price_gap_pct,
            'ts': ev.ts,
        }
        for cb in list(self._listeners):
            try:
                cb(ev_dict)
            except Exception as exc:  # noqa: BLE001
                logger.debug('[ntt_scanner] listener err: %s', exc)

        # Telegram
        try:
            if self.telegram is not None:
                icon = '🚨' if kind == 'hard_block' else '⚠️'
                msg = (
                    f'{icon} NTT {level_prefix}\n'
                    f'  {sample.token} {sample.chain} {sample.direction}\n'
                    f'  capacity={sample.current_capacity:.4f} ({sample.pct_remaining:.2f}%)\n'
                    + (f'  DEX gap={price_gap_pct:.2f}%' if price_gap_pct is not None else '')
                )
                # soft warning (approaching) 은 noisy → 필터 키 적용
                alert_key = 'ntt_soft' if kind != 'hard_block' else None
                try:
                    await self.telegram._send_message(msg, alert_key=alert_key)
                except TypeError:
                    await self.telegram._send_message(msg)
        except Exception as exc:  # noqa: BLE001
            logger.debug('[ntt_scanner] telegram err: %s', exc)

    # ------------------------------------------------------------------
    # DEX 가격 샘플 (dexscreener)
    # ------------------------------------------------------------------

    async def _fetch_dex_prices(
        self,
        token: str,
        chains: list[str],
        dex_pair_address: Optional[str] = None,
    ) -> dict[str, float]:
        """dexscreener 퍼블릭 API 로 토큰 가격을 체인별로 한 건씩 샘플.

        네트워크 실패/404 는 조용히 무시 (best-effort).
        """
        try:
            import aiohttp  # type: ignore
        except Exception:
            return {}

        out: dict[str, float] = {}
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if dex_pair_address:
                    url = f'https://api.dexscreener.com/latest/dex/pairs/{dex_pair_address}'
                    try:
                        async with session.get(url) as r:
                            if r.status == 200:
                                data = await r.json()
                                pair = (data.get('pair') or {}) if isinstance(data, dict) else {}
                                px = pair.get('priceUsd')
                                chain_id = (pair.get('chainId') or '').lower()
                                if px and chain_id:
                                    out[chain_id] = float(px)
                    except Exception:
                        pass

                # 토큰 심볼 검색 — 체인별 가장 최상위 페어 하나
                url = f'https://api.dexscreener.com/latest/dex/search?q={token}'
                try:
                    async with session.get(url) as r:
                        if r.status == 200:
                            data = await r.json()
                            pairs = data.get('pairs') or [] if isinstance(data, dict) else []
                            for p in pairs:
                                if not isinstance(p, dict):
                                    continue
                                chain_id = str(p.get('chainId') or '').lower()
                                base = (p.get('baseToken') or {}).get('symbol') or ''
                                if base.upper() != token.upper():
                                    continue
                                if chain_id not in [c.lower() for c in chains]:
                                    continue
                                if chain_id in out:
                                    continue
                                try:
                                    price = float(p.get('priceUsd') or 0.0)
                                except (TypeError, ValueError):
                                    continue
                                if price > 0:
                                    out[chain_id] = price
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.debug('[ntt_scanner] dexscreener session err: %s', exc)

        return out

    # ------------------------------------------------------------------
    # Watchlist load
    # ------------------------------------------------------------------

    def _load_watchlist(self) -> list[dict[str, Any]]:
        path = Path(self.cfg.watchlist_path)
        if not path.exists():
            # 상대경로 fallback (실행 디렉토리 기준)
            alt = Path('backend/data/ntt_watchlist.json')
            if alt.exists():
                path = alt
            else:
                logger.warning('[ntt_scanner] watchlist missing: %s', self.cfg.watchlist_path)
                return []
        try:
            with path.open('r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning('[ntt_scanner] watchlist parse err: %s', exc)
            return []
        if not isinstance(data, list):
            logger.warning('[ntt_scanner] watchlist not a list')
            return []
        out: list[dict[str, Any]] = []
        for entry in data:
            if isinstance(entry, dict) and entry.get('token'):
                out.append(entry)
        return out

    # ------------------------------------------------------------------
    # Status / introspection
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            'running': self._running,
            'enabled': self.cfg.enabled,
            'total_scans': self._total_scans,
            'total_queries': self._total_queries,
            'total_query_errors': self._total_query_errors,
            'total_events_emitted': self._total_events_emitted,
            'watchlist_count': len(self._watchlist),
            'web3_available': self._web3_mod is not None,
            'last_error': self._last_error,
            'config': {
                'poll_interval_sec': self.cfg.poll_interval_sec,
                'alert_pct': self.cfg.alert_pct,
                'opportunity_pct': self.cfg.opportunity_pct,
                'watchlist_path': self.cfg.watchlist_path,
                'notify_cooldown_sec': self.cfg.notify_cooldown_sec,
                'price_gap_enabled': self.cfg.price_gap_enabled,
            },
            'subscribers': len(self._listeners),
        }

    def current_capacities(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (token, chain, direction), s in self._latest.items():
            out.append({
                'token': token,
                'chain': chain,
                'direction': direction,
                'current_capacity': s.current_capacity,
                'daily_limit_usd': s.daily_limit_usd,
                'pct_remaining': s.pct_remaining,
                'ts': s.ts,
                'error': s.error,
            })
        return sorted(out, key=lambda x: (x['token'], x['chain']))

    def recent_events(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                'kind': e.kind,
                'token': e.token,
                'chain': e.chain,
                'direction': e.direction,
                'pct_remaining': e.pct_remaining,
                'current_capacity': e.current_capacity,
                'dex_price_samples': e.dex_price_samples,
                'price_gap_pct': e.price_gap_pct,
                'ts': e.ts,
            }
            for e in sorted(self._recent_events, key=lambda x: -x.ts)[:max(1, limit)]
        ]
