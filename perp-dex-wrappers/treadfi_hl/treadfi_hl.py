from mpdex.utils.hyperliquid_base import HyperliquidBase
from eth_account import Account
from .mpdex_hl_sign_bridge import sign_l1_action as hl_sign_l1_action, sign_user_signed_action as hl_sign_user_signed_action
import aiohttp
from aiohttp import web
import asyncio
import json
import os
import time
from pathlib import Path
from typing import Optional, Dict
from eth_account import Account
from eth_account.messages import encode_defunct

class TreadfiHlExchange(HyperliquidBase):
	USD_CLASS_TRANSFER_TYPES = [
		{"name": "hyperliquidChain", "type": "string"},
		{"name": "amount", "type": "string"},
		{"name": "toPerp", "type": "bool"},
		{"name": "nonce", "type": "uint64"},
	]
	def __init__(self, session_cookies=None, login_wallet_address=None, login_wallet_private_key=None, trading_wallet_address=None, account_name=None, trading_wallet_private_key=None, options=None):
		super().__init__(
			wallet_address=trading_wallet_address or login_wallet_address,
		)
		self.login_wallet_address = login_wallet_address
		self.trading_wallet_address = trading_wallet_address or login_wallet_address
		self.account_name = account_name
		self.account_id = None

		self.url_base = "https://app.tread.fi/"
		self._logged_in = False
		self._cookies = session_cookies or {}
		self._login_pk = login_wallet_private_key
		self._login_event: Optional[asyncio.Event] = None

		self.trading_wallet_private_key = trading_wallet_private_key # only for transfer
		
		self.options = options

		self.login_html_path = os.environ.get(
			"TREADFI_LOGIN_HTML",
			os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "wrappers/", "treadfi_login.html")),
		)
		
		# 쿠키 유효성 정리: "", None 은 없는 것으로 간주
		self._normalize_or_clear_cookies()  # 빈 문자열/None -> 제거

		# 세션 쿠키가 유효하지 않다면 로컬 캐시에서 우선 로드
		if not self._has_valid_cookies():
			print("No cookies are in given. Checking cached cookies in local dir..")
			self._load_cached_cookies()

	async def _make_signed_payload(self, action: dict) -> dict:
		# Tread.fi는 HL 직접 서명을 사용하지 않음 → NotImplementedError 유지
		raise NotImplementedError("TreadfiHl uses its own API for orders")
	
	async def init(self):
		login_md = await self.login()
		if not self._logged_in:
			raise RuntimeError(f"not logged-in {login_md}")
		
		self.account_id = await self.get_account_id()
		return await super().init()
		# ----------------------------
	# HTML (브라우저 지갑 서명 UI)
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
				print(f"[treadfi_hl] Failed to load built HTML: {e}")

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
		"""
		1) session cookies가 있을경우 get_user_metadata 를 통해 정상 session인지 확인, 아닐시 2)->3)
		2) private key가 있을경우 msg 서명 -> signature 생성 -> session cookies udpate
		3) 1, 2둘다 없을 경우 webserver를 간단히 구동하여 msg 서명하게함 -> 서명시 signature 값 받아서 -> session cookies update
		2) or 3)의 경우도 get_user_metadata를 통해 data 확인후 "is_authenticated" 가 True면 login 확정
		서명 메시지
		"Sign in to Tread with nonce: {nonce}"
		"""

		# 1) 캐시/입력 쿠키로 바로 검증
		if self._has_valid_cookies():
			md = await self._get_user_metadata()
			if md.get("is_authenticated"):
				print("Login authenticated!")
				self._logged_in = True
				self._save_cached_cookies()
				return md
			else:
				print(f"Cache is outdated...{md}")
				print(f"Auto redirecting to PK signing or Web signing")
		
		# 2) 프라이빗키로 서명
		if self._login_pk:
			if Account is None or encode_defunct is None:
				raise RuntimeError("eth_account 미설치. pip install eth-account")
			nonce = await self._get_nonce()
			msg = f"Sign in to Tread with nonce: {nonce}"
			acct = Account.from_key(self._login_pk)
			sign = Account.sign_message(encode_defunct(text=msg), private_key=self._login_pk).signature.hex()
			await self._wallet_auth(acct.address, sign, nonce)
			md = await self._get_user_metadata()
			if not md.get("is_authenticated"):
				raise RuntimeError("login failed with private key")
			else:
				print("Login authenticated!")
			self._logged_in = True
			self._save_cached_cookies()
			return md

		# 3) 브라우저 서명 (포트 6974)
		self._login_event = asyncio.Event()

		async def handle_index(_req: web.Request):
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
				# 프론트에서 alert로 보는 메시지
				return web.Response(status=500, text=f"submit error: {e}")

		app = web.Application()
		app.router.add_get("/", handle_index)
		app.router.add_get("/nonce", handle_nonce)
		app.router.add_post("/submit", handle_submit)

		runner = web.AppRunner(app)
		await runner.setup()
		site = web.TCPSite(runner, "127.0.0.1", 6974)
		await site.start()

		print("[treadfi_hl] Open http://127.0.0.1:6974 in your browser to sign the message")
		await self._login_event.wait()
		await runner.cleanup()

		md = await self._get_user_metadata()
		if not md.get("is_authenticated"):
			raise RuntimeError("login failed after browser sign")
		else:
			print("Login authenticated!")
		self._logged_in = True
		self._save_cached_cookies()
		return md

	def _addr_lower(self, address: str) -> str:  
		return "0x" + address[2:].lower()

	async def _get_nonce(self) -> str:  
		s = self._session()
		headers = {"Origin": self.url_base.rstrip("/"), "Referer": self.url_base, **self._cookie_header()}
		async with s.get(self.url_base + "internal/account/get_nonce/", headers=headers) as r:
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
		headers = {"Origin": self.url_base.rstrip("/"), "Referer": self.url_base, "Content-Type": "application/json"}
		async with s.post(self.url_base + "internal/account/wallet_auth/", json=payload, headers=headers) as r:
			text = await r.text()
			if r.status != 200:
				raise RuntimeError(f"wallet_auth failed: {r.status} {text}")

			# Set-Cookie 직접 수집 (redirect 포함)
			def _collect(resp: aiohttp.ClientResponse):
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

	# ---------------------------
	# 로컬 캐시 유틸
	# ---------------------------
	def _find_project_root_from_cwd(self) -> Path:
		"""
		현재 작업 디렉터리에서 시작해 상위로 올라가며
		프로젝트 루트(marker 파일/폴더) 후보를 탐색한다.
		"""
		markers = {"pyproject.toml", "setup.cfg", "setup.py", ".git"}
		p = Path.cwd().resolve()
		try:
			for parent in [p] + list(p.parents):
				for name in markers:
					if (parent / name).exists():
						return parent
		except Exception:
			pass
		return Path.cwd().resolve()

	def _resolve_cache_base(self) -> Path:
		"""
		캐시 베이스 디렉터리 결정 순서:
		CWD 기준 프로젝트 루트(상위로 스캔)
		"""
		# CWD에서 프로젝트 루트 추정
		return self._find_project_root_from_cwd()

	def _cache_dir(self) -> str:
		"""
		최종 캐시 디렉터리 경로 반환.
		- 기본: <프로젝트루트>/.cache
		- 쓰기 권한 실패 시: ~/.cache/mpdex
		"""
		# [CHANGED] 패키지 폴더 기준 → 프로젝트 루트 기준으로 변경
		base = self._resolve_cache_base()
		target = (base / ".cache")

		try:
			target.mkdir(parents=True, exist_ok=True)
			return str(target)
		except Exception:
			# 권한/컨테이너 등 이슈 시 사용자 홈 캐시로 fallback
			home_fallback = Path.home() / ".cache" / "mpdex"
			home_fallback.mkdir(parents=True, exist_ok=True)
			return str(home_fallback)

	def _cache_path(self) -> str:
		# signing for mainwallet
		addr = (self.login_wallet_address or "default").lower()
		if addr and not addr.startswith("0x"):
			addr = f"0x{addr}"
		safe = addr.replace(":", "_")
		return os.path.join(self._cache_dir(), f"treadfi_session_{safe}.json")

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
			if not (self._cookies.get("csrftoken") and self._cookies.get("sessionid")):
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

	# ---------------------------
	# 쿠키 유효성/정리 헬퍼
	# ---------------------------
	def _has_valid_cookies(self, cookies: Optional[Dict[str, str]] = None) -> bool:
		"""
		True if csrftoken, sessionid 둘 다 존재하고 빈 문자열/None이 아님.
		"""
		c = (cookies if cookies is not None else self._cookies) or {}
		ct = c.get("csrftoken")
		sid = c.get("sessionid")
		return bool(ct) and bool(sid)

	def _normalize_or_clear_cookies(self) -> None:
		"""
		빈 문자열("")/None은 제거하여 '없는 것'으로 처리.
		"""
		c = self._cookies or {}
		ct = c.get("csrftoken")
		sid = c.get("sessionid")
		# 둘 중 하나라도 비어 있으면 전체를 비움(부분 유효는 의미 없음)
		if not ct or not sid:
			self._cookies = {}

	def _cookie_header(self) -> Dict[str, str]:
		# 모든 요청에서 Cookie 헤더를 직접 구성
		if not self._has_valid_cookies():
			return {}
		return {"Cookie": f"csrftoken={self._cookies['csrftoken']}; sessionid={self._cookies['sessionid']}"}

	async def _get_user_metadata(self) -> dict:
		s = self._session()
		headers = {"Origin": self.url_base.rstrip("/"), "Referer": self.url_base, **self._cookie_header()}
		async with s.get(self.url_base + "internal/account/user_metadata/", headers=headers) as r:
			# 상태/본문을 그대로 반환(디버그가 쉬움)
			try:
				data = await r.json()
			except Exception:
				text = await r.text()
				raise RuntimeError(f"user_metadata bad response: {r.status} {text}")
			return data

	async def logout(self):
		if not self._has_valid_cookies():
			self._clear_cached_cookies()
			return {"ok": True, "detail": "already logged out"}

		s = self._session()
		url = self.url_base + "account/logout/"

		headers = {
			"X-CSRFToken": self._cookies.get("csrftoken", ""),   # <- CSRF 헤더
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,                  # <- .dev/treadfi_logout.py와 동일(루트)
			"Content-Type": "application/json",
			"Accept": "*/*",
			"User-Agent": "Mozilla/5.0",
			**self._cookie_header(),                             # <- 쿠키는 수동 첨부
		}

		# 본문 없이 POST, 리다이렉트 미추적
		async with s.post(url, headers=headers, allow_redirects=False) as r:
			text = await r.text()
			status = r.status

		# 성공으로 간주할 범위(로그아웃은 종종 302로 리다이렉트됨)
		success_status = {200, 302}
		# Django 계열이 이미 세션을 끊은 후 403 CSRF HTML을 보내는 경우도 성공으로 간주
		if status in success_status or ("CSRF verification failed" in text and status == 403):  # [ADDED]
			result = {"ok": True, "status": int(status), "text": text}
		else:
			result = {"ok": False, "status": int(status), "text": text}

		# 세션/쿠키 정리는 로컬에서 항상 수행
		self._logged_in = False
		self._cookies = {}
		self._clear_cached_cookies()
		# (선택) 세션을 여기서 닫고 싶다면 aclose가 있다면 호출
		if hasattr(self, "aclose"):
			await self.aclose()
		return result
	
	async def get_account_id(self) -> str | None:
		"""
		GET https://app.tread.fi/internal/sor/get_cached_account_balance
		- query: account_names=<self.account_name>
		- 응답의 balances[*].account_name == self.account_name 인 항목의 account_id 반환
		- 없으면 None
		"""
		if not self._has_valid_cookies():
			raise RuntimeError("not logged in: missing session cookies")

		if not self.account_name:
			raise ValueError("self.account_name is empty")

		s = self._session()
		url = self.url_base + "internal/sor/get_cached_account_balance"

		params = {"account_names": self.account_name}
		headers = {
			"Accept": "*/*",
			"X-CSRFToken": self._cookies["csrftoken"],
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
			**self._cookie_header(),  # comment: 세션 쿠키 포함
		}

		async with s.get(url, params=params, headers=headers) as r:
			r.raise_for_status()
			# comment: content-type이 json이 아닐 수도 있어 예외 처리
			try:
				data = await r.json()
			except Exception:
				text = await r.text()
				raise RuntimeError(f"get_account_id: unexpected response ({r.status}): {text[:200]}")

		# 기대 구조: {"balances":[{...,"account_name":"...", "account_id":"..."}], ...}
		balances = data.get("balances") or []
		if not isinstance(balances, list):
			return None

		for item in balances:
			if not isinstance(item, dict):
				continue
			if item.get("account_name") == self.account_name:
				acc_id = item.get("account_id")
				if isinstance(acc_id, str) and acc_id:
					return acc_id

		return None

	def get_perp_quote(self, symbol, need_to_convert=False): # default is False
		if need_to_convert:
			raw = str(self._symbol_convert_for_ws(symbol)).strip()
		else:
			raw = str(symbol).strip()
		return super().get_perp_quote(raw)

	def _symbol_convert_for_ws(self, symbol: str) -> str:
		"""
		Tread.fi 심볼 → WS/HL 심볼 변환
		- perp: BTC:PERP-USDC → BTC, xyz_XYZ100:PERP-USDC → xyz:XYZ100
		- spot: BTC-USDC → BTC/USDC
		"""
		if ':' in symbol:
			# perp
			front = symbol.split(':')[0]
			dex = f"{front.split('_')[0]}:" if '_' in front else ""
			base = front.split('_')[1] if '_' in front else front
			return f"{dex}{base}"
		else:
			# spot
			base, quote = symbol.split('-')[0], symbol.split('-')[1]
			return f"{base}/{quote}"
	
	async def update_leverage(self, symbol, leverage=None, margin_mode=None):
		"""
		Update leverage and/or margin mode via TreadFi API.

		Args:
			leverage: If None, only margin_mode is updated (current leverage is preserved).
			margin_mode: If None, only leverage is updated (current margin_mode is preserved).

		Note: At least one of leverage or margin_mode should be provided.
		"""
		if leverage is None and margin_mode is None:
			return {"status": "error", "message": "At least one of leverage or margin_mode must be provided"}

		symbol_ws = self._symbol_convert_for_ws(symbol)
		_, _, max_leverage, only_isolated, _, _ = self.perp_asset_map.get(symbol_ws, (None, None, 1, False, 0, None))

		# If one of leverage/margin_mode is None, get current settings to preserve it
		current_leverage = leverage
		current_margin_mode = margin_mode
		if leverage is None or margin_mode is None:
			current_info = await self.get_leverage_info(symbol)
			if current_info.get("status") == "ok":
				if leverage is None:
					current_leverage = current_info.get("leverage", max_leverage or 1)
				if margin_mode is None:
					current_margin_mode = current_info.get("margin_mode", "cross")
			else:
				# Fallback defaults
				if leverage is None:
					current_leverage = max_leverage or 1
				if margin_mode is None:
					current_margin_mode = "cross"

		# Determine actual margin mode (handle only_isolated)
		requested_mode = current_margin_mode.upper()
		forced_msg = None
		if only_isolated and requested_mode == "CROSS":
			actual_margin_mode = "ISOLATED"
			forced_msg = "symbol only supports isolated, forced from cross to isolated"
		else:
			actual_margin_mode = requested_mode

		payload = {
			"account_ids": [self.account_id],
			"margin_mode": actual_margin_mode,
			"pair": symbol,
			"leverage": int(current_leverage)
		}

		headers = {
			"Content-Type": "*/*",
			"X-CSRFToken": self._cookies["csrftoken"],
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
			**self._cookie_header(),
		}

		s = self._session()
		async with s.post(self.url_base + "internal/sor/set_leverage", data=json.dumps(payload), headers=headers) as r:
			txt = await r.text()
			try:
				data = json.loads(txt)
			except Exception:
				data = {"status": r.status, "text": txt}

			if data.get("message") == "Leverage changed successfully.":
				data["status"] = "ok"
				data["leverage"] = leverage
				data["margin_mode"] = margin_mode
			if forced_msg:
				data["message"] = forced_msg
			return data

	async def get_leverage_info(self, symbol: str):
		symbol_ws = self._symbol_convert_for_ws(symbol)
		return await super().get_leverage_info(symbol_ws)

	async def get_position(self, symbol: str):
		symbol_ws = self._symbol_convert_for_ws(symbol)
		return await super().get_position(symbol_ws)

	async def get_mark_price(self, symbol: str, *, is_spot: bool = False):
		symbol_ws = self._symbol_convert_for_ws(symbol)
		raw = str(symbol_ws).strip()
		if "/" in raw:
			is_spot = True
		return await super().get_mark_price(symbol_ws, is_spot=is_spot)

	def parse_orders(self, orders):
		if not orders:
			return []

		# 단일 dict일 경우 리스트로 감싸기
		if isinstance(orders, dict):
			orders = [orders]

		parsed = []
		for order in orders:
			parsed.append({
				"id": order.get("id"),
				"symbol": order.get("pair"),
				"type": order.get("super_strategy").lower(),
				"side": order.get("side").lower(),
				"size": order.get("target_order_qty"),
				"price": order.get("limit_price")
			})

		return parsed

	async def create_order(self, symbol, side, amount, price=None, order_type='market', *, is_reduce_only=False):
		"""
		symbol: 변환돼서 들어옴
		side: 'buy' | 'sell'
		amount: base_asset_qty (예: 0.002)
		price: limit 주문시에만 사용
		order_type: 'market' | 'limit'
		"""
		#if not self._logged_in:
		#print("Need login...")
		#await self.login()
		if not self._has_valid_cookies():
			raise RuntimeError("not logged in: missing session cookies")

		s = self._session()

		# 전략 ID 간단 상수
		limit_order_st = "<UUID>"
		market_order_st = "<UUID>"

		if price:
			order_type = "limit" # auto redirecting to limit order

		if order_type == "market":
			order_st = market_order_st
			st_param = {"reduce_only": is_reduce_only, "ool_pause": False, "entry": False, "max_clip_size": None}
		else:
			if price is None:
				raise ValueError("limit order requires price")
			order_st = limit_order_st
			st_param = {"reduce_only": is_reduce_only, "ool_pause": True, "entry": False, "max_clip_size": None}

		payload = {
			"accounts": [self.account_name],
			"base_asset_qty": amount,
			"pair": symbol,
			"side": side,
			"strategy": order_st,
			"strategy_params": st_param,
			"duration": 86400,
			"engine_passiveness": 0.02,
			"schedule_discretion": 0.06,
			"order_slices": 2,
			"alpha_tilt": 0,
		}
		
		if order_type == "limit":
			payload["limit_price"] = price
		
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
			return self.parse_orders(data)

	async def get_orderbook(self, symbol: str, timeout: float = 5.0):
		symbol_to_hl = self._symbol_convert_for_ws(symbol)
		return await super().get_orderbook(symbol_to_hl, timeout=timeout)

	async def get_open_orders(self, symbol):
		"""
		GET https://app.tread.fi/internal/ems/get_order_table_rows

		쿼리:
		  - status=ACTIVE
		  - type=
		  - page_size=10 (필요 시 조정 가능)
		  - market_type=spot,perp,future
		  - market_type_filter_exception=true
		  - pair=<symbol>  # 인자로 받은 symbol을 그대로 사용

		반환: [{"id": str, "pair": str, "side": "buy"|"sell", "limit_price": str|None}, ...]
		"""
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
			"pair": symbol,  # 그대로 사용
		}
		headers = {
			"Accept": "application/json",
			"Origin": self.url_base.rstrip("/"),
			"Referer": self.url_base,
			**self._cookie_header(),
		}

		async with s.get(url, params=params, headers=headers) as r:
			# JSON 파싱 실패 시 에러 메시지 노출
			try:
				data = await r.json()
			except Exception:
				text = await r.text()
				raise RuntimeError(f"get_open_orders bad response: {r.status} {text}")
		
		orders = data.get("orders") or []
		if not isinstance(orders, list):
			return []
		
		# account_names에 self.account_name이 포함된 주문만 파싱
		results = []
		for o in orders:
			if not isinstance(o, dict):
				continue
			acc_names = o.get("account_names") or []
			if self.account_name and self.account_name not in acc_names:
				continue
			
			if o.get("limit_price"): # only if there is limit_price info
				results.append({
					"id": o.get("id"),
					"symbol": o.get("pair"),
					"side": (o.get("side") or "").lower() or None,
					"price": o.get("limit_price"),  # 문자열 또는 None
					"size": o.get("target_order_qty"),
				})

		return results

	async def cancel_orders(self, symbol, open_orders = None):
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
			oid = o.get("id",None)

			s = self._session()
			if oid:
				url = self.url_base + "internal/oms/cancel_order/"+oid
				headers = {
					"Accept": "*/*",
					"X-CSRFToken": self._cookies["csrftoken"],
					"Origin": self.url_base.rstrip("/"),
					"Referer": self.url_base,
					**self._cookie_header(),
				}
				payload = {}
				s = self._session()
				async with s.post(url, json=payload, headers=headers) as r:
					status = r.status
					try:
						resp = await r.json()
						msg = resp.get("message") or resp.get("detail")
						
						if msg == "Successfully canceled order.":
							results.append({
								"id": oid,
								"status": "SUCCESS",
								"message": str(msg)
							})
						else:
							results.append({
								"id": oid,
								"status": "FAILED",
								"message": str(msg)
							})

					except Exception as e:
						results.append({
							"id": oid,
							"status": "FAILED",
							"message": str(e)
						})
		return results
	
	'''
	async def transfer_to_perp(self, amount):
		raise RuntimeError("Tread.fi는 내부전송 미지원")

	async def transfer_to_spot(self, amount):
		raise RuntimeError("Tread.fi는 내부전송 미지원")
	'''
	
	def _get_wallet(self, *, for_user_action: bool = False):
		"""
		서명용 wallet 객체 반환.
		- for_user_action=True: User Signed Action은 반드시 실제 wallet으로 서명해야 함
		- for_user_action=False: 일반 L1 액션은 agent 또는 wallet 사용
		"""
		priv = self.trading_wallet_private_key
	
		if not priv:
			raise RuntimeError("trading_wallet_private_key가 필요합니다 (User Signed Action)")

		priv_clean = priv[2:] if priv.startswith("0x") else priv
		wallet = Account.from_key(bytes.fromhex(priv_clean))
		
		# private key로 파생된 주소
		derived_address = wallet.address.lower()
		
		# trading_wallet_address 정규화
		trading_addr = (self.trading_wallet_address or "").lower()
		if trading_addr and not trading_addr.startswith("0x"):
			trading_addr = f"0x{trading_addr}"
		
		# 주소가 다르면 vault_address로 설정
		if trading_addr and derived_address != trading_addr:
			self.vault_address = self.trading_wallet_address
			# wallet_address도 파생된 주소로 업데이트 (서명 주체)
			self.wallet_address = wallet.address
		
		#print(self.vault_address)
		#print(self.wallet_address)
		#return

		return wallet
	
	async def _make_transfer_payload(self, action: dict) -> dict:
		"""
		usdClassTransfer 전용 (sign_user_signed_action)
		- action에는 이미 type, amount, toPerp, nonce가 들어있음
		- signatureChainId, hyperliquidChain은 sign_user_signed_action에서 삽입
		"""
		wallet = self._get_wallet()

		nonce = action.get("nonce") or int(time.time() * 1000)
		action["nonce"] = nonce

		# vault_address가 설정되었으면 amount에 subaccount 추가
		raw_amount = action.get("amount", "")
		print(raw_amount)
		# 이미 subaccount가 포함되어 있지 않은 경우에만 추가
		if self.vault_address and "subaccount:" not in raw_amount:
			action["amount"] = f"{raw_amount} subaccount:{self.vault_address}"

		sig = hl_sign_user_signed_action(
			wallet=wallet,
			action=action,
			payload_types=self.USD_CLASS_TRANSFER_TYPES,
			primary_type="HyperliquidTransaction:UsdClassTransfer",
			is_mainnet=True,
		)

		nonce = action.get("nonce") or int(time.time() * 1000)
		payload = {"action": action, "nonce": nonce, "signature": sig}
		#if self.vault_address:
		#	payload["vaultAddress"] = self.vault_address
		return payload