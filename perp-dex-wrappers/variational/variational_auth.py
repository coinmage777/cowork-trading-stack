import asyncio
import base64
import json
import os
import sys
import time
import webbrowser
from typing import Optional, Dict, Any

from aiohttp import web
from curl_cffi import requests as curl_requests
from pathlib import Path  # [ADDED]

# [NEW] 선택: 로컬 개인키 서명 경로 지원
try:
    from eth_account import Account
    from eth_account.messages import encode_defunct
except Exception:
    Account = None
    encode_defunct = None

# [NEW] 체크섬 주소 처리
try:
    from eth_utils import to_checksum_address
except Exception:
    def to_checksum_address(addr: str) -> str:
        return addr


VARIATIONAL_BASE = "https://omni.variational.io"
LOGIN_URL = f"{VARIATIONAL_BASE}/api/auth/login"
GEN_SIGN_DATA_URL = f"{VARIATIONAL_BASE}/api/auth/generate_signing_data"


class VariationalAuth:
    """
    Variational 로그인 래퍼 (TreadFi 스타일)
    - login(port=7080)  # 브라우저 지갑 서명 플로우
    - login(evm_private_key="0x...", port=None)  # 로컬 개인키 서명 플로우
    """

    def __init__(
        self,
        wallet_address: str,
        evm_private_key: Optional[str] = None,
        session_cookies: Optional[dict] = None,         # [CHANGED] 없으면 .cache 규칙을 따르는 경로를 자동 결정
        http_timeout: float = 30.0,
        impersonate: str = "chrome",
    ):
        if not wallet_address:
            raise ValueError("wallet_address is required")
        self.wallet_address = to_checksum_address(wallet_address)
        self._pk = evm_private_key
        self._http_timeout = http_timeout
        self._impersonate = impersonate

        self.session_cookies = session_cookies # 여기서 vr-token이 있고 올바르다면 사용

        # [CHANGED] 캐시 경로 결정: <프로젝트루트>/.cache → 홈 폴백
        self._session_store_path = self._cache_path()
        self._token: Optional[str] = None
        self._cookie_vr_token: Optional[str] = None
        self._logged_in: bool = False

    # ----------------------------
    # Public API
    # ----------------------------
    async def login(self, port: Optional[int] = None, open_browser: bool = True) -> Dict[str, Any]:
        """
        1) 세션 캐시가 있고 토큰 유효 => 바로 OK
        2) 개인키 제공 => generate_signing_data -> personal_sign -> login
        3) 아니면 로컬 서버(port) 띄우고 브라우저 지갑 서명
        """
        self.load_cached_session()

        if self._token and self._is_token_valid(self._token):
            self._logged_in = True
            return {
                "ok": True,
                "cached": True,
                "token": self._token,
                "cookie": self._cookie_vr_token,
                "message": "cached token valid",
            }

        # 개인키 경로
        if self._pk:
            if Account is None or encode_defunct is None:
                raise RuntimeError("eth-account 미설치: pip install eth-account")
            sign_data = await self._generate_signing_data_async(self.wallet_address)
            message = self._extract_message(sign_data)
            signed_message = self._personal_sign_local(message, self._pk)
            resp = await self._login_request(self.wallet_address, signed_message)
            self._stash_login_response(resp)
            self.save_cached_session()  # [CHANGED]
            return {"ok": True, "method": "private_key", **resp}

        # 브라우저 지갑 경로
        if not port:
            raise ValueError("브라우저 서명 사용 시 port를 지정하세요. 예: port=7080")
        result = await self._browser_login_flow(port, open_browser=open_browser)
        return result

    # ----------------------------
    # HTTP helpers
    # ----------------------------
    async def _generate_signing_data_async(self, address: str) -> Dict[str, Any]:
        """
        Variational endpoint 호출: 서명할 메시지 수신
        """
        addr = to_checksum_address(address)
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "vr-connected-address": addr,  # 중요
        }
        payload = {"address": addr}

        print(f"[variational] calling {GEN_SIGN_DATA_URL} with payload={payload}")
        async with curl_requests.AsyncSession(impersonate=self._impersonate, timeout=self._http_timeout) as s:
            r = await s.post(GEN_SIGN_DATA_URL, json=payload, headers=headers)
            print(f"[variational] response status={r.status_code}, text={r.text[:500] if r.text else 'empty'}")
            r.raise_for_status()
            # API가 plain text(SIWE 메시지)를 직접 반환하는 경우 처리
            try:
                return r.json()
            except Exception:
                # plain text인 경우 {"message": text} 형태로 반환
                return {"message": r.text}

    def _extract_message(self, data: Dict[str, Any]) -> str:
        """
        서버가 주는 다양한 키(message/msg/...)에 대응
        """
        for k in ("message", "msg", "signing_message", "data", "payload"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v
        return json.dumps(data, ensure_ascii=False)

    def _personal_sign_local(self, message: str, private_key: str) -> str:
        """
        MetaMask personal_sign과 동일한 서명
        """
        sign = Account.sign_message(encode_defunct(text=message), private_key=private_key).signature.hex()  # type: ignore
        return sign

    async def _login_request(self, address: str, signed_message: str) -> Dict[str, Any]:
        """
        Variational 로그인 POST
        - 최소 헤더: accept, content-type, vr-connected-address
        - 응답: JSON token + Set-Cookie: vr-token=...
        """
        addr = to_checksum_address(address)
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "vr-connected-address": addr,
        }
        payload = {"address": addr, "signed_message": signed_message}

        async with curl_requests.AsyncSession(impersonate=self._impersonate, timeout=self._http_timeout) as s:
            r = await s.post(LOGIN_URL, json=payload, headers=headers)
            content_type = r.headers.get("content-type", "")
            set_cookie_raw = r.headers.get("set-cookie", "")  # 'vr-token=...; Path=/; ...'
            body = {}
            try:
                if "application/json" in content_type:
                    body = r.json()
                else:
                    body = {"raw": r.text}
            except Exception:
                body = {"raw": r.text}

            if r.status_code >= 400:
                raise RuntimeError(f"login failed: {r.status_code}, body={body}")

            # curl_cffi Response.cookies → dict 유사
            cookie_jar = {}
            try:
                cookie_jar = {k: v for k, v in r.cookies.items()}
            except Exception:
                pass

            return {
                "status": r.status_code,
                "json": body,
                "set_cookie": set_cookie_raw,
                "cookies": cookie_jar,
            }

    # ----------------------------
    # 로컬 웹서버(브라우저 지갑 서명)
    # ----------------------------
    async def _browser_login_flow(self, port: int, open_browser: bool = True) -> Dict[str, Any]:
        """
        127.0.0.1:{port}:
        1) /signing-data?address=... -> 메시지 발급
        2) 프론트에서 personal_sign
        3) /submit -> 서버가 로그인 요청
        """
        login_event = asyncio.Event()
        last_response: Dict[str, Any] = {}

        async def handle_index(_req: web.Request):
            return web.Response(text=self._login_html(self.wallet_address), content_type="text/html")

        async def handle_signing_data(req: web.Request):
            try:
                q = req.rel_url.query
                address = q.get("address") or ""
                print(f"[variational] /signing-data called, address={address}")
                if not address:
                    return web.json_response({"error": "missing address"}, status=400)
                data = await self._generate_signing_data_async(address)
                print(f"[variational] signing data received: {data}")
                msg = self._extract_message(data)
                return web.json_response({"address": to_checksum_address(address), "message": msg, "raw": data})
            except Exception as e:
                import traceback
                print(f"[variational] ERROR in /signing-data: {e}")
                traceback.print_exc()
                return web.json_response({"error": str(e)}, status=500)

        async def handle_submit(req: web.Request):
            nonlocal last_response
            try:
                body = await req.json()
                address = body.get("address")
                signed_message = body.get("signed_message")
                if not (address and signed_message):
                    return web.json_response({"error": "missing address/signed_message"}, status=400)

                resp = await self._login_request(address, signed_message)
                self._stash_login_response(resp)
                self.save_cached_session()  # [CHANGED]
                last_response = {"ok": True, **resp}
                login_event.set()
                return web.json_response(last_response)
            except Exception as e:
                last_response = {"ok": False, "error": str(e)}
                return web.json_response(last_response, status=500)

        app = web.Application()
        app.router.add_get("/", handle_index)              # UI
        app.router.add_get("/signing-data", handle_signing_data)
        app.router.add_post("/submit", handle_submit)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()

        url = f"http://127.0.0.1:{port}"
        print(f"[variational] 브라우저를 열고 {url} 에 접속하여 서명하세요.")
        print(f"[variational] WSL에서 Windows 브라우저 사용 시: http://<WSL_IP>:{port}")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

        await login_event.wait()
        await runner.cleanup()
        return last_response

    # ----------------------------
    # .cache 디렉터리/경로 유틸 (TreadFi와 동일 규칙)  [ADDED]
    # ----------------------------
    def _find_project_root_from_cwd(self) -> Path:
        """
        현재 작업 디렉터리에서 상위로 올라가며 프로젝트 루트를 추정한다.
        마커: pyproject.toml, setup.cfg, setup.py, .git
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
        캐시 베이스 디렉터리 결정: 프로젝트 루트 우선
        """
        return self._find_project_root_from_cwd()

    def _cache_dir(self) -> str:
        """
        최종 캐시 디렉터리:
        - 기본: <프로젝트루트>/.cache
        - 쓰기 실패 시: ~/.cache/mpdex
        """
        base = self._resolve_cache_base()
        target = (base / ".cache")
        try:
            target.mkdir(parents=True, exist_ok=True)
            return str(target)
        except Exception:
            home_fallback = Path.home() / ".cache" / "mpdex"
            home_fallback.mkdir(parents=True, exist_ok=True)
            return str(home_fallback)

    def _cache_path(self) -> str:
        """
        주소별 세션 파일 경로(<base>/.cache/variational_session_{address}.json)
        """
        addr = (self.wallet_address or "default").lower()
        if addr and not addr.startswith("0x"):
            addr = f"0x{addr}"
        safe = addr.replace(":", "_")
        return os.path.join(self._cache_dir(), f"variational_session_{safe}.json")

    def _home_cache_path(self) -> str:
        """
        레거시 홈 캐시(~/.cache/mpdex/variational_session_{address}.json)
        """
        base = Path.home() / ".cache" / "mpdex"
        base.mkdir(parents=True, exist_ok=True)
        addr = (self.wallet_address or "default").lower()
        if addr and not addr.startswith("0x"):
            addr = f"0x{addr}"
        safe = addr.replace(":", "_")
        return str(base / f"variational_session_{safe}.json")

    def cache_path(self) -> str:
        """
        외부에서 캐시 경로 확인용 공개 메서드.  # [ADDED]
        """
        return self._session_store_path

    # ----------------------------
    # 세션/토큰 관리
    # ----------------------------
    def _default_cache_path(self) -> str:
        """
        [DEPRECATED] 이전 방식. 현재는 _cache_path() 사용.
        남겨둔 이유는 외부가 직접 호출할 수 있어서.
        """
        return self._cache_path()  # [CHANGED]

    def save_cached_session(self) -> None:
        """
        공개 저장 메서드 (이전 _save_session_cache 래핑)  # [ADDED]
        """
        self._save_session_cache()

    def get_cached_session(self) -> Optional[Dict[str, Any]]:
        """
        캐시 내용을 반환(유효성은 호출측 판단).  # [ADDED]
        """
        try:
            if os.path.exists(self._session_store_path):
                with open(self._session_store_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            return None
        return None

    def clear_cached_session(self) -> bool:
        """
        캐시 파일 삭제.  # [ADDED]
        """
        try:
            if os.path.exists(self._session_store_path):
                os.remove(self._session_store_path)
                return True
        except Exception:
            pass
        return False

    def _save_session_cache(self) -> None:
        """
        token과 vr-token 쿠키를 캐시
        """
        # [ADDED] 디렉터리 보장
        os.makedirs(os.path.dirname(self._session_store_path), exist_ok=True)

        data = {
            "address": self.wallet_address,
            "token": self._token,
            "cookie_vr_token": self._cookie_vr_token,
            "saved_at": int(time.time()),
        }
        try:
            with open(self._session_store_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[variational] cache save failed: {e}", file=sys.stderr)

    def _load_session_cache(self) -> None:
        """
        내부 로드(유효성까지 반영). 새 경로 → 없으면 홈 폴백(레거시) 순으로 탐색.
        """
        # [CHANGED] 메인 캐시 경로
        primary = self._session_store_path
        legacy = self._home_cache_path()
        paths = [p for p in [primary, legacy] if p]

        for path in paths:
            try:
                if not os.path.exists(path):
                    continue
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("address", "").lower() != self.wallet_address.lower():
                    continue
                tok = data.get("token")
                if tok and self._is_token_valid(tok):
                    self._token = tok
                self._cookie_vr_token = data.get("cookie_vr_token")
                # 레거시에서 읽었다면 즉시 현재 경로로 동기화 저장
                if path == legacy and path != primary:
                    self._session_store_path = primary
                    self._save_session_cache()
                return
            except Exception as e:
                print(f"[variational] cache load failed from {path}: {e}", file=sys.stderr)

    def load_cached_session(self) -> bool:
        """
        공개 로드 메서드. 로드 성공 여부 반환.  # [ADDED]
        """
        before = bool(self._token)
        self._load_session_cache()
        after = bool(self._token)
        return after or before

    def _stash_login_response(self, login_resp: Dict[str, Any]) -> None:
        """
        로그인 응답(JSON token + Set-Cookie) 파싱
        """
        # JSON body
        body = login_resp.get("json") or {}
        token = None
        if isinstance(body, dict):
            token = body.get("token") or body.get("accessToken") or body.get("access_token")
        self._token = token

        # Set-Cookie 헤더에서 vr-token 추출
        set_cookie_raw = login_resp.get("set_cookie") or ""
        self._cookie_vr_token = self._extract_vr_token_from_set_cookie(set_cookie_raw)
        self._logged_in = bool(self._token)

    def _extract_vr_token_from_set_cookie(self, set_cookie_raw: str) -> Optional[str]:
        """
        'vr-token=....; Path=/; HttpOnly; ...' 형태에서 값만 분리
        """
        if not set_cookie_raw:
            return None
        parts = [p.strip() for p in set_cookie_raw.split(";")]
        first = parts[0] if parts else ""
        if first.lower().startswith("vr-token="):
            return first.split("=", 1)[1]
        for seg in set_cookie_raw.split(","):
            seg = seg.strip()
            if seg.lower().startswith("vr-token="):
                return seg.split("=", 1)[1].split(";", 1)[0]
        return None

    def _is_token_valid(self, jwt_token: str, leeway_sec: int = 30) -> bool:
        """
        서명 검증 없이 JWT의 exp 클레임만 확인
        """
        try:
            parts = jwt_token.split(".")
            if len(parts) != 3:
                return False
            payload_b64 = parts[1]
            rem = len(payload_b64) % 4
            if rem:
                payload_b64 += "=" * (4 - rem)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")))
            exp = int(payload.get("exp", 0))
            now = int(time.time())
            return exp > (now + leeway_sec)
        except Exception:
            return False

    # ----------------------------
    # HTML (브라우저 지갑 서명 UI)
    # ----------------------------
    def _login_html(self, default_address: str) -> str:
        """Load login UI HTML from built file (with Reown AppKit for WalletConnect)"""
        # Try to load from built dist file
        module_dir = Path(__file__).parent
        dist_html = module_dir / "variational_login_ui" / "dist" / "index.html"

        if dist_html.exists():
            try:
                return dist_html.read_text(encoding="utf-8")
            except Exception as e:
                print(f"[variational] Failed to load built HTML: {e}")

        # Fallback to minimal HTML if build not available
        return self._login_html_fallback(default_address)

    def _login_html_fallback(self, default_address: str) -> str:
        """Minimal fallback HTML (browser wallet only, no WalletConnect)"""
        da = default_address
        return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Variational Login</title>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<style>
body {{ font-family: system-ui, sans-serif; padding: 24px; max-width: 500px; margin: 0 auto; background: #f5f5f5; }}
.card {{ background: white; padding: 24px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
.row {{ margin: 16px 0; }}
input, textarea {{ width: 100%; padding: 10px; font-size: 14px; font-family: monospace; border: 1px solid #ddd; border-radius: 6px; box-sizing: border-box; }}
textarea {{ height: 100px; resize: vertical; }}
.btn-group {{ display: flex; gap: 8px; flex-wrap: wrap; }}
button {{ padding: 12px 20px; cursor: pointer; font-size: 14px; border: none; border-radius: 8px; font-weight: 500; }}
button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.btn-primary {{ background: #8b5cf6; color: white; }}
.btn-secondary {{ background: #6b7280; color: white; }}
.status {{ padding: 12px; border-radius: 8px; margin-top: 16px; display: none; }}
.status.success {{ background: #d1fae5; color: #065f46; display: block; }}
.status.error {{ background: #fee2e2; color: #991b1b; display: block; }}
.status.info {{ background: #dbeafe; color: #1e40af; display: block; }}
h2 {{ margin: 0 0 20px; color: #1f2937; }}
label {{ font-weight: 500; display: block; margin-bottom: 6px; color: #374151; }}
</style>
</head>
<body>
<div class="card">
<h2>Variational Login</h2>
<div class="row">
    <label>Wallet Address</label>
    <input id="address" type="text" value="{da}" readonly />
</div>
<div class="row">
    <div class="btn-group">
        <button id="connectBtn" class="btn-primary">Connect Wallet</button>
        <button id="prepareBtn" class="btn-secondary" disabled>Get Message</button>
        <button id="signBtn" class="btn-primary" disabled>Sign & Login</button>
    </div>
</div>
<div class="row">
    <label>Message to Sign</label>
    <textarea id="message" readonly placeholder="Connect wallet first..."></textarea>
</div>
<div id="statusBox" class="status"></div>
</div>
<script>
const $ = s => document.querySelector(s);
let signingMessage = null, connectedAddress = null;
const showStatus = (msg, type) => {{ const b = $('#statusBox'); b.textContent = msg; b.className = 'status ' + type; }};
$('#connectBtn').onclick = async () => {{
    try {{
        if (!window.ethereum) throw new Error('No wallet detected');
        const accounts = await window.ethereum.request({{ method: 'eth_requestAccounts' }});
        connectedAddress = accounts[0];
        $('#address').value = connectedAddress;
        $('#prepareBtn').disabled = false;
        $('#connectBtn').disabled = true;
        showStatus('Connected: ' + connectedAddress, 'success');
    }} catch (e) {{ showStatus('Connect failed: ' + e.message, 'error'); }}
}};
$('#prepareBtn').onclick = async () => {{
    try {{
        showStatus('Fetching message...', 'info');
        const params = new URLSearchParams({{ address: connectedAddress }});
        const r = await fetch(`/signing-data?${{params.toString()}}`);
        const j = await r.json();
        if (j.error) throw new Error(j.error);
        signingMessage = j.message;
        $('#message').value = j.message;
        $('#signBtn').disabled = false;
        showStatus('Ready to sign', 'success');
    }} catch (e) {{ showStatus('Failed: ' + e.message, 'error'); }}
}};
$('#signBtn').onclick = async () => {{
    try {{
        if (!connectedAddress || !signingMessage) throw new Error('Connect wallet and get message first');
        const message = $('#message').value;
        showStatus('Please sign in your wallet...', 'info');
        const signature = await window.ethereum.request({{ method: 'personal_sign', params: [message, connectedAddress] }});
        showStatus('Submitting...', 'info');
        const r = await fetch('/submit', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ address: connectedAddress, signed_message: signature }}) }});
        const j = await r.json();
        if (!j.ok) throw new Error(j.error || 'Login failed');
        showStatus('Login successful! You can close this tab.', 'success');
        $('#signBtn').disabled = true;
    }} catch (e) {{ showStatus('Failed: ' + e.message, 'error'); }}
}};
window.addEventListener('load', async () => {{
    if (window.ethereum) {{
        try {{
            const accounts = await window.ethereum.request({{ method: 'eth_accounts' }});
            if (accounts.length > 0) {{
                connectedAddress = accounts[0];
                $('#address').value = connectedAddress;
                $('#prepareBtn').disabled = false;
                $('#connectBtn').disabled = true;
                showStatus('Connected: ' + connectedAddress, 'success');
            }}
        }} catch (e) {{}}
    }}
}});
</script>
</body>
</html>
"""

# ----------------------------
# CLI 실행 지원
# ----------------------------
async def _amain():
    import argparse
    p = argparse.ArgumentParser(description="Variational Login (TreadFi-style)")
    p.add_argument("--address", required=True, help="EVM 주소 (0x...)")
    p.add_argument("--pk", default=None, help="옵션: EVM 개인키(0x...) - 로컬 서명")
    p.add_argument("--port", type=int, default=7080, help="로컬 서버 포트 (브라우저 지갑 경로)")
    p.add_argument("--no-browser", action="store_true", help="자동 브라우저 열기 비활성화")
    p.add_argument("--timeout", type=float, default=30.0, help="HTTP 타임아웃(초)")
    p.add_argument("--print-cache-path", action="store_true", help="캐시 경로 출력")  # [ADDED]
    args = p.parse_args()

    auth = VariationalAuth(
        wallet_address=args.address,
        evm_private_key=args.pk,
        http_timeout=args.timeout,
    )

    if args.print_cache_path:  # [ADDED]
        print(auth.cache_path())

    if args.pk:
        # 개인키 서명 경로
        result = await auth.login(port=None)
    else:
        # 브라우저 지갑 경로
        result = await auth.login(port=args.port, open_browser=not args.no_browser)

    # 결과 요약 출력
    summary = {
        "ok": result.get("ok"),
        "status": result.get("status"),
        "has_token": bool(auth._token),
        "has_cookie_vr_token": bool(auth._cookie_vr_token),
        "cache_path": auth.cache_path(),  # [ADDED]
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    asyncio.run(_amain())


if __name__ == "__main__":
    main()