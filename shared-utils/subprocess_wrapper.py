"""
Subprocess Exchange Wrapper
============================
별도 venv에서 실행되는 거래소 브릿지 프로세스와 JSON-RPC로 통신하는 래퍼.
MultiPerpDex와 동일한 인터페이스를 제공하여 PairTrader가 그대로 사용 가능.

사용법:
    wrapper = SubprocessExchangeWrapper(
        venv_python="nado_venv/bin/python",
        exchange="nado",
        config_path="config.yaml",
        account="nado",
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
    MultiPerpDex 인터페이스와 호환됨.
    """

    def __init__(
        self,
        venv_python: str,
        exchange: str,
        config_path: str,
        account: Optional[str] = None,
        display_name: Optional[str] = None,
    ):
        self.venv_python = venv_python
        self.exchange = exchange
        self.config_path = config_path
        self.account = account or exchange
        self.display_name = display_name or exchange

        self._process: Optional[asyncio.subprocess.Process] = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._consecutive_timeouts = 0
        self._restart_lock = asyncio.Lock()
        self._restarting = False

    async def _kill_zombie_bridges(self):
        """같은 exchange의 기존 브릿지 좀비 프로세스를 kill"""
        if sys.platform == "win32":
            # Windows: PowerShell approach
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
            # Unix/macOS: pkill approach
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pkill", "-f", f"exchange_bridge.*--exchange {self.exchange}",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.wait(), timeout=10)
                logger.debug(f"[{self.display_name}] 좀비 브릿지 정리 완료 (Unix)")
            except Exception as e:
                logger.debug(f"[{self.display_name}] 좀비 정리 스킵 (Unix): {e}")

    async def start(self):
        """브릿지 프로세스 시작 및 초기화"""
        # 기존 좀비 브릿지 프로세스 kill (같은 exchange)
        await self._kill_zombie_bridges()

        project_dir = str(Path(__file__).resolve().parent.parent)
        bridge_module = "strategies.exchange_bridge"

        cmd = [
            self.venv_python,
            "-m", bridge_module,
            "--exchange", self.exchange,
            "--config", self.config_path,
            "--account", self.account,
        ]

        logger.info(f"[{self.display_name}] 브릿지 프로세스 시작: {' '.join(cmd)}")

        # Windows: Ctrl+C가 자식 프로세스에 전달되지 않게 격리
        # → 부모가 먼저 청산 명령을 보낸 뒤 자식을 종료할 수 있음
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_dir,
            env={**os.environ, "PYTHONPATH": project_dir},
            **kwargs,
        )

        # stderr 로그 읽기 태스크
        asyncio.create_task(self._read_stderr())

        # stdout 응답 읽기 태스크
        self._reader_task = asyncio.create_task(self._read_responses())

        # 초기화
        result = await self._call("init")
        if result != "ok":
            raise RuntimeError(f"[{self.display_name}] 브릿지 초기화 실패: {result}")

        logger.info(f"[{self.display_name}] 브릿지 프로세스 준비 완료")
        return self

    async def _read_stderr(self):
        """자식 프로세스의 stderr 로그를 부모 로그로 전달"""
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
        """자식 프로세스의 stdout에서 JSON-RPC 응답 읽기"""
        try:
            while self._process and self._process.stdout:
                line = await self._process.stdout.readline()
                if not line:
                    logger.warning(f"[{self.display_name}] 브릿지 프로세스 stdout 종료")
                    break

                line_str = line.decode("utf-8").strip()
                if not line_str:
                    continue

                # Guard: 자식 프로세스가 stdout으로 print(...)한 비-JSON 라인은 스킵
                # (예: lighter SDK의 print 디버그 메시지가 reconnect 시 stdout 오염)
                # JSON-RPC 응답은 항상 dict({)/list([)로 시작
                if line_str[0] not in '{[':
                    logger.debug(f"[{self.display_name}] 비-JSON stdout 라인 무시: {line_str[:200]}")
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
                        logger.warning(f"[{self.display_name}] 매칭되지 않는 응답 id={req_id}")

                except json.JSONDecodeError as e:
                    # 라인 내용 함께 기록, 스팸 방지 위해 debug 강등
                    logger.debug(f"[{self.display_name}] JSON 파싱 에러: {e} | line={line_str[:200]}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[{self.display_name}] 응답 읽기 에러: {e}")

        # 대기 중인 모든 요청 실패 처리
        for req_id, future in self._pending.items():
            if not future.done():
                future.set_exception(RuntimeError("Bridge process terminated"))
        self._pending.clear()

    async def _call(self, method: str, **params) -> any:
        """JSON-RPC 호출"""
        if self._restarting:
            raise RuntimeError(f"[{self.display_name}] 브릿지 재시작 중")
        if not self._process or self._process.returncode is not None:
            raise RuntimeError(f"[{self.display_name}] 브릿지 프로세스가 실행 중이 아닙니다")

        async with self._lock:
            self._request_id += 1
            req_id = self._request_id

        request = {"id": req_id, "method": method, "params": params}
        request_json = json.dumps(request, ensure_ascii=False, default=str) + "\n"

        # Future 등록
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        self._pending[req_id] = future

        try:
            self._process.stdin.write(request_json.encode("utf-8"))
            await self._process.stdin.drain()
        except Exception as e:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"[{self.display_name}] 요청 전송 실패: {e}")

        # 응답 대기 (타임아웃 30초)
        try:
            result = await asyncio.wait_for(future, timeout=30.0)
            self._consecutive_timeouts = 0  # 성공 시 카운터 리셋
            return result
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            self._consecutive_timeouts += 1
            # 3회 연속 타임아웃 시 브릿지 자동 재시작
            if self._consecutive_timeouts >= 3 and not self._restarting:
                asyncio.create_task(self._auto_restart())
            raise RuntimeError(f"[{self.display_name}] 요청 타임아웃: {method} (연속 {self._consecutive_timeouts}회)")

    async def _auto_restart(self):
        """연속 타임아웃 시 브릿지 자동 재시작"""
        async with self._restart_lock:
            if self._restarting:
                return
            self._restarting = True
            try:
                logger.warning(
                    f"[{self.display_name}] ⚠ 연속 타임아웃 {self._consecutive_timeouts}회 → 브릿지 자동 재시작"
                )
                # 기존 프로세스 강제 종료
                if self._process and self._process.returncode is None:
                    try:
                        self._process.kill()
                        await asyncio.wait_for(self._process.wait(), timeout=5)
                    except Exception as e:
                        logger.debug(f"[{self.display_name}] kill 실패: {e}")
                # 대기 중 요청 전부 실패 처리
                for req_id, future in list(self._pending.items()):
                    if not future.done():
                        future.set_exception(RuntimeError("bridge restarting"))
                self._pending.clear()
                # 리더 태스크 취소
                if self._reader_task and not self._reader_task.done():
                    self._reader_task.cancel()
                # 재시작
                self._consecutive_timeouts = 0
                await self.start()
                logger.info(f"[{self.display_name}] ✓ 브릿지 재시작 완료")
            except Exception as e:
                logger.error(f"[{self.display_name}] 브릿지 재시작 실패: {e}")
            finally:
                self._restarting = False

    # ==============================================================
    # MultiPerpDex 호환 인터페이스
    # ==============================================================

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
        return await self._call("update_leverage", symbol=symbol, leverage=leverage, margin_mode=margin_mode)

    async def get_collateral(self):
        return await self._call("get_collateral")

    async def get_balance(self):
        """잔고 조회 (오토스케일러용)"""
        return await self._call("get_balance")

    async def get_open_orders(self, symbol):
        return await self._call("get_open_orders", symbol=symbol)

    async def cancel_orders(self, symbol):
        return await self._call("cancel_orders", symbol=symbol)

    async def close(self):
        """브릿지 프로세스 종료"""
        try:
            if self._process and self._process.returncode is None:
                # close 명령 전송
                try:
                    await asyncio.wait_for(self._call("close"), timeout=5.0)
                except Exception as e:
                    logger.debug(f"[{self.display_name}] close 명령 전송 실패: {e}")

                # stdin 닫기
                try:
                    self._process.stdin.close()
                except Exception as e:
                    logger.debug(f"[{self.display_name}] stdin close 실패: {e}")

                # SIGTERM → wait → SIGKILL
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning(f"[{self.display_name}] SIGTERM 타임아웃 → SIGKILL")
                    self._process.kill()
                    await self._process.wait()

            # reader task 정리
            if self._reader_task and not self._reader_task.done():
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):
                    pass

            # pending 요청 정리
            self._pending.clear()

            logger.info(f"[{self.display_name}] 브릿지 프로세스 종료 완료")

        except Exception as e:
            logger.error(f"[{self.display_name}] 브릿지 종료 에러: {e}")

    async def init(self):
        """start()와 동일 (호환성)"""
        return await self.start()

    def is_alive(self) -> bool:
        """브릿지 프로세스가 살아있는지 확인"""
        return self._process is not None and self._process.returncode is None
