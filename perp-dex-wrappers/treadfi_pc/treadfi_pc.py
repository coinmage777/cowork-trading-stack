"""
TreadFi + Pacifica Exchange Wrapper

Combines:
- Login/Auth: TreadFi session-based login (same as treadfi_hl.py)
- Orders: TreadFi order API
- Data Fetching: Pacifica WebSocket (prices, positions, collateral, open orders)

Symbol format: BTC:PERP-USDC
"""
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import logging

import aiohttp
from aiohttp import web, TCPConnector

logger = logging.getLogger(__name__)
from eth_account import Account
from eth_account.messages import encode_defunct

from mpdex.base import MultiPerpDex, MultiPerpDexMixin


# Pacifica API
PACIFICA_BASE_URL = "https://api.pacifica.fi/api/v1"


class TreadfiPcExchange(MultiPerpDexMixin, MultiPerpDex):
	"""
	TreadFi + Pacifica Exchange

	Usage:
		ex = TreadfiPcExchange(
			login_wallet_address="0x...",
			login_wallet_private_key="0x...",  # or use browser signing
			account_name="my_account",
			pacifica_public_key="...",  # Solana pubkey for Pacifica
		)
		await ex.init()
	"""

	def __init__(
		self,
		session_cookies: Optional[Dict[str, str]] = None,
		login_wallet_address: Optional[str] = None,
		login_wallet_private_key: Optional[str] = None,
		account_name: Optional[str] = None,
		pacifica_public_key: Optional[str] = None,
	):
		super().__init__()

		# TreadFi login
		self.login_wallet_address = login_wallet_address
		self.account_name = account_name
		self.account_id = None

		self.url_base = "https://app.tread.fi/"
		self._logged_in = False
		self._cookies = session_cookies or {}
		self._login_pk = login_wallet_private_key
		self._login_event: Optional[asyncio.Event] = None

		# Pacifica data fetching
		self.pacifica_public_key = pacifica_public_key
		self.ws_client = None
		# WS support flags (uses Pacifica WS)
		self.ws_supported = {
			"get_mark_price": True,
			"get_position": True,
			"get_open_orders": True,
			"get_collateral": True,
			"get_orderbook": True,
			"create_order": False,  # Uses REST
			"cancel_orders": False,  # Uses REST
			"update_leverage": False,
		}

		# HTTP session
		self._http: Optional[aiohttp.ClientSession] = None

		# Symbol metadata from Pacifica
		self._symbol_meta: Dict[str, Dict[str, Any]] = {}
		self._symbol_list: List[str] = []
		self._initialized: bool = False
		self._leverage_updated: Dict[str, bool] = {}  # symbol -> updated flag

		# Price cache (REST fallback)
		self._price_cache: Dict[str, Dict[str, Any]] = {}

		# Normalize cookies
		self._normalize_or_clear_cookies()
		if not self._has_valid_cookies():
			self._load_cached_cookies()

	def _session(self) -> aiohttp.ClientSession:
		if self._http is None or self._http.closed:
			self._http = aiohttp.ClientSession(
				connector=TCPConnector(
					force_close=True,
					enable_cleanup_closed=True,
				),
				headers={
					"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
					              "AppleWebKit/537.36 (KHTML, like Gecko) "
					              "Chrome/145.0.0.0 Safari/537.36",
					"Accept": "application/json, text/plain, */*",
					"Accept-Language": "en-US,en;q=0.9",
				},
			)
		return self._http

	async def close(self, force_close: bool = True):
		"""
		Close connection.

		Args:
			force_close: True (default) = 연결 종료, False = 풀에 유지
		"""
		if self._http and not self._http.closed:
			await self._http.close()
		if self.ws_client:
			from .pacifica_ws_client import PACIFICA_WS_POOL
			await PACIFICA_WS_POOL.release(
				self.pacifica_public_key or "public",
				force_close=force_close
			)
			self.ws_client = None

	# ----------------------------
	# Initialization
	# ----------------------------
	async def init(self) -> "TreadfiPcExchange":
		# 1. TreadFi login
		login_md = await self.login()
		if not self._logged_in:
			raise RuntimeError(f"TreadFi login failed: {login_md}")

		self.account_id = await self.get_account_id()
		if self.account_id:
			logger.debug(f"[treadfi_pc] Account ID: {self.account_id}")
		else:
			logger.debug(f"[treadfi_pc] WARNING: account_id를 가져오지 못함 — 주문 실행이 제한될 수 있음")

		# 2. Fetch Pacifica symbol info
		await self._fetch_pacifica_info()

		# 3. Update available symbols
		self.update_available_symbols()

		# 4. Initialize WS client
		await self._create_ws_client()

		return self

	async def _fetch_pacifica_info(self):
		"""Fetch symbol metadata from Pacifica"""
		url = f"{PACIFICA_BASE_URL}/info"
		s = self._session()
		async with s.get(url) as r:
			r.raise_for_status()
			data = await r.json()

		items = data.get("data") or []
		meta: Dict[str, Dict[str, Any]] = {}
		symbols: List[str] = []
		for it in items:
			if not isinstance(it, dict):
				continue
			sym = str(it.get("symbol") or "").upper()
			if not sym:
				continue
			meta[sym] = {
				"tick_size": str(it.get("tick_size") or "1"),
				"lot_size": str(it.get("lot_size") or "1"),
				"min_tick": str(it.get("min_tick") or "0"),
				"max_tick": str(it.get("max_tick") or "0"),
				"min_order_size": str(it.get("min_order_size") or "0"),
				"max_order_size": str(it.get("max_order_size") or "0"),
				"max_leverage": int(it.get("max_leverage") or 1),
				"isolated_only": bool(it.get("isolated_only", False)),
			}
			symbols.append(sym)

		self._symbol_meta = meta
		self._symbol_list = sorted(set(symbols))
		self._initialized = True

	def update_available_symbols(self):
		"""Update available_symbols dict"""
		self.available_symbols['perp'] = []
		for sym in self._symbol_list:
			quote = self.get_perp_quote(sym)
			# Just same as pacifica
			composite_symbol = f"{sym}-{quote}"
			self.available_symbols['perp'].append(composite_symbol)

	async def _create_ws_client(self):
		"""Create Pacifica WebSocket client"""
		if self.ws_client is not None:
			return

		from .pacifica_ws_client import PACIFICA_WS_POOL

		self.ws_client = await PACIFICA_WS_POOL.acquire(
			public_key=self.pacifica_public_key or "public",
			agent_public_key=None,
			agent_keypair=None,
			subscribe_private=bool(self.pacifica_public_key),
		)

	def get_perp_quote(self, symbol: str, *, is_basic_coll: bool = False) -> str:
		return "USDC"

	def _symbol_to_pacifica(self, symbol: str) -> str:
		"""
		Convert TreadFi symbol to Pacifica symbol.
		BTC:PERP-USDC -> BTC
		"""
		if ":" in symbol:
			return symbol.split(":")[0].upper()
		return symbol.upper()

	# ----------------------------
	# TreadFi Login (from treadfi_hl.py)
	# ----------------------------
	def _login_html(self) -> str:
		"""Load login UI HTML from built file (with Reown AppKit for WalletConnect)"""
		from pathlib import Path
		module_dir = Path(__file__).parent
		dist_html = module_dir / "treadfi_login_ui" / "dist" / "index.html"

		if dist_html.exists():
			try:
				return dist_html.read_text(encoding="utf-8")
			except Exception as e:
				logger.debug(f"[treadfi_pc] Failed to load built HTML: {e}")

		# Fallback to minimal HTML
		return self._login_html_fallback()

	def _login_html_fallback(self) -> str:
		"""Minimal fallback HTML (browser wallet only, no WalletConnect)"""
		return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>TreadFi Login</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
body { font-family: system-ui, sans-serif; padding: 24px; max-width: 500px; margin: 0 auto; background: #f5f5f5; }
.card { background: white; padding: 24px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }
.row { margin: 16px 0; }
input, textarea { width: 100%; padding: 10px; font-size: 14px; font-family: monospace; border: 1px solid #ddd; border-radius: 6px; box-sizing: border-box; }
textarea { height: 80px; resize: vertical; }
.btn-group { display: flex; gap: 8px; flex-wrap: wrap; }
button { padding: 12px 20px; cursor: pointer; font-size: 14px; border: none; border-radius: 8px; font-weight: 500; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-primary { background: #10b981; color: white; }
.btn-secondary { background: #6b7280; color: white; }
.status { padding: 12px; border-radius: 8px; margin-top: 16px; display: none; }
.status.success { background: #d1fae5; color: #065f46; display: block; }
.status.error { background: #fee2e2; color: #991b1b; display: block; }
.status.info { background: #dbeafe; color: #1e40af; display: block; }
h2 { margin: 0 0 20px; color: #1f2937; }
label { font-weight: 500; display: block; margin-bottom: 6px; color: #374151; }
</style>
</head>
<body>
<div class="card">
<h2>TreadFi Login</h2>
<div class="row">
    <label>Wallet Address</label>
    <input id="address" type="text" readonly />
</div>
<div class="row">
    <div class="btn-group">
        <button id="connectBtn" class="btn-primary">Connect Wallet</button>
        <button id="signBtn" class="btn-primary" disabled>Sign & Login</button>
    </div>
</div>
<div class="row">
    <label>Message to Sign</label>
    <textarea id="message" readonly placeholder="Loading..."></textarea>
</div>
<div id="statusBox" class="status"></div>
</div>
<script>
const $ = s => document.querySelector(s);
let account = null, lastNonce = null, signingMessage = null;
const showStatus = (msg, type) => { const b = $('#statusBox'); b.textContent = msg; b.className = 'status ' + type; };

async function fetchNonce() {
    try {
        const r = await fetch('/nonce');
        const j = await r.json();
        if (j.error) throw new Error(j.error);
        lastNonce = j.nonce;
        signingMessage = j.message;
        $('#message').value = j.message;
        showStatus('Message loaded', 'info');
    } catch (e) { showStatus('Failed: ' + e.message, 'error'); }
}
fetchNonce();

$('#connectBtn').onclick = async () => {
    try {
        if (!window.ethereum) throw new Error('No wallet detected');
        const acc = await window.ethereum.request({ method: 'eth_requestAccounts' });
        account = acc[0];
        $('#address').value = account;
        $('#signBtn').disabled = !signingMessage;
        $('#connectBtn').disabled = true;
        showStatus('Connected: ' + account, 'success');
    } catch (e) { showStatus('Connect failed: ' + e.message, 'error'); }
};

$('#signBtn').onclick = async () => {
    try {
        if (!account || !signingMessage) throw new Error('Connect wallet first');
        showStatus('Please sign in your wallet...', 'info');
        const signature = await window.ethereum.request({ method: 'personal_sign', params: [signingMessage, account] });
        showStatus('Submitting...', 'info');
        const r = await fetch('/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address: account, signature, nonce: lastNonce }) });
        const text = await r.text();
        if (!r.ok) throw new Error(text);
        showStatus('Login successful! You can close this tab.', 'success');
        $('#signBtn').disabled = true;
    } catch (e) { showStatus('Failed: ' + e.message, 'error'); }
};
</script>
</body>
</html>
"""

	async def login(self):
		# 1) Try cached cookies
		if self._has_valid_cookies():
			md = await self._get_user_metadata()
			if md.get("is_authenticated"):
				logger.debug("[treadfi_pc] Login authenticated via cached cookies!")
				self._logged_in = True
				self._save_cached_cookies()
				return md
			else:
				logger.debug(f"[treadfi_pc] Cache outdated: {md}")

		# 2) Try private key signing
		if self._login_pk:
			try:
				nonce = await self._get_nonce()
				msg = f"Sign in to Tread with nonce: {nonce}"
				acct = Account.from_key(self._login_pk)
				sign = Account.sign_message(encode_defunct(text=msg), private_key=self._login_pk).signature.hex()
				await self._wallet_auth(acct.address, sign, nonce)
				md = await self._get_user_metadata()
				if not md.get("is_authenticated"):
					raise RuntimeError("login failed with private key")
				logger.debug("[treadfi_pc] Login authenticated via private key!")
				self._logged_in = True
				self._save_cached_cookies()
				return md
			except Exception as e:
				logger.debug(f"[treadfi_pc] PK 로그인 실패 ({e}), 브라우저 로그인으로 전환...")

		# 3) Browser signing (port 6975 to avoid conflict with treadfi_hl)
		self._login_event = asyncio.Event()

		async def handle_index(_req):
			return web.Response(text=self._login_html(), content_type="text/html")

		async def handle_nonce(_req):
			try:
				nonce = await self._get_nonce()
				msg = f"Sign in to Tread with nonce: {nonce}"
				return web.json_response({"nonce": nonce, "message": msg})
			except Exception as e:
				return web.json_response({"error": str(e)}, status=500)

		async def handle_submit(req):
			try:
				body = await req.json()
				address = body.get("address")
				signature = body.get("signature")
				nonce = body.get("nonce")
				if not (address and signature and nonce):
					return web.Response(status=400, text="missing address/signature/nonce")

				await self._wallet_auth(address, signature, nonce)

				md = await self._get_user_metadata()
				if not md.get("is_authenticated"):
					return web.Response(status=400, text="login failed")

				self._logged_in = True
				self._login_event.set()
				return web.Response(text="Login success. You can close this tab.")
			except Exception as e:
				return web.Response(status=500, text=f"submit error: {e}")

		app = web.Application()
		app.router.add_get("/", handle_index)
		app.router.add_get("/nonce", handle_nonce)
		app.router.add_post("/submit", handle_submit)

		runner = web.AppRunner(app)
		await runner.setup()
		site = web.TCPSite(runner, "127.0.0.1", 6975)
		await site.start()

		logger.debug("[treadfi_pc] Open http://127.0.0.1:6975 in your browser to sign the message")
		await self._login_event.wait()
		await runner.cleanup()

		md = await self._get_user_metadata()
		if not md.get("is_authenticated"):
			raise RuntimeError("login failed after browser sign")
		logger.debug("[treadfi_pc] Login authenticated via browser!")
		self._logged_in = True
		self._save_cached_cookies()
		return md

	def _addr_lower(self, address: str) -> str:
		return "0x" + address[2:].lower()

	async def _get_nonce(self) -> str:
		s = self._session()

		# 첫 로그인: 메인 페이지 방문 → CSRF 토큰 + Cloudflare 쿠키 획득
		if not self._has_valid_cookies():
			logger.debug("[treadfi_pc] 메인 페이지 방문 중 (CSRF/CF 쿠키 획득)...")
			async with s.get(self.url_base) as r:
				# aiohttp cookie jar가 자동으로 쿠키 저장
				# 수동으로도 csrftoken 추출
				for key, morsel in (r.cookies or {}).items():
					if key == "csrftoken":
						self._cookies["csrftoken"] = morsel.value
						logger.debug(f"[treadfi_pc] CSRF 토큰 획득: {morsel.value[:12]}...")
				# cookie jar에서도 추출 시도
				if not self._cookies.get("csrftoken"):
					for cookie in s.cookie_jar:
						if cookie.key == "csrftoken":
							self._cookies["csrftoken"] = cookie.value
							logger.debug(f"[treadfi_pc] CSRF 토큰 (jar): {cookie.value[:12]}...")
							break

		# nonce 요청 헤더
		headers = {
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
		}
		# 쿠키 설정 (유효 세션 or csrftoken만)
		if self._has_valid_cookies():
			headers.update(self._cookie_header())
		elif self._cookies.get("csrftoken"):
			headers["Cookie"] = f"csrftoken={self._cookies['csrftoken']}"

		async with s.get(self.url_base + "internal/account/get_nonce/", headers=headers) as r:
			if r.status == 403:
				text = await r.text()
				raise RuntimeError(f"get_nonce 403 — 응답: {text[:300]}")
			data = await r.json()
			nonce = data.get("nonce")
			if not nonce:
				raise RuntimeError(f"failed to get nonce: {data}")
			return nonce

	async def _wallet_auth(self, address: str, signature: str, nonce: str) -> Dict[str, str]:
		s = self._session()
		payload = {
			"address": self._addr_lower(address),
			"signature": signature,
			"nonce": nonce,
			"wallet_type": "evm",
		}
		headers = {
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
			"Content-Type": "application/json",
		}
		# CSRF 토큰 + 쿠키 추가
		if self._cookies.get("csrftoken"):
			headers["X-CSRFToken"] = self._cookies["csrftoken"]
			headers["Cookie"] = f"csrftoken={self._cookies['csrftoken']}"
			if self._cookies.get("sessionid"):
				headers["Cookie"] += f"; sessionid={self._cookies['sessionid']}"
		async with s.post(self.url_base + "internal/account/wallet_auth/", json=payload, headers=headers) as r:
			text = await r.text()
			if r.status != 200:
				raise RuntimeError(f"wallet_auth failed: {r.status} {text}")

			def _collect(resp):
				out = {}
				for res in list(resp.history) + [resp]:
					for k, morsel in (res.cookies or {}).items():
						out[k] = morsel.value
				return out

			setcookies = _collect(r)
			ct_val = setcookies.get("csrftoken")
			sid_val = setcookies.get("sessionid")

		if not ct_val or not sid_val:
			raise RuntimeError("missing csrftoken/sessionid after wallet_auth")

		self._cookies = {"csrftoken": ct_val, "sessionid": sid_val}
		self._normalize_or_clear_cookies()
		self.login_wallet_address = self._addr_lower(address)
		self._save_cached_cookies()
		return self._cookies

	async def _get_user_metadata(self) -> dict:
		s = self._session()
		headers = {"Origin": self.url_base.rstrip("/"), "Referer": self.url_base, **self._cookie_header()}
		async with s.get(self.url_base + "internal/account/user_metadata/", headers=headers) as r:
			try:
				data = await r.json()
			except Exception:
				text = await r.text()
				raise RuntimeError(f"user_metadata bad response: {r.status} {text}")
			return data

	async def get_account_id(self) -> Optional[str]:
		if not self._has_valid_cookies():
			raise RuntimeError("not logged in: missing session cookies")

		s = self._session()
		url = self.url_base + "internal/sor/get_cached_account_balance"
		headers = {
			"Accept": "*/*",
			"X-CSRFToken": self._cookies["csrftoken"],
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
			**self._cookie_header(),
		}

		# 1) 먼저 지정된 account_name으로 시도
		if self.account_name:
			params = {"account_names": self.account_name}
			async with s.get(url, params=params, headers=headers) as r:
				if r.status == 200:
					try:
						data = await r.json()
						balances = data.get("balances") or []
						for item in balances:
							if isinstance(item, dict) and item.get("account_name") == self.account_name:
								acc_id = item.get("account_id")
								if isinstance(acc_id, str) and acc_id:
									return acc_id
					except Exception:
						pass
				else:
					text = await r.text()
					logger.debug(f"[treadfi_pc] account_name='{self.account_name}' 조회 실패 ({r.status}): {text[:200]}")

		# 2) 실패 시 user_metadata에서 계정 목록 확인
		logger.debug("[treadfi_pc] user_metadata에서 계정 정보 탐색 중...")
		md = await self._get_user_metadata()
		accounts = md.get("accounts") or md.get("exchange_accounts") or []
		logger.debug(f"[treadfi_pc] user_metadata 응답 키: {list(md.keys())}")

		# accounts가 리스트인 경우
		if isinstance(accounts, list):
			for acc in accounts:
				if isinstance(acc, dict):
					acc_name = acc.get("account_name") or acc.get("name")
					if acc_name:
						logger.debug(f"[treadfi_pc] 발견된 계정: {acc_name}")
						# 해당 계정으로 다시 조회
						params = {"account_names": acc_name}
						async with s.get(url, params=params, headers=headers) as r:
							if r.status == 200:
								try:
									data = await r.json()
									balances = data.get("balances") or []
									for item in balances:
										if isinstance(item, dict):
											acc_id = item.get("account_id")
											if isinstance(acc_id, str) and acc_id:
												self.account_name = acc_name
												logger.debug(f"[treadfi_pc] 계정 자동 설정: {acc_name} (id={acc_id})")
												return acc_id
								except Exception:
									pass

		# 3) 그래도 못 찾으면 metadata 전체 출력
		logger.debug(f"[treadfi_pc] 계정을 찾지 못함. metadata: {json.dumps(md, indent=2, default=str)[:500]}")
		return None

	# ----------------------------
	# Cookie helpers
	# ----------------------------
	def _has_valid_cookies(self, cookies: Optional[Dict[str, str]] = None) -> bool:
		c = (cookies if cookies is not None else self._cookies) or {}
		return bool(c.get("csrftoken")) and bool(c.get("sessionid"))

	def _normalize_or_clear_cookies(self) -> None:
		c = self._cookies or {}
		if not c.get("csrftoken") or not c.get("sessionid"):
			self._cookies = {}

	def _cookie_header(self) -> Dict[str, str]:
		if not self._has_valid_cookies():
			return {}
		return {"Cookie": f"csrftoken={self._cookies['csrftoken']}; sessionid={self._cookies['sessionid']}"}

	def _find_project_root(self) -> Path:
		markers = {"pyproject.toml", "setup.cfg", "setup.py", ".git"}
		p = Path.cwd().resolve()
		for parent in [p] + list(p.parents):
			for name in markers:
				if (parent / name).exists():
					return parent
		return Path.cwd().resolve()

	def _cache_dir(self) -> str:
		base = self._find_project_root()
		target = base / ".cache"
		try:
			target.mkdir(parents=True, exist_ok=True)
			return str(target)
		except Exception:
			home_fallback = Path.home() / ".cache" / "mpdex"
			home_fallback.mkdir(parents=True, exist_ok=True)
			return str(home_fallback)

	def _cache_path(self) -> str:
		addr = (self.login_wallet_address or "default").lower()
		if addr and not addr.startswith("0x"):
			addr = f"0x{addr}"
		safe = addr.replace(":", "_")
		return os.path.join(self._cache_dir(), f"treadfi_pc_session_{safe}.json")

	def _load_cached_cookies(self) -> bool:
		try:
			path = self._cache_path()
			if not os.path.exists(path):
				return False
			with open(path, "r", encoding="utf-8") as f:
				data = json.load(f)
			csrftoken, sessionid = data.get("csrftoken"), data.get("sessionid")
			if csrftoken and sessionid:
				self._cookies = {"csrftoken": csrftoken, "sessionid": sessionid}
				return True
			return False
		except Exception:
			return False

	def _save_cached_cookies(self) -> None:
		try:
			if not self._has_valid_cookies():
				return
			if not self.login_wallet_address:
				return
			payload = {
				"csrftoken": self._cookies["csrftoken"],
				"sessionid": self._cookies["sessionid"],
				"login_wallet_address": self.login_wallet_address,
				"saved_at": int(time.time()),
			}
			with open(self._cache_path(), "w", encoding="utf-8") as f:
				json.dump(payload, f, ensure_ascii=False, indent=2)
		except Exception:
			pass

	def _clear_cached_cookies(self) -> None:
		try:
			p = self._cache_path()
			if os.path.exists(p):
				os.remove(p)
		except Exception:
			pass

	# ----------------------------
	# Price/Amount formatting (from Pacifica)
	# ----------------------------
	def _dec(self, x) -> Decimal:
		return x if isinstance(x, Decimal) else Decimal(str(x))

	def _format_with_step(self, value: Decimal, step: Decimal) -> str:
		q = value.quantize(step)
		return format(q, "f")

	def _get_symbol_meta(self, symbol: str) -> Dict[str, Any]:
		sym = self._symbol_to_pacifica(symbol)
		meta = self._symbol_meta.get(sym)
		if not meta:
			return {
				"tick_size": "1",
				"lot_size": "1",
				"min_tick": "0",
				"max_tick": "0",
				"min_order_size": "0",
				"max_order_size": "0",
			}
		return meta

	def _adjust_price_tick(self, symbol: str, price, *, rounding=ROUND_HALF_UP) -> str:
		meta = self._get_symbol_meta(symbol)
		step = self._dec(meta["tick_size"])
		p = self._dec(price)
		units = (p / step).to_integral_value(rounding=rounding)
		adjusted = (units * step).quantize(step)
		return self._format_with_step(adjusted, step)

	def _adjust_amount_lot(self, symbol: str, amount, *, rounding=ROUND_DOWN) -> str:
		meta = self._get_symbol_meta(symbol)
		step = self._dec(meta["lot_size"])
		a = self._dec(amount)
		if step <= 0:
			return str(amount)
		units = (a / step).to_integral_value(rounding=rounding)
		adjusted = (units * step).quantize(step)
		return self._format_with_step(adjusted, step)

	# ----------------------------
	# Data Fetching (Pacifica WS/REST)
	# ----------------------------
	async def get_mark_price(self, symbol: str, *, force_refresh: bool = True) -> Optional[float]:
		"""Get mark price (WS first, REST fallback)"""
		pac_symbol = self._symbol_to_pacifica(symbol)

		try:
			return await self.get_mark_price_ws(pac_symbol)
		except Exception as e:
			logger.debug(f"[treadfi_pc] get_mark_price WS failed, falling back to REST: {e}")
			return await self.get_mark_price_rest(pac_symbol, force_refresh=force_refresh)

	async def get_mark_price_ws(self, symbol: str, timeout: float = 5.0) -> Optional[float]:
		if not self.ws_client:
			await self._create_ws_client()

		ready = await self.ws_client.wait_prices_ready(timeout=timeout)
		if not ready:
			raise TimeoutError("WS prices not ready")

		return self.ws_client.get_mark_price(symbol.upper())

	async def get_mark_price_rest(self, symbol: str, *, force_refresh: bool = True) -> Optional[float]:
		symbol = symbol.upper()
		if force_refresh:
			await self._refresh_prices()

		entry = self._price_cache.get(symbol)
		if entry:
			val = entry.get("mark")
			if val is not None:
				try:
					return float(val)
				except (ValueError, TypeError):
					pass
		return None

	async def _refresh_prices(self) -> Dict[str, float]:
		url = f"{PACIFICA_BASE_URL}/info/prices"
		s = self._session()
		async with s.get(url) as r:
			r.raise_for_status()
			data = await r.json()

		items = data.get("data") or []
		ts_now = int(time.time() * 1000)
		out: Dict[str, float] = {}

		for it in items:
			if not isinstance(it, dict):
				continue
			sym = str(it.get("symbol") or "").upper()
			if not sym:
				continue

			def _f(k):
				v = it.get(k)
				try:
					return float(Decimal(str(v))) if v is not None else None
				except Exception:
					return None

			mark = _f("mark")
			self._price_cache[sym] = {
				"mark": mark,
				"mid": _f("mid"),
				"oracle": _f("oracle"),
				"ts": int(it.get("timestamp") or ts_now),
			}
			if mark is not None:
				out[sym] = mark

		return out

	async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
		"""Get position (WS first, REST fallback)"""
		pac_symbol = self._symbol_to_pacifica(symbol)

		if self.pacifica_public_key:
			try:
				return await self.get_position_ws(pac_symbol)
			except Exception as e:
				logger.debug(f"[treadfi_pc] get_position WS failed, falling back to REST: {e}")

		return await self.get_position_rest(pac_symbol)

	async def get_position_ws(self, symbol: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
		if not self.ws_client:
			await self._create_ws_client()

		await self.ws_client.wait_positions_ready(timeout=timeout)
		pos = self.ws_client.get_position(symbol.upper())
		if pos is None:
			return None

		return self._parse_position_ws(pos)

	def _parse_position_ws(self, pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
		if not pos:
			return None
		amount = pos.get("amount", "0")
		try:
			if float(amount) == 0:
				return None
		except (ValueError, TypeError):
			return None

		side_raw = pos.get("side", "")
		side = "long" if side_raw == "bid" else "short"

		return {
			"symbol": pos.get("symbol"),
			"side": side,
			"size": amount,
			"entry_price": pos.get("entry_price"),
			"unrealized_pnl": None,
			"liquidation_price": pos.get("liquidation_price"),
			"raw_data": pos,
		}

	async def get_position_rest(self, symbol: str) -> Optional[Dict[str, Any]]:
		if not self.pacifica_public_key:
			return None

		url = f"{PACIFICA_BASE_URL}/positions"
		s = self._session()
		params = {"account": self.pacifica_public_key}

		async with s.get(url, params=params) as r:
			r.raise_for_status()
			data = await r.json()

		data = data.get('data', {})
		for pos in data:
			if pos.get("symbol") == symbol:
				return {
					"symbol": symbol,
					"side": "long" if pos.get("side") == "bid" else "short",
					"size": pos.get("amount"),
					"entry_price": pos.get("entry_price"),
					"unrealized_pnl": None,
					"liquidation_price": pos.get("liquidation_price"),
					"raw_data": pos,
				}
		return None

	async def get_collateral(self) -> Dict[str, Any]:
		"""Get collateral (WS first, REST fallback)"""
		if self.pacifica_public_key:
			try:
				return await self.get_collateral_ws()
			except Exception as e:
				logger.debug(f"[treadfi_pc] get_collateral WS failed, falling back to REST: {e}")

		return await self.get_collateral_rest()

	async def get_collateral_ws(self, timeout: float = 5.0) -> Dict[str, Any]:
		if not self.ws_client:
			await self._create_ws_client()

		ready = await self.ws_client.wait_account_info_ready(timeout=timeout)
		if not ready:
			raise TimeoutError("WS account_info not ready")

		return self.ws_client.get_collateral()

	async def get_collateral_rest(self) -> Dict[str, Any]:
		if not self.pacifica_public_key:
			return {"total_collateral": None, "available_collateral": None}

		url = f"{PACIFICA_BASE_URL}/account"
		s = self._session()
		params = {"account": self.pacifica_public_key}
		async with s.get(url, params=params) as r:
			r.raise_for_status()
			data = await r.json()

		data = data.get('data', {})
		return {
			"total_collateral": data.get("account_equity"),
			"available_collateral": data.get("available_to_spend"),
		}

	async def get_orderbook(self, symbol: str, agg_level: int = 1, timeout: float = 5.0) -> Dict[str, Any]:
		"""Get orderbook via WS"""
		pac_symbol = self._symbol_to_pacifica(symbol)

		if not self.ws_client:
			await self._create_ws_client()

		await self.ws_client.subscribe_orderbook(pac_symbol, agg_level=agg_level)
		ready = await self.ws_client.wait_orderbook_ready(pac_symbol, timeout=timeout)
		if not ready:
			raise TimeoutError(f"WS orderbook not ready for {pac_symbol}")

		return self.ws_client.get_orderbook(pac_symbol)

	# ----------------------------
	# TreadFi Orders
	# ----------------------------
	async def update_leverage(self, symbol: str, leverage: Optional[int] = None, margin_mode: Optional[str] = None):
		"""
		Update leverage and/or margin mode via TreadFi API.

		Args:
			leverage: If None, only margin_mode is updated (current leverage is preserved).
			margin_mode: If None, only leverage is updated (current margin_mode is preserved).

		Note: At least one of leverage or margin_mode should be provided.
		"""
		if leverage is None and margin_mode is None:
			return {"status": "error", "message": "At least one of leverage or margin_mode must be provided"}

		# ws 조회는 pacifica symbol, treadfi 주문은 treadfi symbol 사용
		pac_symbol = self._symbol_to_pacifica(symbol)

		if not self._has_valid_cookies():
			raise RuntimeError("not logged in")

		# If one of leverage/margin_mode is None, get current settings to preserve it
		current_leverage = leverage
		current_margin_mode = margin_mode
		if leverage is None or margin_mode is None:
			current_info = await self.get_leverage_info(symbol)
			if current_info.get("status") == "ok":
				if leverage is None:
					current_leverage = current_info.get("leverage", 1)
				if margin_mode is None:
					current_margin_mode = current_info.get("margin_mode", "cross")
			else:
				# Fallback defaults
				if leverage is None:
					meta = self._symbol_meta.get(pac_symbol, {})
					current_leverage = meta.get("max_leverage", 10)
				if margin_mode is None:
					current_margin_mode = "cross"

		actual_margin_mode = current_margin_mode.upper()  # TreadFi uses uppercase

		payload = {
			"account_ids": [self.account_id],
			"margin_mode": actual_margin_mode,
			"pair": symbol,
			"leverage": int(current_leverage),
		}

		headers = {
			"Content-Type": "application/json",
			"X-CSRFToken": self._cookies["csrftoken"],
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
			**self._cookie_header(),
		}

		s = self._session()
		async with s.post(self.url_base + "internal/sor/set_leverage", data=json.dumps(payload), headers=headers) as r:
			txt = await r.text()
			try:
				result = json.loads(txt)
				result['leverage'] = leverage
				result['margin_mode'] = margin_mode
				return result
			except Exception:
				return {"status": r.status, "text": txt}

	async def get_leverage_info(self, symbol: str):
		pac_symbol = self._symbol_to_pacifica(symbol)
		meta = self._symbol_meta.get(pac_symbol, {})
		max_lev = meta.get("max_leverage", 1)

		url = f"{PACIFICA_BASE_URL}/account/settings"
		s = self._session()
		params = {"account": self.pacifica_public_key}

		try:
			async with s.get(url, params=params) as r:
				r.raise_for_status()
				data = await r.json()

			settings_list = data.get("data") or []
			for setting in settings_list:
				if setting.get("symbol") == pac_symbol:
					return {
						"symbol": symbol,
						"leverage": setting.get("leverage", max_lev),
						"margin_mode": "isolated" if setting.get("isolated") else "cross",
						"status": "ok",
						"max_leverage": max_lev,
						"available_margin_modes": ["cross", "isolated"],
					}

			# Not found = default settings (cross margin, max leverage)
			return {
				"symbol": symbol,
				"leverage": max_lev,
				"margin_mode": "cross",
				"status": "ok",
				"max_leverage": max_lev,
				"available_margin_modes": ["cross", "isolated"],
			}
		except Exception as e:
			return {
				"symbol": symbol,
				"leverage": None,
				"margin_mode": None,
				"status": "error",
				"available_margin_modes": ["cross", "isolated"],
				"message": str(e),
			}

	async def create_order(
		self,
		symbol: str,
		side: str,
		amount: float,
		price: Optional[float] = None,
		order_type: str = 'market',
		*,
		is_reduce_only: bool = False,
		**kwargs,
	):
		"""Create order via TreadFi API"""
		if not self._has_valid_cookies():
			raise RuntimeError("not logged in: missing session cookies")

		s = self._session()

		# TreadFi strategy IDs
		limit_order_st = "<UUID>"
		market_order_st = "<UUID>"

		if price:
			order_type = "limit"

		if order_type == "market":
			order_st = market_order_st
			st_param = {"reduce_only": is_reduce_only, "ool_pause": False, "entry": False, "max_clip_size": None}
		else:
			if price is None:
				raise ValueError("limit order requires price")
			order_st = limit_order_st
			st_param = {"reduce_only": is_reduce_only, "ool_pause": True, "entry": False, "max_clip_size": None}

		# Adjust amount
		adjusted_amount = self._adjust_amount_lot(symbol, amount)

		payload = {
			"accounts": [self.account_name],
			"base_asset_qty": adjusted_amount,
			"pair": symbol,
			"side": side.lower(),
			"strategy": order_st,
			"strategy_params": st_param,
			"duration": 86400,
			"engine_passiveness": 0.02,
			"schedule_discretion": 0.06,
			"order_slices": 2,
			"alpha_tilt": 0,
		}

		if order_type == "limit":
			adjusted_price = self._adjust_price_tick(symbol, price)
			payload["limit_price"] = adjusted_price

		headers = {
			"Content-Type": "application/json",
			"X-CSRFToken": self._cookies["csrftoken"],
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
			**self._cookie_header(),
		}

		async with s.post(self.url_base + "api/orders/", data=json.dumps(payload), headers=headers) as r:
			txt = await r.text()
			try:
				data = json.loads(txt)
			except Exception:
				data = {"status": r.status, "text": txt}
			return self._parse_orders(data)

	def _parse_orders(self, orders) -> List[Dict[str, Any]]:
		if not orders:
			return []
		if isinstance(orders, dict):
			orders = [orders]

		parsed = []
		for order in orders:
			if not isinstance(order, dict):
				continue
			parsed.append({
				"id": order.get("id"),
				"symbol": order.get("pair"),
				"type": (order.get("super_strategy") or "").lower(),
				"side": (order.get("side") or "").lower(),
				"size": order.get("target_order_qty"),
				"price": order.get("limit_price"),
			})
		return parsed

	async def get_open_orders(self, symbol: str) -> List[Dict[str, Any]]:
		"""Get open orders via TreadFi API"""
		if not self._has_valid_cookies():
			raise RuntimeError("not logged in: missing session cookies")

		s = self._session()
		url = self.url_base + "internal/ems/get_order_table_rows"
		params = {
			"status": "ACTIVE",
			"type": "",
			"page_size": "10",
			"market_type": "spot,perp,future",
			"market_type_filter_exception": "true",
			"pair": symbol,
		}
		headers = {
			"Accept": "application/json",
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
			**self._cookie_header(),
		}

		async with s.get(url, params=params, headers=headers) as r:
			try:
				data = await r.json()
			except Exception:
				text = await r.text()
				raise RuntimeError(f"get_open_orders bad response: {r.status} {text}")

		orders = data.get("orders") or []
		results = []
		for o in orders:
			if not isinstance(o, dict):
				continue
			acc_names = o.get("account_names") or []
			if self.account_name and self.account_name not in acc_names:
				continue

			if o.get("limit_price"):
				results.append({
					"id": o.get("id"),
					"symbol": o.get("pair"),
					"side": (o.get("side") or "").lower() or None,
					"price": o.get("limit_price"),
					"size": o.get("target_order_qty"),
				})

		return results

	async def cancel_orders(self, symbol: str, open_orders: Optional[List] = None) -> List[Dict[str, Any]]:
		"""Cancel orders via TreadFi API"""
		if open_orders is None:
			open_orders = await self.get_open_orders(symbol)
			
		if not open_orders:
			return []

		if open_orders is not None and not isinstance(open_orders, list):
			open_orders = [open_orders]

		if not self._has_valid_cookies():
			raise RuntimeError("not logged in: missing session cookies")

		results = []
		for o in open_orders:
			oid = o.get("id")
			if not oid:
				continue

			s = self._session()
			url = self.url_base + "internal/oms/cancel_order/" + oid
			headers = {
				"Accept": "*/*",
				"X-CSRFToken": self._cookies["csrftoken"],
				"Origin": self.url_base.rstrip("/"),
				"Referer": self.url_base,
				**self._cookie_header(),
			}

			async with s.post(url, json={}, headers=headers) as r:
				try:
					resp = await r.json()
					msg = resp.get("message") or resp.get("detail")

					if msg == "Successfully canceled order.":
						results.append({"id": oid, "status": "SUCCESS", "message": str(msg)})
					else:
						results.append({"id": oid, "status": "FAILED", "message": str(msg)})
				except Exception as e:
					results.append({"id": oid, "status": "FAILED", "message": str(e)})

		return results

	async def close_position(self, symbol: str, position: Optional[Dict] = None, *, is_reduce_only: bool = True):
		"""Close position with market order"""
		return await super().close_position(symbol, position, is_reduce_only=is_reduce_only)
