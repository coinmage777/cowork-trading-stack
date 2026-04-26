"""Solana Wallet Tracker — standalone, Foliage-focused.

DH wallet_tracker is EVM-only (ethereum/base/bsc/arbitrum/optimism/polygon).
Foliage_et's public Solana wallet is tracked here instead.

Polls Solana RPC every 60s for NEW SPL token holdings (first-time buys)
and recent signature history. Fires Telegram alert when the wallet acquires
a new token (especially with an emoji ticker) — this is the on-chain
version of "Foliage just bought X".

Run:
    py -3.12 strategies_minara/solana_wallet_tracker.py

Config: data/solana_wallet_watchlist.json (auto-created with Foliage entry).
Env:    SOLANA_RPC_URL (default: api.mainnet-beta.solana.com)
        HELIUS_API_KEY (optional — upgrades to Helius RPC for reliability)
        TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Output: data/solana_wallet_events.jsonl
Cache:  data/solana_wallet_state.json (known token holdings per wallet)

Lean — only 3 RPC methods used:
  - getTokenAccountsByOwner
  - getSignaturesForAddress
  - getAsset (fallback: token metadata via DexScreener tokens endpoint)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp  # type: ignore

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_DIR = ROOT / 'data'
WATCHLIST_PATH = DATA_DIR / 'solana_wallet_watchlist.json'
EVENTS_PATH = DATA_DIR / 'solana_wallet_events.jsonl'
STATE_PATH = DATA_DIR / 'solana_wallet_state.json'
LOG_PATH = ROOT / 'logs' / 'solana_wallet_tracker.log'

USER_AGENT = 'solana_wallet_tracker/1.0 (+local)'
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20)
SPL_TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'

DEFAULT_POLL_SEC = 60
MIN_POLL_SEC = 30
MAX_POLL_SEC = 600

DEFAULT_WATCHLIST: dict[str, Any] = {
    '_note': (
        'Solana wallets to monitor for new token holdings. DH wallet_tracker '
        'is EVM-only — this handles Solana. Edit entries to tune.'
    ),
    'wallets': {
        'foliage_et': {
            'address': '4t9bWuZsXXKGMgmd96nFD4KWxyPNTsPm4q9jEMH4jD2i',
            'label': 'Foliage_et (Solana)',
            'action': 'alert',
            'note': 'Emoji memecoin thesis author. Track new SPL token buys.',
        },
    },
    'poll_interval_sec': DEFAULT_POLL_SEC,
    'min_ui_amount': 0.0001,
    'alert_on_emoji_symbol_only': False,
}

# Unicode ranges for emoji detection (matches scanner)
_EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x2600, 0x27BF),
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F700, 0x1F77F),
    (0x1F780, 0x1F7FF),
    (0x1F800, 0x1F8FF),
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FA6F),
    (0x1FA70, 0x1FAFF),
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


def _rpc_url() -> str:
    helius = os.environ.get('HELIUS_API_KEY', '').strip()
    if helius:
        return f'https://mainnet.helius-rpc.com/?api-key={helius}'
    return os.environ.get('SOLANA_RPC_URL', '').strip() or 'https://api.mainnet-beta.solana.com'


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
    merged = dict(DEFAULT_WATCHLIST)
    merged.update(data)
    if 'wallets' not in merged or not isinstance(merged['wallets'], dict):
        merged['wallets'] = dict(DEFAULT_WATCHLIST['wallets'])
    return merged


def _load_state() -> dict[str, dict[str, Any]]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        logging.warning('state load failed: %s', exc)
        return {}


def _save_state(state: dict[str, dict[str, Any]]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
    except Exception as exc:  # noqa: BLE001
        logging.debug('state save err: %s', exc)


_TG_LAST_SENT: dict[str, float] = {}


async def send_telegram(session: aiohttp.ClientSession, text: str,
                        dedup_key: str | None = None, cooldown: float = 30.0) -> bool:
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat:
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


async def rpc_call(session: aiohttp.ClientSession, method: str,
                   params: list[Any], retries: int = 2) -> Any:
    url = _rpc_url()
    payload = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    for attempt in range(retries + 1):
        try:
            async with session.post(
                url, json=payload, timeout=HTTP_TIMEOUT,
                headers={'User-Agent': USER_AGENT, 'Content-Type': 'application/json'},
            ) as resp:
                if resp.status == 429:
                    await asyncio.sleep(2 + 3 * attempt)
                    continue
                if resp.status != 200:
                    logging.debug('rpc %s status=%s', method, resp.status)
                    return None
                data = await resp.json(content_type=None)
                if not isinstance(data, dict):
                    return None
                if 'error' in data:
                    logging.debug('rpc %s error: %s', method, data['error'])
                    return None
                return data.get('result')
        except asyncio.TimeoutError:
            logging.debug('rpc timeout %s attempt %d', method, attempt)
        except Exception as exc:  # noqa: BLE001
            logging.debug('rpc err %s: %s', method, exc)
        await asyncio.sleep(1 + attempt)
    return None


async def get_spl_holdings(session: aiohttp.ClientSession,
                           owner: str) -> dict[str, float]:
    """Return {mint: ui_amount} for all non-zero SPL balances of owner."""
    result = await rpc_call(
        session,
        'getTokenAccountsByOwner',
        [owner, {'programId': SPL_TOKEN_PROGRAM}, {'encoding': 'jsonParsed'}],
    )
    if not isinstance(result, dict):
        return {}
    holdings: dict[str, float] = {}
    for entry in (result.get('value') or []):
        try:
            info = entry['account']['data']['parsed']['info']
            mint = str(info.get('mint') or '')
            amt_raw = info.get('tokenAmount') or {}
            ui = amt_raw.get('uiAmount')
            if mint and ui is not None:
                holdings[mint] = float(ui)
        except (KeyError, TypeError, ValueError):
            continue
    return holdings


async def fetch_token_meta_dexscreener(session: aiohttp.ClientSession,
                                       mint: str) -> dict[str, Any]:
    """Best-effort symbol/name/price lookup via DexScreener tokens API."""
    url = f'https://api.dexscreener.com/tokens/v1/solana/{mint}'
    try:
        async with session.get(
            url, timeout=HTTP_TIMEOUT,
            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
        ) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logging.debug('dexscreener lookup err: %s', exc)
        return {}
    if not isinstance(data, list) or not data:
        return {}
    pair = data[0]
    base = pair.get('baseToken') or {}
    return {
        'symbol': str(base.get('symbol') or ''),
        'name': str(base.get('name') or ''),
        'price_usd': float(pair.get('priceUsd') or 0) if pair.get('priceUsd') else 0.0,
        'mcap_usd': float(pair.get('marketCap') or pair.get('fdv') or 0),
        'liquidity_usd': float((pair.get('liquidity') or {}).get('usd') or 0),
        'dexscreener_url': str(pair.get('url') or ''),
    }


def _append_event(rec: dict[str, Any]) -> None:
    try:
        EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with EVENTS_PATH.open('a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except Exception as exc:  # noqa: BLE001
        logging.debug('append event err: %s', exc)


async def check_wallet_once(session: aiohttp.ClientSession, key: str,
                            entry: dict[str, Any],
                            state: dict[str, dict[str, Any]],
                            alert_on_emoji_only: bool,
                            min_ui_amount: float) -> list[dict[str, Any]]:
    addr = str(entry.get('address') or '').strip()
    label = str(entry.get('label') or key)
    if not addr:
        return []
    holdings = await get_spl_holdings(session, addr)
    if not holdings:
        logging.debug('%s: no holdings fetched (rpc may be down)', label)
        return []

    wallet_state = state.setdefault(key, {'known_mints': {}, 'first_seen_ts': int(time.time())})
    known: dict[str, float] = dict(wallet_state.get('known_mints') or {})
    hits: list[dict[str, Any]] = []

    first_scan = not known  # first time we see this wallet — seed state, don't alert
    for mint, ui_amount in holdings.items():
        if ui_amount < min_ui_amount:
            continue
        prev = known.get(mint)
        if prev is not None:
            # already-known holding — update amount silently
            known[mint] = ui_amount
            continue
        # NEW mint
        known[mint] = ui_amount
        if first_scan:
            continue  # seeding — skip alerts

        meta = await fetch_token_meta_dexscreener(session, mint)
        symbol = meta.get('symbol', '')
        name = meta.get('name', '')
        if alert_on_emoji_only and not (_contains_emoji(symbol) or _contains_emoji(name)):
            logging.info('new mint %s (%s) — skipping (no emoji, alert_on_emoji_only=true)',
                         mint, symbol)
            continue
        hit = {
            'ts': int(time.time()),
            'event_type': 'solana_new_holding',
            'wallet_key': key,
            'wallet_label': label,
            'wallet_address': addr,
            'mint': mint,
            'ui_amount': ui_amount,
            'symbol': symbol,
            'name': name,
            'price_usd': meta.get('price_usd', 0),
            'mcap_usd': meta.get('mcap_usd', 0),
            'liquidity_usd': meta.get('liquidity_usd', 0),
            'dexscreener_url': meta.get('dexscreener_url', ''),
            'solscan_url': f'https://solscan.io/token/{mint}',
            'wallet_url': f'https://solscan.io/account/{addr}',
            'contains_emoji': _contains_emoji(symbol) or _contains_emoji(name),
        }
        hits.append(hit)

    wallet_state['known_mints'] = known
    wallet_state['last_scan_ts'] = int(time.time())
    state[key] = wallet_state
    return hits


def _fmt_hit_for_tg(hit: dict[str, Any]) -> str:
    wallet = hit.get('wallet_label', '?')
    symbol = hit.get('symbol', '?')
    name = hit.get('name', '')
    ui = hit.get('ui_amount', 0)
    mcap = hit.get('mcap_usd', 0)
    lp = hit.get('liquidity_usd', 0)
    price = hit.get('price_usd', 0)
    mint = hit.get('mint', '')
    url = hit.get('dexscreener_url') or hit.get('solscan_url', '')
    emoji_flag = '🔥 EMOJI ' if hit.get('contains_emoji') else ''
    return (
        f'<b>{emoji_flag}NEW SOL HOLDING</b>\n'
        f'{wallet} bought {symbol} ({name})\n'
        f'amount: {ui:.4f} · price ${price:g} · notional ~${ui * price:,.0f}\n'
        f'MCap ${mcap:,.0f} · LP ${lp:,.0f}\n'
        f'mint: <code>{mint}</code>\n'
        f'{url}'
    )


async def main_loop(one_shot: bool = False) -> int:
    _load_dotenv(ROOT / '.env')
    logging.info('solana_wallet_tracker starting (rpc=%s one_shot=%s)',
                 _rpc_url().split('?')[0], one_shot)

    state = _load_state()
    conn = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    async with aiohttp.ClientSession(connector=conn) as session:
        if os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'):
            await send_telegram(
                session,
                '🛰️ <b>Solana Wallet Tracker ONLINE</b>\n'
                f'watching: <code>{WATCHLIST_PATH.name}</code>',
                dedup_key='startup_sol',
                cooldown=60,
            )

        iterations = 0
        while True:
            iterations += 1
            watchlist = _load_watchlist()
            poll_sec = max(MIN_POLL_SEC, min(MAX_POLL_SEC,
                int(watchlist.get('poll_interval_sec') or DEFAULT_POLL_SEC)))
            alert_on_emoji_only = bool(watchlist.get('alert_on_emoji_symbol_only'))
            min_ui = float(watchlist.get('min_ui_amount') or 0.0001)

            total_hits = 0
            wallets = watchlist.get('wallets') or {}
            for key, entry in wallets.items():
                if not isinstance(entry, dict):
                    continue
                try:
                    hits = await check_wallet_once(
                        session, key, entry, state, alert_on_emoji_only, min_ui,
                    )
                except Exception as exc:  # noqa: BLE001
                    logging.exception('wallet %s scan err: %s', key, exc)
                    continue
                for hit in hits:
                    _append_event(hit)
                    logging.info('HIT %s new mint %s (%s)',
                                 hit['wallet_label'], hit['mint'], hit.get('symbol'))
                    try:
                        await send_telegram(
                            session, _fmt_hit_for_tg(hit),
                            dedup_key=f'solhit:{hit["wallet_address"]}:{hit["mint"]}',
                            cooldown=3600,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logging.debug('tg hit err: %s', exc)
                total_hits += len(hits)

            _save_state(state)
            logging.info('iter %d: %d wallets scanned, %d new holdings',
                         iterations, len(wallets), total_hits)
            if one_shot:
                return total_hits
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
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
