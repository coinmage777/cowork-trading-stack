"""
Base WebSocket Client
=====================
공통 연결/재연결/ping 로직을 제공하는 추상 베이스 클래스.

사용법:
    class MyWSClient(BaseWSClient):
        WS_URL = "wss://example.com/ws"
        PING_INTERVAL = 30.0

        async def _handle_message(self, data: Dict) -> None:
            ...

        async def _resubscribe(self) -> None:
            ...

        def _build_ping_message(self) -> Optional[str]:
            return json.dumps({"type": "ping"})
"""
import asyncio
import base64
import json
import logging
import os
import random
import socket as socket_module
import ssl
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import websockets
from websockets.client import WebSocketClientProtocol
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, ConnectionClosedError, InvalidStatusCode

# websockets 15+: additional_headers, 13 이하: extra_headers
_WS_HEADER_KEY = "additional_headers" if int(websockets.__version__.split(".")[0]) >= 14 else "extra_headers"

logger = logging.getLogger(__name__)


def _json_dumps(obj: Any) -> str:
    """Compact JSON serialization (no spaces after separators)"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class BaseWSClient(ABC):
    """
    WebSocket 클라이언트 공통 베이스.

    서브클래스에서 구현해야 하는 메서드:
    - _handle_message(data): 메시지 처리
    - _resubscribe(): 재연결 후 구독 복구
    - _build_ping_message(): ping 메시지 생성 (None이면 ping 안 함)
    """

    # 서브클래스에서 override
    WS_URL: str = ""
    WS_CONNECT_TIMEOUT: float = 10.0
    PING_INTERVAL: Optional[float] = None  # None이면 ping 안 함
    PING_FAIL_THRESHOLD: int = 2  # ping 연속 실패 시 재연결
    RECV_TIMEOUT: Optional[float] = None  # 수신 타임아웃 (ping 없는 경우 사용)
    RECONNECT_MIN: float = 1.0
    RECONNECT_MAX: float = 8.0
    CLOSE_TIMEOUT: float = 2.0
    CONNECT_MAX_ATTEMPTS: int = 6  # 429 대응 최대 재시도

    def __init__(self, proxy: Optional[str] = None):
        self._ws: Optional[WebSocketClientProtocol] = None
        self._running: bool = False
        self._recv_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._lock: asyncio.Lock = asyncio.Lock()
        self._reconnecting: bool = False
        self._ping_fail_count: int = 0
        self._last_recv_time: float = 0.0
        self._proxy: Optional[str] = proxy  # HTTP proxy URL (e.g., "http://user:pass@host:port")
        self._extra_headers: Dict[str, str] = {}  # Custom headers for WS connection

    @property
    def connected(self) -> bool:
        """연결 상태 확인"""
        return self._ws is not None and self._running

    @property
    def _log_prefix(self) -> str:
        """로그 prefix (proxy 사용 시 표시)"""
        base = f"[{self.__class__.__name__}]"
        if self._proxy:
            # proxy URL에서 IP만 추출
            parsed = urlparse(self._proxy)
            proxy_ip = parsed.hostname or "proxy"
            return f"{base}[via {proxy_ip}]"
        return base

    async def connect(self) -> bool:
        """
        WebSocket 연결 (429 rate limit 대응 포함).
        서브클래스에서 추가 로직이 필요하면 super().connect() 호출 후 처리.
        """
        async with self._lock:
            if self._ws is not None and self._running:
                return True

        # 429 대응: exponential backoff with jitter
        base_delay = 0.5
        max_delay = 30.0

        for attempt in range(1, self.CONNECT_MAX_ATTEMPTS + 1):
            try:
                # Proxy 사용 시 HTTP CONNECT 터널
                if self._proxy:
                    parsed_ws = urlparse(self.WS_URL)
                    ws_host = parsed_ws.hostname
                    ws_port = parsed_ws.port or (443 if parsed_ws.scheme == "wss" else 80)
                    is_ssl = parsed_ws.scheme == "wss"

                    # proxy URL 파싱
                    parsed_proxy = urlparse(self._proxy)
                    proxy_host = parsed_proxy.hostname
                    proxy_port = parsed_proxy.port or 8080

                    # proxy에 연결
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(proxy_host, proxy_port),
                        timeout=self.WS_CONNECT_TIMEOUT,
                    )

                    # HTTP CONNECT 요청
                    connect_req = f"CONNECT {ws_host}:{ws_port} HTTP/1.1\r\nHost: {ws_host}:{ws_port}\r\n"
                    if parsed_proxy.username:
                        # Basic auth
                        creds = f"{parsed_proxy.username}:{parsed_proxy.password or ''}"
                        auth = base64.b64encode(creds.encode()).decode()
                        connect_req += f"Proxy-Authorization: Basic {auth}\r\n"
                    connect_req += "\r\n"

                    writer.write(connect_req.encode())
                    await writer.drain()

                    # 응답 확인
                    response = await asyncio.wait_for(reader.readline(), timeout=10)
                    if b"200" not in response:
                        writer.close()
                        raise RuntimeError(f"Proxy CONNECT failed: {response.decode().strip()}")
                    # 나머지 헤더 읽기
                    while True:
                        line = await reader.readline()
                        if line == b"\r\n" or line == b"":
                            break

                    # 터널 연결된 소켓에서 websocket 생성
                    # writer의 transport에서 socket 추출 후 detach
                    transport = writer.transport
                    raw_sock = transport.get_extra_info('socket')

                    # fd를 복제해서 새 socket 생성 (원본 닫아도 유지됨)
                    new_fd = os.dup(raw_sock.fileno())
                    new_sock = socket_module.socket(fileno=new_fd)
                    new_sock.setblocking(False)

                    # 원본 transport/streams 정리
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

                    # SSL wrap이 필요하면 직접 처리
                    ssl_context = ssl.create_default_context() if is_ssl else None

                    self._ws = await asyncio.wait_for(
                        websockets.connect(
                            self.WS_URL,
                            sock=new_sock,
                            ssl=ssl_context,
                            server_hostname=ws_host if is_ssl else None,
                            ping_interval=None,
                            ping_timeout=None,
                            close_timeout=5,
                            **({_WS_HEADER_KEY: self._extra_headers} if self._extra_headers else {}),
                        ),
                        timeout=self.WS_CONNECT_TIMEOUT,
                    )
                else:
                    # 일반 연결 (proxy 없음)
                    self._ws = await asyncio.wait_for(
                        websockets.connect(
                            self.WS_URL,
                            ping_interval=None,  # 자체 ping 사용
                            ping_timeout=None,
                            close_timeout=5,
                            **({_WS_HEADER_KEY: self._extra_headers} if self._extra_headers else {}),
                        ),
                        timeout=self.WS_CONNECT_TIMEOUT,
                    )
                self._running = True
                self._recv_task = asyncio.create_task(self._recv_loop())
                if self.PING_INTERVAL is not None:
                    self._ping_task = asyncio.create_task(self._ping_loop())
                logging.getLogger(__name__).debug(f"{self._log_prefix} connected")
                return True

            except InvalidStatusCode as e:
                status = getattr(e, "status_code", None) or getattr(e, "code", None)
                if status != 429:
                    msg = f"{self._log_prefix} connect failed (HTTP {status}): {e}"
                    logger.debug(msg)
                    return False

                # 429: Retry-After 헤더 우선, 없으면 exponential backoff
                headers = getattr(e, "headers", None) or getattr(e, "response_headers", None) or {}
                retry_after = None
                try:
                    ra = headers.get("Retry-After") if hasattr(headers, "get") else None
                    retry_after = float(ra) if ra is not None else None
                except Exception:
                    pass

                if retry_after is None:
                    backoff = min(max_delay, base_delay * (2 ** (attempt - 1)))
                    jitter = random.uniform(0, backoff * 0.2)
                    sleep_for = backoff + jitter
                else:
                    sleep_for = max(0.0, retry_after)

                msg = f"{self._log_prefix} 429 rate limit, retry in {sleep_for:.1f}s (attempt {attempt}/{self.CONNECT_MAX_ATTEMPTS})"
                logger.debug(msg)
                logger.warning(msg)
                await asyncio.sleep(sleep_for)

            except asyncio.TimeoutError:
                msg = f"{self._log_prefix} connect timeout (attempt {attempt}/{self.CONNECT_MAX_ATTEMPTS})"
                logger.debug(msg)
                logger.warning(msg)
                await asyncio.sleep(base_delay)

            except Exception as e:
                msg = f"{self._log_prefix} connect failed: {e}"
                logger.debug(msg)
                logger.error(msg)
                return False

        msg = f"{self._log_prefix} connect failed after {self.CONNECT_MAX_ATTEMPTS} attempts"
        logger.debug(msg)
        logger.error(msg)
        return False

    async def close(self) -> None:
        """연결 종료"""
        self._running = False

        # 태스크 취소
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None

        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        # 소켓 종료 (timeout으로 hang 방지)
        await self._safe_close(self._ws)
        self._ws = None

    async def _safe_close(self, ws: Optional[WebSocketClientProtocol]) -> None:
        """소켓 안전하게 종료 (timeout 적용)"""
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=self.CLOSE_TIMEOUT)
        except Exception:
            pass

    async def _recv_loop(self) -> None:
        """메시지 수신 루프"""
        import time
        self._last_recv_time = time.time()

        while self._running:
            if not self._ws:
                await asyncio.sleep(0.1)
                continue
            try:
                # 수신 타임아웃 적용 (ping 없는 경우 연결 체크용)
                if self.RECV_TIMEOUT:
                    msg = await asyncio.wait_for(self._ws.recv(), timeout=self.RECV_TIMEOUT)
                else:
                    msg = await self._ws.recv()

                self._last_recv_time = time.time()
                self._ping_fail_count = 0  # 메시지 수신 시 ping 실패 카운트 리셋
                data = json.loads(msg)
                await self._handle_message(data)

            except asyncio.TimeoutError:
                # 수신 타임아웃 - 연결 죽은 것으로 간주
                log_msg = f"{self._log_prefix} recv timeout, reconnecting..."
                logger.debug(log_msg)
                logger.warning(log_msg)
                await self._handle_disconnect()
                break
            except (ConnectionClosed, ConnectionClosedOK, ConnectionClosedError) as e:
                log_msg = f"{self._log_prefix} connection closed (code={e.code}), reconnecting..."
                logger.debug(log_msg)
                logger.warning(log_msg)
                await self._handle_disconnect()
                break
            except asyncio.CancelledError:
                break
            except json.JSONDecodeError as e:
                log_msg = f"{self._log_prefix} JSON decode error: {e}, raw msg: {repr(msg[:200]) if len(msg) > 200 else repr(msg)}"
                logger.debug(log_msg)
                logger.warning(log_msg)
                continue  # Skip invalid JSON, don't break
            except Exception as e:
                log_msg = f"{self._log_prefix} recv error: {type(e).__name__}: {e}"
                logger.debug(log_msg)
                logger.error(log_msg, exc_info=True)
                await asyncio.sleep(0.1)

    async def _ping_loop(self) -> None:
        """주기적 ping 전송 (연속 실패 시 재연결)"""
        if self.PING_INTERVAL is None:
            return

        try:
            while self._running:
                await asyncio.sleep(self.PING_INTERVAL)
                if self._ws and self._running:
                    ping_msg = self._build_ping_message()
                    if ping_msg:
                        try:
                            await self._ws.send(ping_msg)
                            # ping 성공 시 카운트 리셋 (pong 응답은 recv_loop에서 처리)
                        except Exception as e:
                            self._ping_fail_count += 1
                            log_msg = f"{self._log_prefix} ping failed ({self._ping_fail_count}/{self.PING_FAIL_THRESHOLD}): {e}"
                            logger.debug(log_msg)
                            logger.warning(log_msg)

                            if self._ping_fail_count >= self.PING_FAIL_THRESHOLD:
                                log_msg = f"{self._log_prefix} ping failed {self.PING_FAIL_THRESHOLD} times, reconnecting..."
                                logger.debug(log_msg)
                                logger.warning(log_msg)
                                self._ping_fail_count = 0
                                await self._handle_disconnect()
                                return  # 이 태스크는 종료, 재연결 시 새로 생성됨
        except asyncio.CancelledError:
            pass

    async def _handle_disconnect(self) -> None:
        """연결 끊김 처리"""
        old_ws = self._ws
        self._ws = None
        await self._safe_close(old_ws)
        await self._reconnect_with_backoff()

    async def _do_reconnect(self) -> bool:
        """
        단일 재연결 시도 (공통 로직).
        Returns: 성공 시 True, 실패 시 False
        """
        try:
            old_ws = self._ws
            self._ws = None
            await self._safe_close(old_ws)

            # 기존 태스크 정리 (취소 완료까지 대기)
            # 현재 실행 중인 태스크는 제외 (자기 자신을 cancel하면 안 됨)
            current_task = asyncio.current_task()

            if self._ping_task and not self._ping_task.done() and self._ping_task is not current_task:
                self._ping_task.cancel()
                try:
                    await self._ping_task
                except asyncio.CancelledError:
                    pass
            self._ping_task = None

            if self._recv_task and not self._recv_task.done() and self._recv_task is not current_task:
                self._recv_task.cancel()
                try:
                    await self._recv_task
                except asyncio.CancelledError:
                    pass
            self._recv_task = None

            self._ws = await asyncio.wait_for(
                websockets.connect(
                    self.WS_URL,
                    ping_interval=None,
                    ping_timeout=None,
                    close_timeout=5,
                    **({_WS_HEADER_KEY: self._extra_headers} if self._extra_headers else {}),
                ),
                timeout=self.WS_CONNECT_TIMEOUT,
            )
            self._recv_task = asyncio.create_task(self._recv_loop())
            if self.PING_INTERVAL is not None:
                self._ping_task = asyncio.create_task(self._ping_loop())

            # 재구독
            await self._resubscribe()
            return True
        except Exception as e:
            msg = f"{self._log_prefix} reconnect failed: {e}"
            logger.debug(msg)
            logger.error(msg)
            return False

    async def _reconnect_with_backoff(self) -> None:
        """Exponential backoff으로 재연결"""
        if self._reconnecting:
            return
        self._reconnecting = True

        delay = self.RECONNECT_MIN
        attempt = 0
        try:
            while self._running:
                attempt += 1
                msg = f"{self._log_prefix} reconnecting in {delay:.1f}s... (attempt {attempt})"
                logger.debug(msg)
                logger.info(msg)
                await asyncio.sleep(delay)

                logging.getLogger(__name__).debug(f"{self._log_prefix} attempting reconnect...")
                if await self._do_reconnect():
                    msg = f"{self._log_prefix} ✓ reconnected successfully"
                    logger.debug(msg)
                    logger.info(msg)
                    return
                else:
                    msg = f"{self._log_prefix} ✗ reconnect attempt {attempt} failed, will retry..."
                    logger.debug(msg)
                    logger.warning(msg)

                delay = min(self.RECONNECT_MAX, delay * 2.0) + random.uniform(0, 0.5)
        finally:
            self._reconnecting = False

    async def _send(self, msg: Dict[str, Any]) -> None:
        """메시지 전송 (연결 안 되어 있으면 연결 시도)"""
        if not self._ws or not self._running:
            await self.connect()
        if self._ws:
            await self._ws.send(json.dumps(msg))

    # ==================== Abstract Methods ====================

    @abstractmethod
    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """
        수신한 메시지 처리.
        서브클래스에서 채널별 dispatch 구현.
        """
        pass

    @abstractmethod
    async def _resubscribe(self) -> None:
        """
        재연결 후 구독 복구.
        이전에 구독했던 채널들을 다시 구독.
        """
        pass

    @abstractmethod
    def _build_ping_message(self) -> Optional[str]:
        """
        Ping 메시지 생성.
        None을 반환하면 ping 전송 안 함.
        예: return json.dumps({"method": "ping"})
        """
        pass
