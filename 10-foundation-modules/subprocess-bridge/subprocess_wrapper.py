"""
Subprocess Exchange Wrapper (격리 venv JSON-RPC bridge)
========================================================
별도 venv에서 실행되는 거래소 브릿지 프로세스와 JSON-RPC로 통신하는 래퍼.

언제 쓰나:
  - 거래소 SDK들이 서로 의존성 충돌 (lighter는 grpcio 1.x, GRVT는 2.x)
  - 한 SDK가 sys.exit()을 부르거나 sigwait을 깔끔하게 정리하지 못함
  - SDK 업데이트 시 봇 전체를 재배포하지 않고 venv만 갈아끼우고 싶음

해결:
  거래소별 venv (lighter_venv/, grvt_venv/) + 자식 프로세스 + stdin/stdout JSON-RPC.
  부모는 표준 인터페이스 (get_mark_price, create_order, ...)만 알면 됨.

자식 측 (`exchange_bridge.py`)은 본 모듈에 포함되지 않음 — 사용자가 거래소 SDK
래핑을 작성해야 함. 본 모듈은 부모(orchestrator) 측만 제공.

사용법:
    wrapper = SubprocessExchangeWrapper(
        venv_python="lighter_venv/bin/python",
        exchange="lighter",
        config_path="config.yaml",
        account="lighter_main",
    )
    await wrapper.start()
    price = await wrapper.get_mark_price("BTC")
    await wrapper.close()
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SubprocessExchangeWrapper:
    """
    자식 프로세스의 exchange_bridge.py와 JSON-RPC로 통신하는 래퍼.
    """

    def __init__(
        self,
        venv_python: str,
        exchange: str,
        config_path: str,
        account: Optional[str] = None,
        display_name: Optional[str] = None,
        bridge_module: str = "strategies.exchange_bridge",
        project_dir: Optional[str] = None,
    ):
        self.venv_python = venv_python
        self.exchange = exchange
        self.config_path = config_path
        self.account = account or exchange
        self.display_name = display_name or exchange
        self.bridge_module = bridge_module
        self.project_dir = project_dir or str(Path(__file__).resolve().parent.parent)

        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._consecutive_timeouts = 0
        self._restart_lock = asyncio.Lock()
        self._restarting = False

    async def _kill_zombie_bridges(self):
        """같은 exchange의 기존 브릿지 좀비 프로세스를 kill."""
        if sys.platform == "win32":
            try:
                proc = await asyncio.create_subprocess_exec(
                    "powershell.exe", "-Command",
                    f"Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -match 'exchange_bridge.*--exchange {self.exchange}' }} | "
                    f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.wait(), timeout=10)
                logger.debug(f"[{self.display_name}] 좀비 브릿지 정리 완료")
            except Exception as e:
                logger.debug(f"[{self.display_name}] 좀비 정리 스킵: {e}")
        else:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pkill", "-f", f"exchange_bridge.*--exchange {self.exchange}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.wait(), timeout=10)
                logger.debug(f"[{self.display_name}] 좀비 정리 완료 (Unix)")
            except Exception as e:
                logger.debug(f"[{self.display_name}] 좀비 정리 스킵 (Unix): {e}")

    async def start(self):
        """브릿지 프로세스 시작 및 초기화."""
        await self._kill_zombie_bridges()

        cmd = [
            self.venv_python,
            "-m", self.bridge_module,
            "--exchange", self.exchange,
            "--config", self.config_path,
            "--account", self.account,
        ]
        logger.info(f"[{self.display_name}] 브릿지 프로세스 시작: {' '.join(cmd)}")

        # Windows: Ctrl+C가 자식에 전달되지 않게 격리
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.project_dir,
            env={**os.environ, "PYTHONPATH": self.project_dir},
            **kwargs,
        )

        asyncio.create_task(self._read_stderr())
        self._reader_task = asyncio.create_task(self._read_responses())

        result = await self._call("init")
        if result != "ok":
            raise RuntimeError(f"[{self.display_name}] 브릿지 초기화 실패: {result}")

        logger.info(f"[{self.display_name}] 브릿지 프로세스 준비 완료")
        return self

    async def _read_stderr(self):
        try:
            while self._process and self._process.stderr:
                line = await self._process.stderr.readline()
                if not line:
                    break
                msg = line.decode("utf-8", errors="replace").strip()
                if msg:
                    logger.info(f"[{self.display_name}:bridge] {msg}")
        except Exception:
            pass

    async def _read_responses(self):
        """stdout에서 JSON-RPC 응답 읽기. 비-JSON 라인은 무시 (SDK print 오염 방지)."""
        try:
            while self._process and self._process.stdout:
                line = await self._process.stdout.readline()
                if not line:
                    logger.warning(f"[{self.display_name}] 브릿지 stdout 종료")
                    break

                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue
                if line_str[0] not in '{[':
                    logger.debug(f"[{self.display_name}] 비-JSON 라인 무시: {line_str[:200]}")
                    continue

                try:
                    response = json.loads(line_str)
                    req_id = response.get("id", 0)
                    if req_id in self._pending:
                        future = self._pending.pop(req_id)
                        if "error" in response:
                            future.set_exception(RuntimeError(response["error"]))
                        else:
                            future.set_result(response.get("result"))
                    else:
                        logger.warning(f"[{self.display_name}] 매칭 안 됨 id={req_id}")
                except json.JSONDecodeError as e:
                    logger.debug(f"[{self.display_name}] JSON 파싱: {e} | {line_str[:200]}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.display_name}] 응답 읽기 에러: {e}")

        for req_id, future in self._pending.items():
            if not future.done():
                future.set_exception(RuntimeError("Bridge process terminated"))
        self._pending.clear()

    async def _call(self, method: str, **params):
        if self._restarting:
            raise RuntimeError(f"[{self.display_name}] 브릿지 재시작 중")
        if not self._process or self._process.returncode is not None:
            raise RuntimeError(f"[{self.display_name}] 브릿지 미실행")

        async with self._lock:
            self._request_id += 1
            req_id = self._request_id

        request = {"id": req_id, "method": method, "params": params}
        request_json = json.dumps(request, ensure_ascii=False, default=str) + "\n"

        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[req_id] = future

        try:
            self._process.stdin.write(request_json.encode("utf-8"))
            await self._process.stdin.drain()
        except Exception as e:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"[{self.display_name}] 요청 전송 실패: {e}")

        try:
            result = await asyncio.wait_for(future, timeout=30.0)
            self._consecutive_timeouts = 0
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            self._consecutive_timeouts += 1
            if self._consecutive_timeouts >= 3 and not self._restarting:
                asyncio.create_task(self._auto_restart())
            raise RuntimeError(
                f"[{self.display_name}] 타임아웃: {method} (연속 {self._consecutive_timeouts}회)"
            )

    async def _auto_restart(self):
        """3회 연속 타임아웃 시 브릿지 자동 재시작."""
        async with self._restart_lock:
            if self._restarting:
                return
            self._restarting = True
            try:
                logger.warning(
                    f"[{self.display_name}] 연속 타임아웃 {self._consecutive_timeouts}회 → 재시작"
                )
                if self._process and self._process.returncode is None:
                    try:
                        self._process.kill()
                        await asyncio.wait_for(self._process.wait(), timeout=5)
                    except Exception as e:
                        logger.debug(f"[{self.display_name}] kill 실패: {e}")
                for req_id, future in list(self._pending.items()):
                    if not future.done():
                        future.set_exception(RuntimeError("bridge restarting"))
                self._pending.clear()
                if self._reader_task and not self._reader_task.done():
                    self._reader_task.cancel()
                self._consecutive_timeouts = 0
                await self.start()
                logger.info(f"[{self.display_name}] 브릿지 재시작 완료")
            except Exception as e:
                logger.error(f"[{self.display_name}] 재시작 실패: {e}")
            finally:
                self._restarting = False

    # ---- 표준 거래소 인터페이스 ----
    async def get_mark_price(self, symbol: str, **kwargs):
        return await self._call("get_mark_price", symbol=symbol)

    async def create_order(self, symbol, side, amount, price=None, order_type='market', **kwargs):
        return await self._call(
            "create_order",
            symbol=symbol, side=side, amount=amount,
            price=price, order_type=order_type,
        )

    async def get_position(self, symbol: str):
        return await self._call("get_position", symbol=symbol)

    async def close_position(self, symbol: str, position=None, **kwargs):
        return await self._call("close_position", symbol=symbol, position=position)

    async def update_leverage(self, symbol: str, leverage=None, margin_mode=None):
        return await self._call(
            "update_leverage", symbol=symbol, leverage=leverage, margin_mode=margin_mode
        )

    async def get_collateral(self):
        return await self._call("get_collateral")

    async def get_balance(self):
        return await self._call("get_balance")

    async def get_open_orders(self, symbol):
        return await self._call("get_open_orders", symbol=symbol)

    async def cancel_orders(self, symbol):
        return await self._call("cancel_orders", symbol=symbol)

    async def close(self):
        """브릿지 프로세스 graceful 종료."""
        try:
            if self._process and self._process.returncode is None:
                try:
                    await asyncio.wait_for(self._call("close"), timeout=5.0)
                except Exception as e:
                    logger.debug(f"[{self.display_name}] close 명령 실패: {e}")
                try:
                    self._process.stdin.close()
                except Exception:
                    pass
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"[{self.display_name}] terminate 타임아웃 → kill")
                    self._process.kill()
                    await self._process.wait()

            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):
                    pass

            self._pending.clear()
            logger.info(f"[{self.display_name}] 브릿지 종료 완료")
        except Exception as e:
            logger.error(f"[{self.display_name}] 종료 에러: {e}")

    async def init(self):
        return await self.start()

    def is_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None
