from typing import Optional, Dict, Any
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP, ROUND_DOWN
import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hyperliquid.xyz"
STABLES = ["USDC","USDT0","USDH","USDE"]
STABLES_DISPLAY = ["USDC","USDT","USDH","USDE"]

# 429 재시도 설정
_RETRY_MAX_ATTEMPTS = 5
_RETRY_BASE_DELAY = 1.0  # 초
_RETRY_MAX_DELAY = 30.0  # 초


async def _post_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    *,
    max_attempts: int = _RETRY_MAX_ATTEMPTS,
    base_delay: float = _RETRY_BASE_DELAY,
    max_delay: float = _RETRY_MAX_DELAY,
) -> tuple[int, Any]:
    """
    POST 요청을 429 에러 시 지수 백오프로 재시도.
    반환: (status_code, json_response or None)
    """
    headers = {"Content-Type": "application/json"}
    delay = base_delay

    for attempt in range(max_attempts):
        try:
            async with session.post(url, json=payload, headers=headers) as r:
                status = r.status

                if status == 429:
                    # Retry-After 헤더 확인
                    retry_after = r.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait_time = float(retry_after)
                        except ValueError:
                            wait_time = delay
                    else:
                        wait_time = delay

                    wait_time = min(wait_time, max_delay)
                    logger.warning(f"[HL] 429 Too Many Requests, retry {attempt + 1}/{max_attempts} in {wait_time:.1f}s...")
                    await asyncio.sleep(wait_time)
                    delay = min(max_delay, delay * 2)
                    continue

                # 성공 또는 다른 에러
                try:
                    resp = await r.json()
                except aiohttp.ContentTypeError:
                    resp = None
                return status, resp

        except aiohttp.ClientError as e:
            logger.warning(f"[HL] Request error: {e}, retry {attempt + 1}/{max_attempts} in {delay:.1f}s...")
            await asyncio.sleep(delay)
            delay = min(max_delay, delay * 2)
            continue

    # 모든 시도 실패
    return 429, None

# ============================================================
# [ADDED] 모듈 레벨 공유 캐시(프로세스 내 1회만 로드)
# ============================================================
_HL_SHARED_CACHE = {
    "inited": False,
    "dex_list": ["hl"],
    "spot_index_to_name": {},
    "spot_name_to_index": {},
    "spot_asset_index_to_pair": {},
    "spot_asset_pair_to_index": {},   # reverse
    "spot_asset_index_to_bq": {},
    "spot_token_sz_decimals": {},
    "perp_metas_raw": [],
    "perp_asset_map": {},             # key -> (asset_id, szDec, maxLev, onlyIsolated, quoteId)
}
_HL_INIT_LOCK = asyncio.Lock()

async def init_shared_hl_cache(session: Optional[aiohttp.ClientSession] = None, *, force: bool = False) -> dict:
    """
    Hyperliquid 공용 메타(dex_list, spot, perp)를 1회만 로드하여 모듈 캐시에 저장.
    - 이미 초기화되었으면 즉시 반환(force=True면 강제 재로드).
    - session이 None이면 내부에서 임시 생성/종료.
    반환: _HL_SHARED_CACHE dict (참조용)
    """
    global _HL_SHARED_CACHE

    async with _HL_INIT_LOCK:
        if _HL_SHARED_CACHE["inited"] and not force:
            #print('use cache')
            return _HL_SHARED_CACHE

        own_session = False
        if session is None:
            session = aiohttp.ClientSession()
            own_session = True

        try:
            # 1) dex_list
            _HL_SHARED_CACHE["dex_list"] = await get_dex_list(session) or ["hl"]

            # 2) spot meta
            await init_spot_token_map(
                session,
                _HL_SHARED_CACHE["spot_index_to_name"],
                _HL_SHARED_CACHE["spot_name_to_index"],
                _HL_SHARED_CACHE["spot_asset_index_to_pair"],
                _HL_SHARED_CACHE["spot_asset_index_to_bq"],
                _HL_SHARED_CACHE["spot_token_sz_decimals"],
            )
            # reverse
            _HL_SHARED_CACHE["spot_asset_pair_to_index"] = {
                v: k for k, v in _HL_SHARED_CACHE["spot_asset_index_to_pair"].items()
            }

            # 3) perp meta
            await init_perp_meta_cache(
                session,
                _HL_SHARED_CACHE["perp_metas_raw"],
                _HL_SHARED_CACHE["perp_asset_map"],
            )

            _HL_SHARED_CACHE["inited"] = True
            
        finally:
            if own_session:
                await session.close()

    return _HL_SHARED_CACHE

def get_shared_hl_cache() -> dict:
    """
    이미 초기화된 공유 캐시를 반환. 초기화 전이면 빈 기본값.
    (동기 컨텍스트에서 참조용)
    """
    return _HL_SHARED_CACHE

def _strip_decimal_trailing_zeros(s: str) -> str:
    """
    문자열 s가 '123.4500'이면 '123.45'로,
    '123.000'이면 '123'으로 변환한다.
    소수점이 없으면(예: '26350') 정수부의 0는 절대 제거하지 않는다.
    """
    if "." in s:
        return s.rstrip("0").rstrip(".")  # comment: 정수부는 건드리지 않음
    return s

def parse_hip3_symbol(sym: str) -> tuple[Optional[str], str]:
    s = str(sym).strip()
    if ":" in s:
        dex, coin = s.split(":", 1)
        return dex.lower().strip(), f"{dex.lower().strip()}:{coin.upper().strip()}"
    return None, s.upper().strip()

def round_to_tick(value: float, decimals: int, up: bool) -> Decimal:
    q = Decimal(f"1e-{decimals}") if decimals > 0 else Decimal("1")
    d = Decimal(str(value))
    return d.quantize(q, rounding=(ROUND_UP if up else ROUND_DOWN))

def format_price(px: float, tick_decimals: int) -> str:
    d = Decimal(str(px))
    # 1) tick에 맞게 반올림
    q = Decimal(f"1e-{max(0,int(tick_decimals))}") if int(tick_decimals) > 0 else Decimal("1")
    d = d.quantize(q, rounding=ROUND_HALF_UP)
    s = format(d, "f")
    if "." not in s:
        return s  # 정수 그대로

    int_part, frac_part = s.split(".", 1)
    int_digits = 0 if int_part in ("", "0") else len(int_part.lstrip("0"))
    sig_digits = (0 if int_part in ("", "0") else int_digits) + len(frac_part)

    # 유효숫자 5 이하면 그대로(소수부 0 제거만)
    if sig_digits <= 5:
        return _strip_decimal_trailing_zeros(s)

    # 2) 유효숫자 5로 축소(소수 자리만 줄임). 여기서도 tick보다 '더 굵은' 자리로만 줄여서 tick 배수 성질은 유지됨.
    allow_frac = max(0, 5 - int_digits)
    allow_frac = min(allow_frac, max(0,int(tick_decimals)))
    q2 = Decimal(f"1e-{allow_frac}") if allow_frac > 0 else Decimal("1")
    d2 = d.quantize(q2, rounding=ROUND_HALF_UP)
    s2 = format(d2, "f")
    return _strip_decimal_trailing_zeros(s2)

def format_size(amount: float, sz_dec: int) -> str:
    if int(sz_dec) > 0:
        q = Decimal(f"1e-{int(sz_dec)}")
        sz_d = Decimal(str(amount)).quantize(q, rounding=ROUND_HALF_UP)
    else:
        sz_d = Decimal(int(round(amount)))
    size_str = format(sz_d, "f")
    # [중요 수정] size도 정수부 0가 잘리지 않도록 소수부가 있을 때만 제거
    return _strip_decimal_trailing_zeros(size_str)

def extract_order_id(raw) -> Optional[str]:
    """
    지원 형태(단순화):
      {'status':'ok','response':{'type':'order','data':{'statuses':[{'resting':{'oid':...}}]}}}
      {'status':'ok','response':{'type':'order','data':{'statuses':[{'filled': {'oid':...,'avgPx':...}}]}}}
      {'status':'ok','response':{'type':'order','data':{'statuses':[{'error':'...'}]}}}
    - 성공 시: oid를 문자열로 반환
    - 실패 시: RuntimeError(error) 발생
    - 매칭 불가 시: None
    """
    # 루트 정규화(list로 감싸져 올 가능성 방어)
    obj = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(obj, dict):
        return None

    # statuses 추출
    try:
        statuses = obj["response"]["data"]["statuses"]
    except Exception:
        return None

    if not isinstance(statuses, list):
        return None

    # 우선 에러 검사 → 성공 oid 추출
    for st in statuses:
        if isinstance(st, dict) and isinstance(st.get("error"), str) and st["error"].strip():
            raise RuntimeError(st["error"].strip())

    for st in statuses:
        if not isinstance(st, dict):
            continue
        for key in ("resting", "filled"):
            node = st.get(key)
            if isinstance(node, dict) and "oid" in node:
                return str(node["oid"])

    return None

# cancel 응답 파서: 성공/오류 판정
def extract_cancel_status(raw) -> bool:
    """
    성공 시 True, 오류 메시지 있으면 RuntimeError(error)를 발생시킵니다.
    """
    def _collect_errors(node, sink: list):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("error", "reason", "message") and isinstance(v, str) and v.strip():
                    sink.append(v.strip())
                elif isinstance(v, (dict, list)):
                    _collect_errors(v, sink)
        elif isinstance(node, list):
            for it in node:
                _collect_errors(it, sink)

    obj = raw[0] if isinstance(raw, list) and raw else raw
    if not isinstance(obj, dict):
        raise RuntimeError("invalid cancel response")

    resp = obj.get("response") or obj
    data = resp.get("data") or {}
    statuses = data.get("statuses")
    # 1) 에러 우선 탐지
    errors = []
    if statuses is not None:
        _collect_errors(statuses, errors)
    if not errors:
        _collect_errors(obj, errors)
    if errors:
        raise RuntimeError(errors[0])

    # 2) 'success' 확인
    if isinstance(statuses, list) and all((isinstance(x, str) and x.lower() == "success") for x in statuses):
        return True

    # 상태가 비어있거나 알 수 없는 형식인 경우도 보수적으로 성공 처리하지 않음
    raise RuntimeError("unknown cancel response")

async def get_dex_list(s: aiohttp.ClientSession):
    url = f"{BASE_URL}/info"
    payload = {"type": "perpDexs"}

    _, resp = await _post_with_retry(s, url, payload)
    if resp is None:
        return ["hl"]  # 기본값

    # 순서 유지 + 중복 제거 + lower 정규화
    order = ["hl"]  # HL 항상 선두
    seen = set(["hl"])
    if isinstance(resp, list):
        for e in resp:
            if not isinstance(e, dict):
                continue
            n = e.get("name")
            if not n:
                continue
            k = str(n).lower().strip()
            if k and k not in seen:
                order.append(k)
                seen.add(k)
    return order

async def init_spot_token_map(s: aiohttp.ClientSession,
                              spot_index_to_name:dict,
                              spot_name_to_index:dict,
                              spot_asset_index_to_pair:dict,
                              spot_asset_index_to_bq:dict,
                              spot_token_sz_decimals:dict,
                              ):
    """
    REST info(spotMeta)를 통해
    - 토큰 인덱스 <-> 이름(USDC, PURR, ...) 맵
    - 스팟 페어 인덱스(spotInfo.index) <-> 'BASE/QUOTE' 및 (BASE, QUOTE) 맵
    을 1회 로드/갱신한다.
    """
    url = f"{BASE_URL}/info"
    payload = {"type": "spotMeta"}

    _, resp = await _post_with_retry(s, url, payload)

    # 안전 가드: dict 응답인지 확인
    if not isinstance(resp, dict):
        spot_index_to_name.clear()
        spot_name_to_index.clear()
        spot_asset_index_to_pair.clear()
        spot_asset_index_to_bq.clear()
        spot_token_sz_decimals.clear()
        return False
    
    tokens = (resp or {}).get("tokens") or []
    universe = (resp or {}).get("universe") or (resp or {}).get("spotInfos") or []

    # 1) 토큰 맵(spotMeta.tokens[].index -> name)
    idx2name: Dict[int, str] = {}
    name2idx: Dict[str, int] = {}
    token_szdec: Dict[str, int] = {}
    for t in tokens:
        if isinstance(t, dict) and "index" in t and "name" in t:
            try:
                idx = int(t["index"])
                name = str(t["name"]).upper().strip()
                szd = int(t.get("szDecimals") or 0)
                if not name:
                    continue
                idx2name[idx] = name
                name2idx[name] = idx
                token_szdec[name] = szd
            except Exception:
                pass
        #print(name,idx)
    spot_index_to_name.update(idx2name)
    spot_name_to_index.update(name2idx)
    spot_token_sz_decimals.update(token_szdec)
    
    # 2) 페어 맵(spotInfo.index -> 'BASE/QUOTE' 및 (BASE, QUOTE))
    pair_by_index: Dict[int, str] = {}
    bq_by_index: Dict[int, tuple[str, str]] = {}
    ok = 0
    fail = 0
    for si in universe:
        if not isinstance(si, dict):
            continue
        # 필수: spotInfo.index
        try:
            s_idx = int(si.get("index"))
        except Exception:
            fail += 1
            continue

        # 우선 'tokens': [baseIdx, quoteIdx] 배열 처리
        base_idx = None
        quote_idx = None
        toks = si.get("tokens")
        if isinstance(toks, (list, tuple)) and len(toks) >= 2:
            try:
                base_idx = int(toks[0])
                quote_idx = int(toks[1])
            except Exception:
                base_idx, quote_idx = None, None

        # 보조: 환경별 키(base/baseToken/baseTokenIndex, quote/...)
        if base_idx is None:
            bi = si.get("base") or si.get("baseToken") or si.get("baseTokenIndex")
            try:
                base_idx = int(bi) if bi is not None else None
            except Exception:
                base_idx = None
        if quote_idx is None:
            qi = si.get("quote") or si.get("quoteToken") or si.get("quoteTokenIndex")
            try:
                quote_idx = int(qi) if qi is not None else None
            except Exception:
                quote_idx = None

        base_name = idx2name.get(base_idx) if base_idx is not None else None
        quote_name = idx2name.get(quote_idx) if quote_idx is not None else None

        # name 필드가 'BASE/QUOTE'면 그대로, '@N' 등인 경우 토큰명으로 합성
        name_field = si.get("name")
        pair_name = None
        if isinstance(name_field, str) and "/" in name_field:
            pair_name = name_field.strip().upper()
            # base/quote 이름 보완
            try:
                b, q = pair_name.split("/", 1)
                base_name = base_name or b
                quote_name = quote_name or q
            except Exception:
                pass
        else:
            if base_name and quote_name:
                pair_name = f"{base_name}/{quote_name}"

        if pair_name and base_name and quote_name:
            pair_by_index[s_idx] = pair_name
            bq_by_index[s_idx] = (base_name, quote_name)
            ok += 1
        else:
            fail += 1
        #print(base_name,quote_name)
    
    spot_asset_index_to_pair.update(pair_by_index)
    spot_asset_index_to_bq.update(bq_by_index)

    return True

async def init_perp_meta_cache(s: aiohttp.ClientSession,
                               perp_metas_raw: dict,
                               perp_asset_map: dict,
                               ) -> bool:
    """
    /info {"type":"allPerpMetas"}를 1회 호출해 런타임 캐시를 만든다.
    - 메인(HL, meta_idx==0):  key='BTC' (대문자), asset_id = local_idx
    - HIP-3(meta_idx>0):      key='dex:COIN' (원문), asset_id = 100000 + meta_idx*10000 + local_idx
    """

    url = f"{BASE_URL}/info"
    payload = {"type": "allPerpMetas"}

    _, metas = await _post_with_retry(s, url, payload)
    if metas is None:
        metas = []

    # 원본 저장
    perp_metas_raw.clear()
    perp_metas_raw.extend(metas if isinstance(metas, list) else [])
    #print(perp_metas_raw)
    
    perp_asset_map.clear()
    try:
        for meta_idx, meta in enumerate(perp_metas_raw):
            
            uni = (meta or {}).get("universe") or []
            collateral_token_id = (meta or {}).get("collateralToken") or 0
            
            for local_idx, a in enumerate(uni):
                #print(meta_idx,local_idx,a)
                if not isinstance(a, dict):
                    continue
                name = a.get("name")
                if not isinstance(name, str) or not name:
                    continue

                if a.get("isDelisted", False):
                    continue
                
                try:
                    szd = int(a.get("szDecimals") or 0)
                except Exception:
                    szd = 0

                try:
                    max_lev = int(a.get("maxLeverage") or 1)
                except Exception:
                    max_lev = 1

                try:
                    isolated = int(a.get("onlyIsolated") or False)
                except Exception:
                    isolated = False
                
                if meta_idx == 0:
                    key = name.upper()                 # 메인(HL)
                    original_name = name               # 오더북 구독시 case-sensitive 하므로 원본 보존
                    asset_id = int(local_idx)
                else:
                    key = name                         # HIP-3: 'dex:COIN'
                    original_name = name               # HIP-3도 원본 보존
                    asset_id = 100000 + meta_idx * 10000 + local_idx

                # (asset_id, szd, max_lev, isolated, collateral_token_id, original_name)
                perp_asset_map[key] = (asset_id, szd, max_lev, isolated, collateral_token_id, original_name)
    except Exception as e:
        logger.error(e)

    return True