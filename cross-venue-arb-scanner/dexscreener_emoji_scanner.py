"""DexScreener Emoji Token Scanner — standalone.

Thesis (Foliage_et, 2026-04): emoji memecoins are the next wave. ASTEROID
already +443%. This scanner polls DexScreener every N minutes for tokens
whose symbol contains an emoji character, filters by MCap / LP / volume /
age, dedupes, and fires a Telegram alert on first detection.

Run standalone:
    py -3.12 strategies_minara/dexscreener_emoji_scanner.py

Config: data/emoji_watchlist.json (auto-created if missing, hot-reloaded).
Env:    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (loaded from .env or env).
Output: data/emoji_tokens.jsonl (one JSON object per detection).
Cache:  data/emoji_seen_cas.json (dedupe across restarts).

No external deps beyond aiohttp + stdlib.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Iterable

import aiohttp  # type: ignore

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = ROOT / 'data'
WATCHLIST_PATH = DATA_DIR / 'emoji_watchlist.json'
EVENTS_PATH = DATA_DIR / 'emoji_tokens.jsonl'
SEEN_PATH = DATA_DIR / 'emoji_seen_cas.json'
LOG_PATH = ROOT / 'logs' / 'emoji_scanner.log'

DEXSCREENER_SEARCH = 'https://api.dexscreener.com/latest/dex/search?q={q}'
DEXSCREENER_BOOSTS = 'https://api.dexscreener.com/token-boosts/latest/v1'
DEXSCREENER_TOKENS = 'https://api.dexscreener.com/tokens/v1/{chain}/{addr}'

USER_AGENT = 'dexscreener_emoji_scanner/1.0 (+local)'
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)

# DexScreener free tier: 300 req/min for search, 60/min for tokens. We stay well under.
MIN_POLL_INTERVAL = 60
DEFAULT_POLL_INTERVAL = 300

DEFAULT_WATCHLIST: dict[str, Any] = {
    '_note': (
        'Emoji symbols to scan for on DexScreener. Foliage_et thesis (2026-04): '
        'emoji memecoin wave. ASTEROID(☄️) already +443%.'
    ),
    'priority_emojis': [
        '🔶', '◎', '🔮', '🦞', '🥷', '🐱', '💹', '🧲', '⚡️', '☄️',
        '🌞', '🌑', '🐸', '🐶', '🐭', '🐹', '🦊', '🐵', '🦍', '🐧',
        '🐍', '🐙', '🦑', '🦀', '🐋', '🦈', '🐡', '🐬',
    ],
    'filters': {
        'mcap_min_usd': 50_000,
        'mcap_max_usd': 50_000_000,
        'lp_min_usd': 10_000,
        'vol24h_min_usd': 50_000,
        'age_max_days': 7,
        'chains_allowlist': ['solana', 'base', 'ethereum', 'bsc', 'arbitrum'],
    },
    'poll_interval_sec': DEFAULT_POLL_INTERVAL,
}


# ----------------------------------------------------------------------
# .env loader (minimal — no python-dotenv dep)
# ----------------------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as exc:  # noqa: BLE001
        logging.debug('dotenv load fail %s: %s', path, exc)


# ----------------------------------------------------------------------
# emoji detection
# ----------------------------------------------------------------------

# Unicode ranges covering most pictographic / symbol / emoji codepoints.
# Not exhaustive (purposely) — we want real emojis, not just any non-ASCII.
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x2600, 0x27BF),    # Miscellaneous Symbols + Dingbats (⚡, ☄, ✨, ◎, etc.)
    (0x1F300, 0x1F5FF),  # Misc Symbols and Pictographs
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F680, 0x1F6FF),  # Transport and Map
    (0x1F700, 0x1F77F),  # Alchemical
    (0x1F780, 0x1F7FF),  # Geometric Shapes Extended
    (0x1F800, 0x1F8FF),  # Supplemental Arrows-C
    (0x1F900, 0x1F9FF),  # Supplemental Symbols and Pictographs
    (0x1FA00, 0x1FA6F),  # Chess Symbols
    (0x1FA70, 0x1FAFF),  # Symbols and Pictographs Extended-A
    (0x2700, 0x27BF),    # Dingbats (redundant but explicit)
)


def _contains_emoji(text: str) -> bool:
    if not text:
        return False
    for ch in text:
        cp = ord(ch)
        for lo, hi in _EMOJI_RANGES:
            if lo <= cp <= hi:
                return True
    return False


def _extract_emojis(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        for lo, hi in _EMOJI_RANGES:
            if lo <= cp <= hi:
                out.append(ch)
                break
    return out


# ----------------------------------------------------------------------
# config load / save
# ----------------------------------------------------------------------

def _ensure_default_watchlist() -> None:
    if WATCHLIST_PATH.exists():
        return
    try:
        WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        WATCHLIST_PATH.write_text(
            json.dumps(DEFAULT_WATCHLIST, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception as exc:  # noqa: BLE001
        logging.warning('watchlist scaffold write failed: %s', exc)


def _load_watchlist() -> dict[str, Any]:
    _ensure_default_watchlist()
    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        logging.warning('watchlist load failed, using defaults: %s', exc)
        return dict(DEFAULT_WATCHLIST)
    if not isinstance(data, dict):
        return dict(DEFAULT_WATCHLIST)
    # merge with defaults for any missing keys
    merged = dict(DEFAULT_WATCHLIST)
    merged.update(data)
    if 'filters' not in merged or not isinstance(merged.get('filters'), dict):
        merged['filters'] = dict(DEFAULT_WATCHLIST['filters'])
    else:
        f = dict(DEFAULT_WATCHLIST['filters'])
        f.update(merged['filters'])
        merged['filters'] = f
    return merged


def _load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        data = json.loads(SEEN_PATH.read_text(encoding='utf-8'))
        if isinstance(data, list):
            return {str(x).lower() for x in data if x}
    except Exception as exc:  # noqa: BLE001
        logging.warning('seen-cache load failed: %s', exc)
    return set()


def _save_seen(seen: set[str]) -> None:
    try:
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        # cap at 10k to prevent unbounded growth
        trimmed = list(seen)[-10_000:]
        SEEN_PATH.write_text(
            json.dumps(trimmed, ensure_ascii=False),
            encoding='utf-8',
        )
    except Exception as exc:  # noqa: BLE001
        logging.debug('seen-cache save failed: %s', exc)


# ----------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------

_TG_LAST_SENT: dict[str, float] = {}


async def send_telegram(session: aiohttp.ClientSession, text: str,
                        dedup_key: str | None = None, cooldown: float = 30.0) -> bool:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
        logging.debug('telegram disabled (no creds)')
        return False
    if dedup_key:
        now = time.time()
        last = _TG_LAST_SENT.get(dedup_key, 0.0)
        if now - last < cooldown:
            return False
        _TG_LAST_SENT[dedup_key] = now
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = {
        'chat_id': chat,
        'text': text,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    }
    try:
        async with session.post(url, json=payload, timeout=HTTP_TIMEOUT) as resp:
            if resp.status != 200:
                body = await resp.text()
                logging.warning('telegram %s: %s', resp.status, body[:200])
                return False
            return True
    except Exception as exc:  # noqa: BLE001
        logging.warning('telegram send err: %s', exc)
        return False


# ----------------------------------------------------------------------
# DexScreener
# ----------------------------------------------------------------------

async def _fetch_json(session: aiohttp.ClientSession, url: str,
                      retries: int = 2) -> Any:
    for attempt in range(retries + 1):
        try:
            async with session.get(
                url,
                timeout=HTTP_TIMEOUT,
                headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
            ) as resp:
                if resp.status == 429:
                    # rate limited — back off
                    await asyncio.sleep(5 + 5 * attempt)
                    continue
                if resp.status != 200:
                    logging.debug('fetch %s -> %s', url, resp.status)
                    return None
                try:
                    return await resp.json(content_type=None)
                except Exception:
                    text = await resp.text()
                    try:
                        return json.loads(text)
                    except Exception:
                        return None
        except asyncio.TimeoutError:
            logging.debug('fetch timeout %s (attempt %d)', url, attempt)
        except Exception as exc:  # noqa: BLE001
            logging.debug('fetch err %s: %s', url, exc)
        await asyncio.sleep(1 + attempt)
    return None


async def search_emoji(session: aiohttp.ClientSession, emoji: str) -> list[dict[str, Any]]:
    """DexScreener /search?q=<emoji> — returns pair list."""
    import urllib.parse
    q = urllib.parse.quote(emoji)
    url = DEXSCREENER_SEARCH.format(q=q)
    data = await _fetch_json(session, url)
    if not isinstance(data, dict):
        return []
    pairs = data.get('pairs') or []
    return [p for p in pairs if isinstance(p, dict)]


# ----------------------------------------------------------------------
# pair filtering
# ----------------------------------------------------------------------

def _get_num(d: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def _pair_matches(pair: dict[str, Any], filters: dict[str, Any]) -> tuple[bool, str]:
    """Return (match, reason-if-not)."""
    chain = str(pair.get('chainId') or '').lower()
    allowed_chains = [c.lower() for c in filters.get('chains_allowlist', [])]
    if allowed_chains and chain not in allowed_chains:
        return False, f'chain {chain!r} not in allowlist'

    base = pair.get('baseToken') or {}
    symbol = str(base.get('symbol') or '')
    name = str(base.get('name') or '')
    if not (_contains_emoji(symbol) or _contains_emoji(name)):
        return False, 'no emoji in symbol/name'

    mcap = _get_num(pair, 'marketCap') or _get_num(pair, 'fdv')
    if mcap <= 0:
        return False, 'no mcap/fdv'
    mcap_min = float(filters.get('mcap_min_usd', 50_000))
    mcap_max = float(filters.get('mcap_max_usd', 50_000_000))
    if mcap < mcap_min:
        return False, f'mcap ${mcap:,.0f} < ${mcap_min:,.0f}'
    if mcap > mcap_max:
        return False, f'mcap ${mcap:,.0f} > ${mcap_max:,.0f}'

    lp = _get_num(pair, 'liquidity', 'usd')
    lp_min = float(filters.get('lp_min_usd', 10_000))
    if lp < lp_min:
        return False, f'lp ${lp:,.0f} < ${lp_min:,.0f}'

    vol24 = _get_num(pair, 'volume', 'h24')
    vol_min = float(filters.get('vol24h_min_usd', 50_000))
    if vol24 < vol_min:
        return False, f'vol24h ${vol24:,.0f} < ${vol_min:,.0f}'

    pair_created_ms = pair.get('pairCreatedAt') or 0
    try:
        pair_created_ms = int(pair_created_ms)
    except (TypeError, ValueError):
        pair_created_ms = 0
    age_max_days = float(filters.get('age_max_days', 7))
    if pair_created_ms > 0:
        age_days = (time.time() * 1000 - pair_created_ms) / 1000.0 / 86400.0
        if age_days > age_max_days:
            return False, f'age {age_days:.1f}d > {age_max_days}d'
    # if pair_created_ms == 0, API didn't provide — we let it pass (some DEXes omit)
    return True, ''


# ----------------------------------------------------------------------
# main scan cycle
# ----------------------------------------------------------------------

async def scan_once(session: aiohttp.ClientSession, watchlist: dict[str, Any],
                    seen: set[str]) -> list[dict[str, Any]]:
    emojis = list(watchlist.get('priority_emojis') or [])
    filters = dict(watchlist.get('filters') or {})
    hits: list[dict[str, Any]] = []
    total_pairs_seen = 0

    for emoji in emojis:
        pairs = await search_emoji(session, emoji)
        total_pairs_seen += len(pairs)
        for p in pairs:
            base = p.get('baseToken') or {}
            addr = str(base.get('address') or '').lower()
            chain = str(p.get('chainId') or '').lower()
            if not addr:
                continue
            ca_key = f'{chain}:{addr}'
            if ca_key in seen:
                continue
            ok, reason = _pair_matches(p, filters)
            if not ok:
                logging.debug('skip %s (%s): %s', base.get('symbol'), ca_key, reason)
                continue
            hit = _build_hit(p, emoji)
            hits.append(hit)
            seen.add(ca_key)
        # gentle rate-limit
        await asyncio.sleep(0.25)

    logging.info('scan_once: %d emojis -> %d pairs -> %d new hits',
                 len(emojis), total_pairs_seen, len(hits))
    return hits


def _build_hit(pair: dict[str, Any], matched_emoji: str) -> dict[str, Any]:
    base = pair.get('baseToken') or {}
    quote = pair.get('quoteToken') or {}
    mcap = _get_num(pair, 'marketCap') or _get_num(pair, 'fdv')
    lp = _get_num(pair, 'liquidity', 'usd')
    vol24 = _get_num(pair, 'volume', 'h24')
    price_usd = _get_num(pair, 'priceUsd')
    pair_created_ms = pair.get('pairCreatedAt') or 0
    try:
        pair_created_ms = int(pair_created_ms)
    except (TypeError, ValueError):
        pair_created_ms = 0
    age_hours = 0.0
    if pair_created_ms > 0:
        age_hours = (time.time() * 1000 - pair_created_ms) / 1000.0 / 3600.0
    change_h24 = _get_num(pair, 'priceChange', 'h24')
    return {
        'ts': int(time.time()),
        'matched_emoji': matched_emoji,
        'chain': str(pair.get('chainId') or ''),
        'dex': str(pair.get('dexId') or ''),
        'ticker': str(base.get('symbol') or ''),
        'name': str(base.get('name') or ''),
        'ca': str(base.get('address') or ''),
        'quote_symbol': str(quote.get('symbol') or ''),
        'price_usd': price_usd,
        'mcap_usd': mcap,
        'lp_usd': lp,
        'vol24h_usd': vol24,
        'change_h24_pct': change_h24,
        'age_hours': round(age_hours, 2),
        'dexscreener_url': str(pair.get('url') or ''),
    }


def _append_event(rec: dict[str, Any]) -> None:
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as exc:  # noqa: BLE001
        logging.debug('append event err: %s', exc)


def _fmt_hit_for_tg(hit: dict[str, Any]) -> str:
    ticker = hit.get('ticker', '?')
    name = hit.get('name', '')
    chain = hit.get('chain', '?')
    mcap = hit.get('mcap_usd', 0)
    lp = hit.get('lp_usd', 0)
    vol = hit.get('vol24h_usd', 0)
    age = hit.get('age_hours', 0)
    url = hit.get('dexscreener_url', '')
    ca = hit.get('ca', '')
    ch = hit.get('change_h24_pct', 0)
    emoji = hit.get('matched_emoji', '')
    return (
        f'<b>EMOJI TOKEN {emoji}</b>\n'
        f'{ticker} ({name}) · {chain}\n'
        f'MCap ${mcap:,.0f} · LP ${lp:,.0f} · Vol24h ${vol:,.0f}\n'
        f'24h {ch:+.1f}% · age {age:.1f}h\n'
        f'CA: <code>{ca}</code>\n'
        f'{url}'
    )


# ----------------------------------------------------------------------
# main loop
# ----------------------------------------------------------------------

async def main_loop(one_shot: bool = False) -> int:
    _load_dotenv(ROOT / '.env')
    logging.info('dexscreener_emoji_scanner starting (one_shot=%s)', one_shot)

    seen = _load_seen()
    logging.info('loaded %d seen CAs from cache', len(seen))

    conn = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=conn) as session:
        # startup test ping
        if os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'):
            await send_telegram(
                session,
                '🔎 <b>Emoji Scanner ONLINE</b>\n'
                f'watching: <code>{WATCHLIST_PATH.name}</code>\n'
                'polling DexScreener every 5min',
                dedup_key='startup',
                cooldown=60,
            )

        iterations = 0
        while True:
            iterations += 1
            watchlist = _load_watchlist()
            poll_sec = max(
                MIN_POLL_INTERVAL,
                int(watchlist.get('poll_interval_sec') or DEFAULT_POLL_INTERVAL),
            )
            try:
                hits = await scan_once(session, watchlist, seen)
            except Exception as exc:  # noqa: BLE001
                logging.exception('scan_once fatal: %s', exc)
                hits = []

            for hit in hits:
                _append_event(hit)
                logging.info(
                    'HIT %s %s %s mcap=$%.0f vol=$%.0f age=%.1fh',
                    hit['matched_emoji'], hit['ticker'], hit['chain'],
                    hit['mcap_usd'], hit['vol24h_usd'], hit['age_hours'],
                )
                try:
                    await send_telegram(
                        session,
                        _fmt_hit_for_tg(hit),
                        dedup_key=f'hit:{hit["chain"]}:{hit["ca"]}',
                        cooldown=3600,
                    )
                except Exception as exc:  # noqa: BLE001
                    logging.debug('tg send hit err: %s', exc)

            if hits:
                _save_seen(seen)

            if one_shot:
                return len(hits)

            logging.info('iter %d done — sleep %ds', iterations, poll_sec)
            try:
                await asyncio.sleep(poll_sec)
            except asyncio.CancelledError:
                break
    return 0


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fmt = '%(asctime)s %(levelname)s %(name)s: %(message)s'
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # clear preexisting handlers (re-runs in same interpreter)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = logging.FileHandler(LOG_PATH, encoding='utf-8')
    fh.setFormatter(logging.Formatter(fmt))
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter(fmt))
    root.addHandler(sh)


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or sys.argv[1:])
    one_shot = '--once' in argv
    _setup_logging()
    try:
        return asyncio.run(main_loop(one_shot=one_shot))
    except KeyboardInterrupt:
        logging.info('interrupted')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
