"""HYPER (Hyperlane) cross-venue listing arbitrage scanner.

Leo Telegram 스크린샷의 "PRICE / SIZE / SLIP / GAP" 4컬럼 스캐너 재현.
신규 상장 토큰의 venue 간 가격 격차를 실시간 감시하고 임계값 초과 시 Telegram 알림.

설계 원칙:
    - read-only: 자동 거래 없음, 알림만
    - rate-limit safe: ccxt enableRateLimit + poll_interval >= 5s
    - 모든 페어 조합 (NxN/2) gap 계산, top-K 보고
    - 슬리피지 계산: 오더북 기반 VWAP (size_usd notional 가정)
    - dedup: 같은 venue 페어 N분 내 중복 알림 방지
    - graceful shutdown: SIGINT/SIGTERM 처리

데이터 흐름:
    1. ccxt.async_support 로 venue 별 fetch_order_book(symbol, limit=20)
    2. asyncio.gather 로 동시 fetch (단일 round trip)
    3. bid_vwap / ask_vwap 계산 (size_usd 만큼 체결 가정)
    4. 모든 페어 (buy_at_X, sell_at_Y) gap_pct 계산
       gap_pct = (Y_bid_vwap - X_ask_vwap) / X_ask_vwap * 100
    5. gap_pct > info_threshold: jsonl log
       gap_pct > warn_threshold: Telegram WARNING
       gap_pct > crit_threshold: Telegram CRITICAL (즉시)

CLI:
    python -m strategies_minara.hyper_listing_arb_scanner \\
        --token HYPER --quote USDT --size-usd 500 --interval 10 --duration 300

환경변수 (.env):
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

리스크 / 알려진 한계:
    - 신규 상장 토큰은 출금 비활성 흔함 → 시그널 ≠ 실행 가능
    - REST polling 10초 → tick-level alpha 못 잡음, 추후 WS 업그레이드
    - 수수료/슬리피지 가산 시 gap 1~2%는 노이즈
    - bingx 는 HYPER 미상장 (감시 대상 자동 제외)

GPT-5 Codex + Devin 리뷰 대비:
    - decimal precision: gap 계산을 float 로 일관 (CEX ticker 정밀도 한계)
    - rate limit: enableRateLimit + 인스턴스 close, gather return_exceptions
    - 빈 오더북: bids[] / asks[] 빈 배열 가드
    - timezone: 모든 타임스탬프 UTC ISO8601
    - dedup state: 메모리 dict, 프로세스 재시작 시 초기화 (의도된 동작)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
import ccxt.async_support as ccxt

# ----------------------------------------------------------------------
# .env 로더 (Windows 한글 경로 호환)
# ----------------------------------------------------------------------

def _load_dotenv() -> None:
    """루트 .env 를 읽어 os.environ 에 주입. python-dotenv 없이 동작."""
    here = Path(__file__).resolve().parent
    for cand in (here.parent / '.env', here / '.env'):
        if not cand.exists():
            continue
        try:
            with cand.open('r', encoding='utf-8') as fh:
                for line in fh:
                    s = line.strip()
                    if not s or s.startswith('#') or '=' not in s:
                        continue
                    k, v = s.split('=', 1)
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception as exc:  # pragma: no cover
            logging.warning('failed to load %s: %s', cand, exc)


_load_dotenv()


# ----------------------------------------------------------------------
# 로깅
# ----------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)
DATA_DIR = Path(__file__).resolve().parent / 'data'
DATA_DIR.mkdir(exist_ok=True)

LOG_FILE = LOG_DIR / 'hyper_arb_scanner.log'
JSONL_FILE = DATA_DIR / 'hyper_arb_signals.jsonl'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger('hyper_arb')


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------

class TelegramNotifier:
    """경량 Telegram 알림. token/chat_id 미설정이면 no-op."""

    def __init__(self) -> None:
        self.token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        self.chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
        self.enabled = bool(self.token and self.chat_id)
        self._session: aiohttp.ClientSession | None = None
        if not self.enabled:
            logger.warning('Telegram disabled (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정)')

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        try:
            session = await self._get_session()
            url = f'https://api.telegram.org/bot{self.token}/sendMessage'
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True,
            }
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning('telegram %s: %s', resp.status, body[:200])
                    return False
                return True
        except Exception as exc:
            logger.warning('telegram send err: %s', exc)
            return False

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ----------------------------------------------------------------------
# 데이터 모델
# ----------------------------------------------------------------------

@dataclass
class VenueQuote:
    venue: str
    symbol: str
    bid: float | None
    ask: float | None
    bid_vwap: float | None
    ask_vwap: float | None
    bid_size_filled: float
    ask_size_filled: float
    bid_slippage_pct: float | None
    ask_slippage_pct: float | None
    timestamp: float
    error: str | None = None


@dataclass
class GapSignal:
    buy_venue: str          # 싸게 사는 곳
    sell_venue: str         # 비싸게 파는 곳
    buy_ask: float          # 매수가 (taker)
    sell_bid: float         # 매도가 (taker)
    gap_pct: float          # 단순 가격 갭
    gap_pct_vwap: float     # VWAP 기반 (size_usd notional)
    size_usd: float
    timestamp: float
    severity: str           # info / warn / crit


# ----------------------------------------------------------------------
# Venue probe
# ----------------------------------------------------------------------

DEFAULT_VENUES = ['binance', 'bybit', 'bitget', 'gate', 'mexc', 'kucoin']


async def fetch_quote(
    ex: ccxt.Exchange,
    venue: str,
    symbol: str,
    size_usd: float,
) -> VenueQuote:
    """오더북 기반 호가 + VWAP 계산.

    bid_vwap: size_usd 어치 매도 시 평균 체결가 (bid 위에서부터 소화)
    ask_vwap: size_usd 어치 매수 시 평균 체결가 (ask 아래에서부터 소화)
    슬리피지 = (vwap - top_of_book) / top_of_book * 100
    """
    ts = time.time()
    try:
        ob = await ex.fetch_order_book(symbol, limit=20)
    except Exception as exc:
        return VenueQuote(
            venue=venue, symbol=symbol,
            bid=None, ask=None,
            bid_vwap=None, ask_vwap=None,
            bid_size_filled=0.0, ask_size_filled=0.0,
            bid_slippage_pct=None, ask_slippage_pct=None,
            timestamp=ts, error=f'{type(exc).__name__}: {str(exc)[:80]}',
        )

    bids: list[list[float]] = ob.get('bids') or []
    asks: list[list[float]] = ob.get('asks') or []
    if not bids or not asks:
        return VenueQuote(
            venue=venue, symbol=symbol,
            bid=None, ask=None,
            bid_vwap=None, ask_vwap=None,
            bid_size_filled=0.0, ask_size_filled=0.0,
            bid_slippage_pct=None, ask_slippage_pct=None,
            timestamp=ts, error='empty orderbook',
        )

    top_bid = float(bids[0][0])
    top_ask = float(asks[0][0])

    bid_vwap, bid_size_filled = _vwap(bids, size_usd)
    ask_vwap, ask_size_filled = _vwap(asks, size_usd)

    bid_slip = ((top_bid - bid_vwap) / top_bid * 100) if (bid_vwap and top_bid) else None
    ask_slip = ((ask_vwap - top_ask) / top_ask * 100) if (ask_vwap and top_ask) else None

    return VenueQuote(
        venue=venue, symbol=symbol,
        bid=top_bid, ask=top_ask,
        bid_vwap=bid_vwap, ask_vwap=ask_vwap,
        bid_size_filled=bid_size_filled, ask_size_filled=ask_size_filled,
        bid_slippage_pct=bid_slip, ask_slippage_pct=ask_slip,
        timestamp=ts,
    )


def _vwap(levels: list[list[float]], size_usd: float) -> tuple[float | None, float]:
    """levels (price, qty) 를 size_usd 만큼 소화한 VWAP + 실제 체결 USD."""
    if not levels or size_usd <= 0:
        return None, 0.0
    remaining = size_usd
    cost_usd = 0.0
    qty_total = 0.0
    for entry in levels:
        if len(entry) < 2:
            continue
        try:
            px = float(entry[0])
            qty = float(entry[1])
        except (TypeError, ValueError):
            continue
        if px <= 0 or qty <= 0:
            continue
        notional = px * qty
        take_usd = min(notional, remaining)
        take_qty = take_usd / px
        cost_usd += take_usd
        qty_total += take_qty
        remaining -= take_usd
        if remaining <= 0:
            break
    if qty_total <= 0:
        return None, 0.0
    if remaining > 0:
        # 호가 부족 — 부분 체결 VWAP 반환 (filled 표시로 구분)
        pass
    return cost_usd / qty_total, cost_usd


# ----------------------------------------------------------------------
# Scanner core
# ----------------------------------------------------------------------

class HyperArbScanner:
    def __init__(
        self,
        token: str = 'HYPER',
        quote: str = 'USDT',
        venues: list[str] | None = None,
        size_usd: float = 500.0,
        interval: float = 10.0,
        info_threshold_pct: float = 1.0,
        warn_threshold_pct: float = 3.0,
        crit_threshold_pct: float = 7.0,
        dedup_minutes: float = 30.0,
        top_k: int = 5,
    ) -> None:
        self.token = token.upper()
        self.quote = quote.upper()
        self.symbol = f'{self.token}/{self.quote}'
        self.venues = venues or DEFAULT_VENUES
        self.size_usd = size_usd
        self.interval = max(5.0, float(interval))
        self.info_thr = info_threshold_pct
        self.warn_thr = warn_threshold_pct
        self.crit_thr = crit_threshold_pct
        self.dedup_seconds = dedup_minutes * 60
        self.top_k = top_k

        self._exchanges: dict[str, ccxt.Exchange] = {}
        self._supported_venues: list[str] = []
        self._dedup: dict[str, float] = {}
        self._notifier = TelegramNotifier()
        self._stop = asyncio.Event()
        self._tick_count = 0
        self._signal_count = 0

    async def setup(self) -> None:
        """ccxt 인스턴스 생성 + symbol 지원 venue 필터링."""
        for venue in self.venues:
            try:
                cls = getattr(ccxt, venue)
            except AttributeError:
                logger.warning('ccxt has no %s, skipping', venue)
                continue
            inst = cls({'enableRateLimit': True})
            try:
                await inst.load_markets()
                if self.symbol in inst.markets:
                    self._exchanges[venue] = inst
                    self._supported_venues.append(venue)
                    logger.info('venue OK: %s -> %s', venue, self.symbol)
                else:
                    logger.info('venue skip: %s (%s not listed)', venue, self.symbol)
                    await inst.close()
            except Exception as exc:
                logger.warning('venue %s init err: %s', venue, exc)
                try:
                    await inst.close()
                except Exception:
                    pass

        if len(self._supported_venues) < 2:
            raise RuntimeError(
                f'Need >=2 venues with {self.symbol}, got {self._supported_venues}'
            )

        await self._notifier.send(
            f'<b>HYPER Arb Scanner START</b>\n'
            f'symbol: <code>{self.symbol}</code>\n'
            f'venues: {", ".join(self._supported_venues)}\n'
            f'size: ${self.size_usd:.0f} | interval: {self.interval}s\n'
            f'thresholds: info {self.info_thr}% / warn {self.warn_thr}% / crit {self.crit_thr}%'
        )

    async def teardown(self) -> None:
        for venue, ex in self._exchanges.items():
            try:
                await ex.close()
            except Exception as exc:
                logger.debug('close %s err: %s', venue, exc)
        await self._notifier.close()

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self, max_duration_sec: float | None = None) -> None:
        start = time.time()
        try:
            while not self._stop.is_set():
                tick_start = time.time()
                await self._tick()
                self._tick_count += 1
                if max_duration_sec is not None and (time.time() - start) >= max_duration_sec:
                    logger.info('max duration reached, stopping')
                    break
                elapsed = time.time() - tick_start
                sleep_for = max(0.5, self.interval - elapsed)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
                    break
                except asyncio.TimeoutError:
                    continue
        finally:
            await self._notifier.send(
                f'<b>HYPER Arb Scanner STOP</b>\n'
                f'ticks: {self._tick_count} | signals: {self._signal_count}\n'
                f'elapsed: {(time.time()-start)/60:.1f}min'
            )

    async def _tick(self) -> None:
        coros = [
            fetch_quote(self._exchanges[v], v, self.symbol, self.size_usd)
            for v in self._supported_venues
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        quotes: list[VenueQuote] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning('gather exc: %s', r)
                continue
            if r.error:
                logger.info('quote err %s: %s', r.venue, r.error)
                continue
            quotes.append(r)

        if len(quotes) < 2:
            logger.warning('insufficient quotes (%d)', len(quotes))
            return

        # 콘솔 4컬럼 스냅샷 (Leo 스크린샷 양식)
        self._print_snapshot(quotes)

        signals = self._compute_gaps(quotes)
        if not signals:
            return

        # severity desc 정렬
        sev_order = {'crit': 0, 'warn': 1, 'info': 2}
        signals.sort(key=lambda s: (sev_order.get(s.severity, 3), -s.gap_pct))

        for sig in signals[: self.top_k]:
            self._log_signal(sig)
            if sig.severity in ('warn', 'crit'):
                if not self._dedup_check(sig):
                    continue
                await self._notify(sig)
                self._signal_count += 1

    def _print_snapshot(self, quotes: list[VenueQuote]) -> None:
        print(f'\n=== {datetime.now(timezone.utc).strftime("%H:%M:%S")} UTC | {self.symbol} | size=${self.size_usd:.0f} ===')
        print(f'{"VENUE":<10} {"BID":<10} {"ASK":<10} {"BID_VWAP":<10} {"ASK_VWAP":<10} {"BID_SLIP":<8} {"ASK_SLIP":<8} {"FILL_USD":<10}')
        for q in quotes:
            bid_str = f'{q.bid:.6f}' if q.bid else '--'
            ask_str = f'{q.ask:.6f}' if q.ask else '--'
            bvw = f'{q.bid_vwap:.6f}' if q.bid_vwap else '--'
            avw = f'{q.ask_vwap:.6f}' if q.ask_vwap else '--'
            bsl = f'{q.bid_slippage_pct:.2f}%' if q.bid_slippage_pct is not None else '--'
            asl = f'{q.ask_slippage_pct:.2f}%' if q.ask_slippage_pct is not None else '--'
            fill = f'${q.bid_size_filled:.0f}/{q.ask_size_filled:.0f}'
            print(f'{q.venue:<10} {bid_str:<10} {ask_str:<10} {bvw:<10} {avw:<10} {bsl:<8} {asl:<8} {fill:<10}')

    def _compute_gaps(self, quotes: list[VenueQuote]) -> list[GapSignal]:
        out: list[GapSignal] = []
        ts = time.time()
        for buy in quotes:
            for sell in quotes:
                if buy.venue == sell.venue:
                    continue
                if not (buy.ask and sell.bid and buy.ask_vwap and sell.bid_vwap):
                    continue
                # 단순 갭: top of book
                gap_pct = (sell.bid - buy.ask) / buy.ask * 100
                # VWAP 갭: size_usd notional 가정
                gap_vwap = (sell.bid_vwap - buy.ask_vwap) / buy.ask_vwap * 100
                # 음수/0 은 의미 없음
                if gap_vwap <= 0 and gap_pct <= 0:
                    continue
                if gap_vwap < self.info_thr:
                    continue
                # 부분 체결인 경우 gap 깎기 (호가 부족 시 신뢰도 ↓)
                if buy.ask_size_filled < self.size_usd * 0.5 or sell.bid_size_filled < self.size_usd * 0.5:
                    # 표시는 하지만 severity 강등
                    severity = 'info'
                else:
                    if gap_vwap >= self.crit_thr:
                        severity = 'crit'
                    elif gap_vwap >= self.warn_thr:
                        severity = 'warn'
                    else:
                        severity = 'info'
                out.append(GapSignal(
                    buy_venue=buy.venue, sell_venue=sell.venue,
                    buy_ask=buy.ask, sell_bid=sell.bid,
                    gap_pct=gap_pct, gap_pct_vwap=gap_vwap,
                    size_usd=self.size_usd, timestamp=ts, severity=severity,
                ))
        return out

    def _log_signal(self, sig: GapSignal) -> None:
        rec = {
            'ts': datetime.fromtimestamp(sig.timestamp, tz=timezone.utc).isoformat(),
            'symbol': self.symbol,
            'buy_venue': sig.buy_venue,
            'sell_venue': sig.sell_venue,
            'buy_ask': sig.buy_ask,
            'sell_bid': sig.sell_bid,
            'gap_pct': round(sig.gap_pct, 4),
            'gap_pct_vwap': round(sig.gap_pct_vwap, 4),
            'size_usd': sig.size_usd,
            'severity': sig.severity,
        }
        try:
            with JSONL_FILE.open('a', encoding='utf-8') as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
        except Exception as exc:
            logger.warning('jsonl write err: %s', exc)
        logger.info(
            '[%s] BUY %s @ %.6f -> SELL %s @ %.6f | gap=%.2f%% (vwap %.2f%%)',
            sig.severity.upper(), sig.buy_venue, sig.buy_ask,
            sig.sell_venue, sig.sell_bid, sig.gap_pct, sig.gap_pct_vwap,
        )

    def _dedup_check(self, sig: GapSignal) -> bool:
        key = f'{sig.buy_venue}->{sig.sell_venue}:{sig.severity}'
        last = self._dedup.get(key, 0.0)
        now = time.time()
        if now - last < self.dedup_seconds:
            return False
        self._dedup[key] = now
        return True

    async def _notify(self, sig: GapSignal) -> None:
        emoji = '[CRIT]' if sig.severity == 'crit' else '[WARN]'
        text = (
            f'<b>{emoji} {self.symbol} Cross-Venue Gap</b>\n'
            f'BUY  <code>{sig.buy_venue}</code> @ <code>{sig.buy_ask:.6f}</code>\n'
            f'SELL <code>{sig.sell_venue}</code> @ <code>{sig.sell_bid:.6f}</code>\n'
            f'gap (top): <b>{sig.gap_pct:.2f}%</b>\n'
            f'gap (vwap ${sig.size_usd:.0f}): <b>{sig.gap_pct_vwap:.2f}%</b>\n'
            f'<i>read-only signal — verify withdraw status before execution</i>'
        )
        await self._notifier.send(text)


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description='HYPER cross-venue arbitrage scanner')
    p.add_argument('--token', default='HYPER')
    p.add_argument('--quote', default='USDT')
    p.add_argument('--venues', default=','.join(DEFAULT_VENUES),
                   help='comma-separated ccxt venue ids')
    p.add_argument('--size-usd', type=float, default=500.0)
    p.add_argument('--interval', type=float, default=10.0)
    p.add_argument('--info', type=float, default=1.0, dest='info_thr')
    p.add_argument('--warn', type=float, default=3.0, dest='warn_thr')
    p.add_argument('--crit', type=float, default=7.0, dest='crit_thr')
    p.add_argument('--dedup-minutes', type=float, default=30.0)
    p.add_argument('--duration', type=float, default=0.0,
                   help='max runtime in seconds (0 = until SIGINT)')
    p.add_argument('--top-k', type=int, default=5)
    return p.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    venues = [v.strip() for v in args.venues.split(',') if v.strip()]
    scanner = HyperArbScanner(
        token=args.token, quote=args.quote, venues=venues,
        size_usd=args.size_usd, interval=args.interval,
        info_threshold_pct=args.info_thr,
        warn_threshold_pct=args.warn_thr,
        crit_threshold_pct=args.crit_thr,
        dedup_minutes=args.dedup_minutes,
        top_k=args.top_k,
    )

    loop = asyncio.get_running_loop()
    # Windows: SIGTERM 미지원, SIGINT 만 처리
    for sig_name in ('SIGINT', 'SIGTERM'):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, scanner.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows ProactorEventLoop 는 SIGTERM add_signal_handler 미지원
            pass

    try:
        await scanner.setup()
    except Exception as exc:
        logger.error('setup failed: %s', exc)
        await scanner.teardown()
        return 2

    try:
        max_dur = args.duration if args.duration > 0 else None
        await scanner.run(max_duration_sec=max_dur)
    finally:
        await scanner.teardown()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
