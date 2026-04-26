"""Telegram 명령 봇 (스탠드얼론).

실행: python telegram_commander.py

환경변수:
  TELEGRAM_BOT_TOKEN  — BotFather 봇 토큰
  TELEGRAM_CHAT_ID    — 화이트리스트 chat ID (보안: 이 ID에서 온 메시지만 처리)
  PERP_DEX_DIR        — Perp DEX 봇 디렉토리 (기본: 현재 파일의 상위)
  POLYMARKET_DIR      — Polymarket 봇 디렉토리 (선택)

지원 명령어 (모든 명령어는 / 로 시작):
  /status            봇 상태 + 최근 PnL
  /pnl [days]        N일 PnL 리포트 (기본 1일)
  /balance           거래소별 잔고
  /positions         현재 오픈 포지션
  /restart           graceful restart (포지션 유지)
  /reload            hot reload (config만)
  /close             전체 청산 + 종료 (확인 필요)
  /kill <exchange>   특정 거래소 auto-disable
  /revive <exchange> auto-disable 해제
  /bnb               Polymarket BNB 잔고
  /help              도움말
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("telegram_commander")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
PERP_DEX_DIR = Path(
    os.environ.get("PERP_DEX_DIR") or Path(__file__).resolve().parent
)
POLYMARKET_DIR_ENV = os.environ.get("POLYMARKET_DIR")
POLYMARKET_DIR = Path(POLYMARKET_DIR_ENV) if POLYMARKET_DIR_ENV else None

API = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else ""
TRIGGER_DIR = PERP_DEX_DIR / "triggers"
TRIGGER_DIR.mkdir(parents=True, exist_ok=True)


class Commander:
    def __init__(self):
        self.offset = 0
        self._pending_confirm: dict[str, str] = {}  # {chat_id: command}

    async def send(self, session: aiohttp.ClientSession, text: str, *, chat_id: Optional[str] = None):
        target = chat_id or CHAT_ID
        payload = {"chat_id": target, "text": text[:4000], "parse_mode": "HTML"}
        try:
            async with session.post(f"{API}/sendMessage", json=payload) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning(f"send failed {resp.status}: {body[:200]}")
        except Exception as e:
            logger.warning(f"send exception: {e}")

    async def get_updates(self, session: aiohttp.ClientSession) -> list[dict]:
        params = {"offset": self.offset, "timeout": 30}
        try:
            async with session.get(f"{API}/getUpdates", params=params, timeout=aiohttp.ClientTimeout(total=35)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("result", [])
        except asyncio.TimeoutError:
            return []
        except Exception as e:
            logger.debug(f"poll error: {e}")
            await asyncio.sleep(2)
            return []

    def _touch_trigger(self, name: str) -> bool:
        try:
            (TRIGGER_DIR / name).write_text(str(time.time()), encoding="utf-8")
            return True
        except Exception as e:
            logger.warning(f"trigger write failed {name}: {e}")
            return False

    async def _run_status_dump(self) -> Optional[dict]:
        """status.trigger → status.out 대기 후 반환."""
        out = TRIGGER_DIR / "status.out"
        try:
            if out.exists():
                out.unlink()
        except Exception:
            pass
        self._touch_trigger("status.trigger")
        deadline = time.time() + 10
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if out.exists():
                try:
                    return json.loads(out.read_text(encoding="utf-8"))
                except Exception:
                    return None
        return None

    async def handle(self, session: aiohttp.ClientSession, update: dict):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip()

        # 화이트리스트
        if CHAT_ID and chat_id != CHAT_ID:
            logger.warning(f"unauthorized chat_id={chat_id}")
            return

        # 음성 메시지 처리 (Whisper)
        if msg.get("voice") and not text:
            text = await self._transcribe_voice(session, msg["voice"])
            if text:
                await self.send(session, f"🎙 <i>음성 해석</i>: <code>{text}</code>")
            else:
                return

        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        # Pending confirm 처리
        if chat_id in self._pending_confirm:
            pending = self._pending_confirm.pop(chat_id)
            if cmd == "/yes":
                cmd = pending
                args = []
            else:
                await self.send(session, "확인 취소")
                return

        try:
            if cmd == "/help":
                await self.send(session, self._help_text())
            elif cmd == "/status":
                await self._cmd_status(session)
            elif cmd == "/pnl":
                days = int(args[0]) if args else 1
                await self._cmd_pnl(session, days)
            elif cmd == "/balance":
                await self._cmd_balance(session)
            elif cmd == "/positions":
                await self._cmd_positions(session)
            elif cmd == "/restart":
                ok = self._touch_trigger("restart.trigger")
                await self.send(session, "restart.trigger 생성됨" if ok else "실패")
            elif cmd == "/reload":
                ok = self._touch_trigger("reload.trigger")
                await self.send(session, "reload.trigger 생성됨" if ok else "실패")
            elif cmd == "/close":
                self._pending_confirm[chat_id] = "/close_confirmed"
                await self.send(session, "⚠ 전체 포지션 청산됩니다. /yes 입력하면 실행.")
            elif cmd == "/close_confirmed":
                ok = self._touch_trigger("close.trigger")
                await self.send(session, "close.trigger 생성됨" if ok else "실패")
            elif cmd == "/kill":
                if not args:
                    await self.send(session, "사용: /kill <exchange>")
                else:
                    self._manual_disable(args[0], reason="manual via telegram")
                    await self.send(session, f"{args[0]} auto-disable 등록")
            elif cmd == "/revive":
                if not args:
                    await self.send(session, "사용: /revive <exchange>")
                else:
                    self._manual_enable(args[0])
                    await self.send(session, f"{args[0]} auto-disable 해제")
            elif cmd == "/clearcb":
                ok = self._touch_trigger("clear_cb.trigger")
                await self.send(session, "circuit breaker 해제 trigger 생성" if ok else "실패")
            elif cmd == "/volume" or cmd == "/vol":
                await self._cmd_volume(session)
            elif cmd == "/points":
                await self._cmd_points(session, args)
            elif cmd == "/setpoint":
                await self._cmd_setpoint(session, args)
            elif cmd == "/pointlinks":
                await self._cmd_pointlinks(session)
            elif cmd == "/report":
                await self._cmd_report(session)
            elif cmd == "/livepts" or cmd == "/live":
                await self._cmd_livepts(session)
            elif cmd == "/all" or cmd == "/points_all":
                await self._cmd_all_points(session)
            elif cmd == "/bulk" or cmd == "/setbulk":
                await self._cmd_setbulk(session, text)
            elif cmd == "/missing":
                await self._cmd_missing(session)
            elif cmd == "/alerts":
                await self._cmd_alerts(session, args)
            elif cmd == "/diag":
                await self._cmd_diag(session)
            elif cmd == "/perf":
                await self._cmd_perf(session)
            elif cmd == "/weekly":
                await self._cmd_weekly(session)
            elif cmd == "/tune":
                await self._cmd_tune(session, args)
            elif cmd == "/fills":
                await self._cmd_fills(session)
            elif cmd == "/funding":
                await self._cmd_funding(session)
            elif cmd == "/chart":
                await self._cmd_chart(session, args)
            elif cmd == "/about":
                await self._cmd_about(session)
            elif cmd == "/backtest":
                await self._cmd_backtest(session, args)
            elif cmd == "/ab":
                await self._cmd_ab(session)
            elif cmd == "/rebalance":
                await self._cmd_rebalance(session)
            elif cmd == "/evolver":
                await self._cmd_evolver(session)
            elif cmd == "/scout":
                await self._cmd_scout(session)
            elif cmd == "/blog":
                await self._cmd_blog(session, args)
            elif cmd == "/decide":
                await self._run_script(session, "scripts/decision_dashboard.py", ["--telegram"], "의사결정 분석 중...")
            elif cmd == "/weekdecide":
                await self._run_script(session, "scripts/weekly_decision.py", ["--telegram"], "주간 보고서 생성 중...")
            elif cmd == "/roi":
                await self._run_script(session, "scripts/airdrop_roi.py", ["--telegram"], "에어드랍 ROI 분석 중...")
            elif cmd == "/topics":
                await self._run_script(session, "scripts/content_suggester.py", ["--telegram"], "콘텐츠 주제 추출 중...")
            elif cmd == "/bnb":
                await self._cmd_bnb(session)
            else:
                await self.send(session, f"알 수 없는 명령: {cmd}")
        except Exception as e:
            logger.error(f"handle {cmd} error: {e}", exc_info=True)
            await self.send(session, f"에러: {e}")

    # ---------- commands ----------
    def _help_text(self) -> str:
        return (
            "<b>Bot Commander</b>\n"
            "/status — 봇 상태\n"
            "/pnl [days] — PnL 리포트\n"
            "/balance — 거래소 잔고\n"
            "/positions — 오픈 포지션\n"
            "/restart — graceful restart\n"
            "/reload — config hot reload\n"
            "/close — 전체 청산 후 종료 (/yes 확인)\n"
            "/kill &lt;exchange&gt; — auto-disable\n"
            "/revive &lt;exchange&gt; — 해제\n"
            "/clearcb — circuit breaker 해제\n"
            "/volume — 일일 볼륨 증감 리포트\n"
            "/points [exchange] — 수동 입력 포인트 조회\n"
            "/setpoint &lt;ex&gt; &lt;value&gt; [note] — 포인트 수동 저장\n"
            "/pointlinks — 거래소별 포인트 확인 URL\n"
            "/report — 지금 즉시 일일 리포트 실행\n"
            "/livepts — 거래소 API 실시간 포인트 조회 (HL/Ethereal/HyENA)\n"
            "/all — 전 거래소 포인트 + 전일 대비 증감률 통합 뷰\n"
            "/bulk — 여러 거래소 포인트 한번에 입력 (여러 줄)\n"
            "/missing — 아직 기록 안 된 거래소 + 링크\n"
            "/alerts [on|off|list] [prefix] — 알림 필터\n"
            "/diag — 봇 자가 진단 (WS 에러율, 거래소 안정성)\n"
            "/perf — 시간대별 + 거래소별 성과 분석\n"
            "/weekly — 주간 요약 + blog draft 생성\n"
            "/tune [apply] — 거래소별 파라미터 권장 (apply=자동 적용)\n"
            "/fills — Maker/Taker 체결 품질 + 슬리피지\n"
            "/funding — 펀딩 스프레드 기회 스캔\n"
            "/chart balance|ethereal|volume — 차트 PNG 전송\n"
            "/about — 봇 자기소개\n"
            "/backtest [tp] [sl] — 과거 파라미터 시뮬레이션\n"
            "/ab — A/B 테스트 결과\n"
            "/rebalance — 거래소간 잔고 불균형 + 이체 제안\n"
            "/evolver — Strategy Evolver 현재 signal weights + 최근 업데이트\n"
            "/scout — 신규 에어드랍 감지 스캔\n"
            "/blog [blog|youtube|twitter|all] — LLM 콘텐츠 자동 생성\n"
            "/decide — 오늘의 거래소별 액션 (INCREASE/MAINTAIN/REDUCE/STOP)\n"
            "/weekdecide — 주간 의사결정 보고서\n"
            "/roi — 거래소별 에어드랍 ROI + 재배분 권장\n"
            "/topics — 이번 주 콘텐츠 주제 5개 자동 제안\n"
            "/live (음성) — voice message로 명령 (OPENAI_API_KEY 필요)\n"
            "/bnb — Polymarket BNB 잔고\n"
        )

    async def _cmd_status(self, session: aiohttp.ClientSession):
        status = await self._run_status_dump()
        if not status:
            await self.send(session, "status 응답 없음 (봇 미실행 또는 trigger_watcher 미적용)")
            return
        lines = [f"<b>봇 상태</b> — {status.get('timestamp', '')}"]
        health = status.get("health", {})
        if health:
            cb = health.get("circuit_breaker_tripped", False)
            lines.append(f"circuit breaker: {'🔴 TRIPPED' if cb else '🟢 ok'}")
            disabled = health.get("auto_disabled", {})
            if disabled:
                lines.append(f"auto-disabled: {', '.join(disabled.keys())}")
            ws = health.get("ws_fallback_counts", {})
            if ws:
                high = {k: v for k, v in ws.items() if v > 3}
                if high:
                    lines.append(f"WS fallback: {high}")
        tasks = status.get("exchange_tasks", [])
        lines.append(f"active exchanges ({len(tasks)}): {', '.join(tasks[:8])}{'...' if len(tasks) > 8 else ''}")
        await self.send(session, "\n".join(lines))

    async def _cmd_pnl(self, session: aiohttp.ClientSession, days: int):
        script = PERP_DEX_DIR / "scripts" / "daily_report.py"
        if not script.exists():
            await self.send(session, "daily_report.py 없음")
            return
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), "--days", str(days),
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "daily_report 타임아웃")
            return
        txt = out.decode("utf-8", errors="replace")[-3000:]
        await self.send(session, f"<pre>{txt}</pre>")

    async def _cmd_balance(self, session: aiohttp.ClientSession):
        eq = PERP_DEX_DIR / "equity_tracker.json"
        try:
            data = json.loads(eq.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not data:
                await self.send(session, "equity_tracker 비어있음")
                return
            latest = data[-1]
            ts = latest.get("timestamp", "")
            balances = latest.get("exchanges", {})
            lines = [f"<b>잔고</b> — {ts}"]
            total = 0.0
            for ex, bal in sorted(balances.items(), key=lambda x: -(float(x[1] or 0))):
                if ex in ("bulk", "dreamcash"):
                    continue
                b = float(bal or 0)
                total += b
                if b > 0.1:
                    lines.append(f"  {ex:20s} ${b:>8.2f}")
            lines.append(f"\n총: ${total:.2f}")
            await self.send(session, "\n".join(lines))
        except Exception as e:
            await self.send(session, f"balance 에러: {e}")

    async def _cmd_positions(self, session: aiohttp.ClientSession):
        state = PERP_DEX_DIR / "trader_state.json"
        if not state.exists():
            await self.send(session, "trader_state.json 없음 (포지션 없거나 봇 미실행)")
            return
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            lines = ["<b>오픈 포지션</b>"]
            for ex, st in data.items():
                direction = st.get("direction") or "-"
                if direction != "-":
                    lines.append(f"  {ex}: {direction} dca={st.get('dca_count', 0)}")
            if len(lines) == 1:
                lines.append("(none)")
            await self.send(session, "\n".join(lines))
        except Exception as e:
            await self.send(session, f"positions 에러: {e}")

    async def _cmd_bnb(self, session: aiohttp.ClientSession):
        if not POLYMARKET_DIR:
            await self.send(session, "POLYMARKET_DIR 미설정")
            return
        # balance_snapshots.jsonl 마지막 + bnb는 별도 조회 필요
        snap = POLYMARKET_DIR / "balance_snapshots.jsonl"
        try:
            if snap.exists():
                with snap.open("r", encoding="utf-8") as f:
                    last = None
                    for line in f:
                        if line.strip():
                            last = line
                if last:
                    await self.send(session, f"<b>Polymarket</b>\n<code>{last.strip()}</code>")
                    return
            await self.send(session, "balance_snapshots 없음")
        except Exception as e:
            await self.send(session, f"bnb 에러: {e}")

    async def _cmd_volume(self, session: aiohttp.ClientSession):
        """전일 대비 오늘 볼륨 증감."""
        snap_path = PERP_DEX_DIR / "points_snapshots.json"
        if not snap_path.exists():
            await self.send(session, "points_snapshots.json 없음. 먼저 /report 실행하세요")
            return
        try:
            data = json.loads(snap_path.read_text(encoding='utf-8'))
            dates = sorted(data.keys())
            if len(dates) < 1:
                await self.send(session, "스냅샷 데이터 부족")
                return
            today = dates[-1]
            yest = dates[-2] if len(dates) >= 2 else None
            tv = data[today].get("volume", {})
            yv = data[yest].get("volume", {}) if yest else {}
            lines = [f"<b>볼륨 증감</b> — {today}"]
            if yest:
                lines.append(f"<i>vs {yest}</i>")
            lines.append("")
            rows = []
            for ex in set(list(tv.keys()) + list(yv.keys())):
                t = tv.get(ex, 0)
                y = yv.get(ex, 0)
                d = t - y
                if t < 1 and d < 1:
                    continue
                rows.append((ex, t, d))
            rows.sort(key=lambda r: -r[2])
            for ex, t, d in rows:
                s = "+" if d >= 0 else ""
                lines.append(f"<code>{ex:<20s}</code> ${t:>10,.0f} ({s}${d:>+8,.0f})")
            lines.append("")
            lines.append(f"<b>합계: ${sum(tv.values()):,.0f}</b>")
            await self.send(session, "\n".join(lines))
        except Exception as e:
            await self.send(session, f"/volume 에러: {e}")

    async def _cmd_points(self, session: aiohttp.ClientSession, args: list):
        """수동 입력한 포인트 조회 + 전일 대비 증감."""
        mp_path = PERP_DEX_DIR / "points_manual.json"
        if not mp_path.exists():
            await self.send(session, "수동 포인트 기록 없음. /setpoint 로 입력하세요")
            return
        try:
            data = json.loads(mp_path.read_text(encoding='utf-8'))
            dates = sorted(data.keys())
            if not dates:
                await self.send(session, "기록 없음")
                return
            today = dates[-1]
            yest = dates[-2] if len(dates) >= 2 else None
            tp = data[today]
            yp = data.get(yest, {}) if yest else {}
            filter_ex = args[0] if args else None
            lines = [f"<b>수동 포인트</b> — {today}" + (f" ({filter_ex})" if filter_ex else "")]
            for ex, info in sorted(tp.items()):
                if filter_ex and ex != filter_ex:
                    continue
                t = float(info.get("points", 0))
                y = float(yp.get(ex, {}).get("points", 0))
                d = t - y
                s = "+" if d >= 0 else ""
                note = info.get("note", "")
                nstr = f" <i>({note})</i>" if note else ""
                lines.append(f"<code>{ex:<20s}</code> {t:>10,.1f} ({s}{d:,.1f}){nstr}")
            if len(lines) == 1:
                lines.append("기록 없음")
            await self.send(session, "\n".join(lines))
        except Exception as e:
            await self.send(session, f"/points 에러: {e}")

    async def _cmd_setpoint(self, session: aiohttp.ClientSession, args: list):
        """수동 포인트 기록: /setpoint <exchange> <value> [note...]"""
        if len(args) < 2:
            await self.send(session, "사용: /setpoint &lt;exchange&gt; &lt;value&gt; [note]\n예: /setpoint ethereal 12500 dashboard")
            return
        ex = args[0]
        try:
            val = float(args[1].replace(",", ""))
        except ValueError:
            await self.send(session, "value는 숫자")
            return
        note = " ".join(args[2:]) if len(args) > 2 else ""
        mp_path = PERP_DEX_DIR / "points_manual.json"
        data = {}
        if mp_path.exists():
            try:
                data = json.loads(mp_path.read_text(encoding='utf-8'))
            except Exception:
                pass
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        kst = _tz(_td(hours=9))
        today = _dt.now(tz=kst).date().isoformat()
        data.setdefault(today, {})[ex] = {"points": val, "note": note}
        mp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        # 전일 대비 증감
        yest = (_dt.now(tz=kst).date() - _td(days=1)).isoformat()
        yval = float(data.get(yest, {}).get(ex, {}).get("points", 0))
        diff = val - yval
        sign = "+" if diff >= 0 else ""
        await self.send(session, f"<code>{ex}</code> 포인트 저장: {val:,.1f}\n전일({yest}) 대비 <b>{sign}{diff:,.1f}</b>")

    async def _cmd_pointlinks(self, session: aiohttp.ClientSession):
        """거래소별 포인트 확인 웹 URL."""
        lines = ["<b>거래소별 포인트 확인 링크</b>"]
        links = [
            ("Hyperliquid", "https://app.hyperliquid.xyz/points"),
            ("Ethereal", "https://www.ethereal.trade/trade"),
            ("StandX", "https://dex.standx.com/points"),
            ("Nado", "https://nado.xyz/points"),
            ("EdgeX", "https://pro.edgex.exchange/point-dashboard"),
            ("Lighter", "https://app.lighter.xyz/public-pools"),
            ("GRVT", "https://trade.grvt.io/rewards"),
            ("Reya", "https://app.reya.xyz/points"),
            ("Aster", "https://www.asterdex.com/rewards"),
            ("TreadFi", "https://app.tread.fi/rewards"),
            ("Hotstuff", "https://hotstuff.io/points"),
            ("Variational", "https://trade.variational.io/rewards"),
            ("Miracle", "https://miracletrade.com/points"),
            ("HyENA", "https://app.hyperliquid.xyz/vaults"),
            ("Katana", "https://pro.katana.trade/points"),
            ("Ostium", "https://ostium.app/points"),
        ]
        for name, url in links:
            lines.append(f"• <b>{name}</b>: {url}")
        lines.append("")
        lines.append("<i>URL 바뀌었으면 알려주세요 (수정 필요)</i>")
        await self.send(session, "\n".join(lines))

    async def _cmd_livepts(self, session: aiohttp.ClientSession):
        """거래소 API에서 실시간 포인트 직접 조회 (빠름)."""
        try:
            import sys as _sys
            _sys.path.insert(0, str(PERP_DEX_DIR / "scripts"))
            from points_fetcher import fetch_all, format_summary
            await self.send(session, "실시간 조회 중...")
            data = await fetch_all()
            summary = format_summary(data)
            await self.send(session, summary)
        except Exception as e:
            logger.error(f"livepts 에러: {e}", exc_info=True)
            await self.send(session, f"조회 실패: {e}")

    async def _transcribe_voice(self, session: aiohttp.ClientSession, voice: dict) -> str:
        """Telegram voice message → OpenAI Whisper API로 텍스트 변환.

        OPENAI_API_KEY 필요. 없으면 빈 문자열 반환.
        음성을 한국어/영어로 해석, 명령어 형태로 변환 시도.
        예: "밸런스" → "/balance", "상태" → "/status"
        """
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            await self.send(session, "음성 명령 비활성 — OPENAI_API_KEY 설정 필요")
            return ""
        try:
            # 1. voice file 다운로드
            file_id = voice.get("file_id")
            if not file_id:
                return ""
            async with session.get(f"{API}/getFile", params={"file_id": file_id}) as r:
                info = (await r.json()).get("result", {})
            file_path = info.get("file_path")
            if not file_path:
                return ""
            file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            async with session.get(file_url) as r:
                audio_bytes = await r.read()

            # 2. Whisper API
            form = aiohttp.FormData()
            form.add_field("file", audio_bytes, filename="voice.ogg", content_type="audio/ogg")
            form.add_field("model", "whisper-1")
            form.add_field("language", "ko")
            headers = {"Authorization": f"Bearer {api_key}"}
            async with session.post(
                "https://api.openai.com/v1/audio/transcriptions",
                data=form, headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as r:
                if r.status != 200:
                    logger.warning(f"Whisper {r.status}: {(await r.text())[:200]}")
                    return ""
                result = await r.json()
            transcript = result.get("text", "").strip().lower()
            if not transcript:
                return ""

            # 3. 자연어 → 명령 매핑
            mapping = {
                "상태": "/status", "status": "/status",
                "잔고": "/balance", "밸런스": "/balance", "balance": "/balance",
                "포지션": "/positions", "positions": "/positions",
                "재시작": "/restart", "restart": "/restart",
                "리로드": "/reload", "reload": "/reload",
                "청산": "/close", "close": "/close",
                "현황": "/all", "전체": "/all",
                "포인트": "/livepts", "points": "/livepts",
                "성과": "/perf", "perf": "/perf",
                "체결": "/fills", "fills": "/fills",
                "진단": "/diag", "diag": "/diag",
                "리포트": "/report", "report": "/report",
                "주간": "/weekly", "weekly": "/weekly",
                "튜닝": "/tune", "tune": "/tune",
                "펀딩": "/funding", "funding": "/funding",
                "미기록": "/missing", "missing": "/missing",
                "도움": "/help", "help": "/help",
            }
            for keyword, cmd in mapping.items():
                if keyword in transcript:
                    return cmd
            return ""
        except Exception as e:
            logger.error(f"voice 변환 에러: {e}", exc_info=True)
            return ""

    async def _cmd_alerts(self, session: aiohttp.ClientSession, args: list):
        """알림 필터 토글: /alerts list | /alerts off ws_fallback | /alerts on ws_fallback"""
        filter_path = PERP_DEX_DIR / "alert_filters.json"
        data = {"disabled_keys": [], "disabled_prefixes": []}
        if filter_path.exists():
            try:
                data = json.loads(filter_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        if not args or args[0] == "list":
            lines = ["<b>알림 필터 상태</b>"]
            disabled = data.get("disabled_keys", []) + data.get("disabled_prefixes", [])
            if not disabled:
                lines.append("  모든 알림 활성")
            else:
                lines.append("  <b>차단 중</b>:")
                for k in disabled:
                    lines.append(f"    - {k}")
            lines.append("")
            lines.append("<i>/alerts off ws_fallback — WS fallback 알림 끄기</i>")
            lines.append("<i>/alerts on ws_fallback — 다시 켜기</i>")
            await self.send(session, "\n".join(lines))
            return

        action = args[0].lower()
        if action not in ("on", "off"):
            await self.send(session, "사용: /alerts on|off|list [prefix]")
            return
        if len(args) < 2:
            await self.send(session, f"어떤 알림? 예: /alerts {action} ws_fallback")
            return
        prefix = args[1]
        prefixes = set(data.get("disabled_prefixes", []))
        if action == "off":
            prefixes.add(prefix)
            await self.send(session, f"✓ '{prefix}*' 알림 차단")
        else:
            prefixes.discard(prefix)
            await self.send(session, f"✓ '{prefix}*' 알림 재활성")
        data["disabled_prefixes"] = list(prefixes)
        filter_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    async def _cmd_diag(self, session: aiohttp.ClientSession):
        """봇 자가 진단 실행."""
        script = PERP_DEX_DIR / "scripts" / "self_diagnosis.py"
        if not script.exists():
            await self.send(session, "self_diagnosis.py 없음")
            return
        await self.send(session, "진단 실행 중...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
            # self_diagnosis.py가 자체적으로 텔레그램 전송
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_perf(self, session: aiohttp.ClientSession):
        script = PERP_DEX_DIR / "scripts" / "analyze_performance.py"
        if not script.exists():
            await self.send(session, "analyze_performance.py 없음")
            return
        await self.send(session, "성과 분석 중 (약 15초)...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), "--telegram",
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_tune(self, session: aiohttp.ClientSession, args: list):
        apply = args and args[0].lower() == "apply"
        script = PERP_DEX_DIR / "scripts" / "tune_params.py"
        if not script.exists():
            await self.send(session, "tune_params.py 없음")
            return
        cmd_args = [sys.executable, str(script), "--telegram"]
        if apply:
            cmd_args.append("--apply")
        await self.send(session, "파라미터 분석 중..." + (" (자동 적용)" if apply else ""))
        proc = await asyncio.create_subprocess_exec(
            *cmd_args,
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_fills(self, session: aiohttp.ClientSession):
        script = PERP_DEX_DIR / "scripts" / "analyze_fills.py"
        if not script.exists():
            await self.send(session, "analyze_fills.py 없음")
            return
        await self.send(session, "체결 품질 분석 중...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), "--telegram",
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=90)
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_chart(self, session: aiohttp.ClientSession, args: list):
        chart_type = args[0] if args else "balance"
        if chart_type not in ("balance", "ethereal", "volume"):
            await self.send(session, "사용: /chart balance|ethereal|volume")
            return
        script = PERP_DEX_DIR / "scripts" / "chart_generator.py"
        if not script.exists():
            await self.send(session, "chart_generator.py 없음")
            return
        await self.send(session, f"차트 생성 중 ({chart_type})...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), chart_type, "--telegram",
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_about(self, session: aiohttp.ClientSession):
        import json as _json
        from datetime import datetime as _dt
        # 봇 메타정보
        eq_path = PERP_DEX_DIR / "equity_tracker.json"
        total = 0
        count = 0
        if eq_path.exists():
            data = _json.loads(eq_path.read_text(encoding='utf-8'))
            if data:
                latest = data[-1].get("exchanges", {})
                total = sum(float(v or 0) for k,v in latest.items() if k not in ("bulk","dreamcash"))
                count = sum(1 for v in latest.values() if float(v or 0) > 1)
        ad_path = PERP_DEX_DIR / "auto_disabled_exchanges.json"
        disabled_n = 0
        if ad_path.exists():
            try:
                disabled_n = len(_json.loads(ad_path.read_text(encoding='utf-8')))
            except Exception:
                pass
        # airdrop 가치
        airdrop_path = PERP_DEX_DIR / "airdrop_valuation_history.json"
        airdrop_usd = 0
        if airdrop_path.exists():
            try:
                av = _json.loads(airdrop_path.read_text(encoding='utf-8'))
                dates = sorted(av.keys())
                if dates:
                    airdrop_usd = float(av[dates[-1]].get("total_usd", 0))
            except Exception:
                pass

        msg = (
            "<b>🤖 Perpdex Mage Bot</b>\n\n"
            "Perp DEX 에어드랍 파밍 + 페어트레이딩 자동화 봇.\n\n"
            f"<b>관리 거래소</b>: {count}개 활성 / {disabled_n}개 비활성\n"
            f"<b>총 잔고</b>: ${total:,.2f}\n"
            f"<b>예상 에어드랍 가치</b>: ${airdrop_usd:,.0f}\n\n"
            "<b>가동 서비스</b>:\n"
            "  • Perp DEX 봇 (16개 거래소)\n"
            "  • Telegram Commander (40+ 명령)\n"
            "  • HealthMonitor (자동 리스크 가드)\n"
            "  • Position Monitor (3분 주기)\n"
            "  • Polymarket + Predict.fun 봇\n\n"
            "<b>자동 가드</b>: circuit breaker / auto-disable / auto-revive / WS fallback\n"
            "<b>일일 보고</b>: 00:05 / 09:00 / 22:00 (자동)\n\n"
            "<i>made by coinmage | /help로 전체 명령어</i>"
        )
        await self.send(session, msg)

    async def _cmd_backtest(self, session: aiohttp.ClientSession, args: list):
        tp = args[0] if len(args) > 0 else "2.0"
        sl = args[1] if len(args) > 1 else "2.5"
        script = PERP_DEX_DIR / "scripts" / "backtest.py"
        if not script.exists():
            await self.send(session, "backtest.py 없음")
            return
        await self.send(session, f"백테스트 실행 중 (TP={tp}% SL={sl}%)...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), "--tp", tp, "--sl", sl,
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            txt = out.decode("utf-8", errors="replace")[-3500:]
            await self.send(session, f"<pre>{txt}</pre>")
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_ab(self, session: aiohttp.ClientSession):
        script = PERP_DEX_DIR / "scripts" / "ab_test.py"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), "--telegram",
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()

    async def _cmd_evolver(self, session: aiohttp.ClientSession):
        import yaml
        try:
            with (PERP_DEX_DIR / "config.yaml").open("r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            evolver = cfg.get("strategy_evolver", {})
            if not evolver.get("enabled"):
                await self.send(session, "Strategy Evolver 비활성")
                return
            lines = ["<b>🧬 Strategy Evolver</b>", ""]
            lines.append(f"활성: {evolver.get('enabled')}")
            lines.append(f"nado_scalper 통합: {evolver.get('use_in_nado_scalper', False)}")
            lines.append(f"주기: {evolver.get('interval_hours')}시간")
            lines.append(f"최근 업데이트: {evolver.get('last_updated', '-')}")
            lines.append("")
            lines.append("<b>Signal Weights</b>")
            for k, v in (evolver.get("signal_weights", {}) or {}).items():
                bar = "█" * int(v * 20)
                lines.append(f"  <code>{k:<22s}</code> {v:.2f} {bar}")
            lines.append("")
            lines.append("<b>Min Weights</b>")
            for k, v in (evolver.get("min_signal_weights", {}) or {}).items():
                lines.append(f"  {k}: {v}")
            await self.send(session, "\n".join(lines))
        except Exception as e:
            await self.send(session, f"/evolver 에러: {e}")

    async def _cmd_scout(self, session: aiohttp.ClientSession):
        script = PERP_DEX_DIR / "scripts" / "airdrop_scout.py"
        if not script.exists():
            await self.send(session, "airdrop_scout.py 없음")
            return
        await self.send(session, "신규 에어드랍 스캔 중 (30초)...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()

    async def _run_script(self, session: aiohttp.ClientSession, script_rel: str, args: list, msg: str):
        """Helper: 스크립트 서브프로세스로 실행."""
        script = PERP_DEX_DIR / script_rel
        if not script.exists():
            await self.send(session, f"{script_rel} 없음")
            return
        await self.send(session, msg)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), *args,
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_blog(self, session: aiohttp.ClientSession, args: list):
        kind = args[0] if args else "all"
        if kind not in ("blog", "youtube", "twitter", "all"):
            await self.send(session, "사용: /blog blog|youtube|twitter|all")
            return
        script = PERP_DEX_DIR / "scripts" / "llm_content_engine.py"
        if not script.exists():
            await self.send(session, "llm_content_engine.py 없음")
            return
        await self.send(session, f"LLM 콘텐츠 생성 중 ({kind})...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), kind,
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
            txt = out.decode("utf-8", errors="replace")[-2000:]
            await self.send(session, f"<pre>{txt}</pre>")
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃 (LLM 응답 지연)")

    async def _cmd_rebalance(self, session: aiohttp.ClientSession):
        script = PERP_DEX_DIR / "scripts" / "rebalance_alert.py"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()

    async def _cmd_funding(self, session: aiohttp.ClientSession):
        script = PERP_DEX_DIR / "scripts" / "funding_scan.py"
        if not script.exists():
            await self.send(session, "funding_scan.py 없음")
            return
        await self.send(session, "펀딩 스프레드 스캔 중...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), "--telegram",
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_weekly(self, session: aiohttp.ClientSession):
        script = PERP_DEX_DIR / "scripts" / "weekly_summary.py"
        if not script.exists():
            await self.send(session, "weekly_summary.py 없음")
            return
        await self.send(session, "주간 요약 생성 중...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            await asyncio.wait_for(proc.communicate(), timeout=60)
            await self.send(session, "✓ 주간 요약 전송 + weekly_draft.md 저장")
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "타임아웃")

    async def _cmd_setbulk(self, session: aiohttp.ClientSession, full_text: str):
        """여러 거래소 한번에 입력.

        사용법:
          /bulk
          standx 8500
          nado 42000 s1
          katana 15000
          edgex 5000 post-tge
        """
        lines_in = full_text.split("\n")[1:]  # 첫줄은 /bulk
        if not lines_in or not lines_in[0].strip():
            example = (
                "<b>사용법</b> (각 줄: 거래소 포인트 [note]):\n"
                "<code>/bulk\n"
                "standx 8500\n"
                "nado 42000 s1\n"
                "katana 15000\n"
                "edgex 5000 post-tge</code>"
            )
            await self.send(session, example)
            return
        mp_path = PERP_DEX_DIR / "points_manual.json"
        data = {}
        if mp_path.exists():
            try:
                data = json.loads(mp_path.read_text(encoding='utf-8'))
            except Exception:
                pass
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        kst = _tz(_td(hours=9))
        today = _dt.now(tz=kst).date().isoformat()
        yest = (_dt.now(tz=kst).date() - _td(days=1)).isoformat()
        data.setdefault(today, {})
        yest_data = data.get(yest, {})
        summary = ["<b>📝 일괄 저장 결과</b>"]
        errors = []
        for ln in lines_in:
            parts = ln.strip().split(maxsplit=2)
            if len(parts) < 2:
                continue
            ex = parts[0]
            try:
                val = float(parts[1].replace(",", ""))
            except ValueError:
                errors.append(f"{ex}: '{parts[1]}' 숫자 아님")
                continue
            note = parts[2] if len(parts) > 2 else ""
            data[today][ex] = {"points": val, "note": note}
            y_val = float(yest_data.get(ex, {}).get("points", 0))
            diff = val - y_val
            pct = (diff / y_val * 100) if y_val > 0 else 0
            sign = "+" if diff >= 0 else ""
            if y_val > 0:
                summary.append(f"<code>{ex:<10s}</code> {val:>10,.0f} ({sign}{diff:,.0f}, {sign}{pct:.1f}%)")
            else:
                summary.append(f"<code>{ex:<10s}</code> {val:>10,.0f} <i>(첫 기록)</i>")
        mp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
        if errors:
            summary.append("\n❌ " + " | ".join(errors))
        await self.send(session, "\n".join(summary))

    async def _cmd_missing(self, session: aiohttp.ClientSession):
        """아직 수동 입력 안 된 거래소 + 확인 URL을 간편 복붙 가능하게."""
        mp_path = PERP_DEX_DIR / "points_manual.json"
        manual = {}
        if mp_path.exists():
            try:
                manual = json.loads(mp_path.read_text(encoding='utf-8'))
            except Exception:
                pass
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        kst = _tz(_td(hours=9))
        today = _dt.now(tz=kst).date().isoformat()
        recorded = set(manual.get(today, {}).keys())
        checklist = [
            ("standx",       "https://dex.standx.com/points"),
            ("nado",         "https://app.nado.xyz/points"),
            ("katana",       "https://pro.katana.trade/points"),
            ("edgex",        "https://pro.edgex.exchange/point-dashboard"),
            ("grvt",         "https://trade.grvt.io/rewards"),
            ("reya",         "https://app.reya.xyz/points"),
            ("aster",        "https://www.asterdex.com/en/stage1/team"),
            ("treadfi",      "https://app.tread.fi/points"),
            ("hotstuff",     "https://hotstuff.io/points"),
            ("variational",  "https://trade.variational.io/rewards"),
            ("lighter",      "https://app.lighter.xyz/public-pools"),
            ("miracle",      "https://miracletrade.com/dashboard"),
            ("hyena",        "https://app.hyena.trade/points"),
        ]
        missing = [(ex, url) for ex, url in checklist if ex not in recorded]
        lines = [f"<b>📋 오늘 미기록 거래소 ({len(missing)})</b>", ""]
        for ex, url in missing:
            lines.append(f"• <b>{ex}</b> — <a href=\"{url}\">{url}</a>")
        if missing:
            lines.append("")
            lines.append("<b>확인 후 한번에 저장:</b>")
            template = "<code>/bulk\n"
            for ex, _ in missing:
                template += f"{ex} 0\n"
            template = template.rstrip() + "</code>"
            lines.append(template)
            lines.append("<i>↑ 복사해서 숫자만 수정 후 전송</i>")
        else:
            lines.append("✅ 오늘 모든 거래소 기록 완료!")
        msg = "\n".join(lines)
        if len(msg) > 3900:
            for i in range(0, len(msg), 3900):
                await self.send(session, msg[i:i+3900])
        else:
            await self.send(session, msg)

    async def _cmd_all_points(self, session: aiohttp.ClientSession):
        """전 거래소 통합 뷰: 자동 API + 수동 입력 + 증감률."""
        try:
            import sys as _sys
            _sys.path.insert(0, str(PERP_DEX_DIR / "scripts"))
            from points_fetcher import fetch_all

            await self.send(session, "전체 조회 중 (약 10초)...")
            live = await fetch_all()

            # 전일 스냅샷 (볼륨/live points)
            snap_path = PERP_DEX_DIR / "points_snapshots.json"
            snapshots = {}
            if snap_path.exists():
                try:
                    snapshots = json.loads(snap_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            dates = sorted(snapshots.keys())
            yest_live = snapshots[dates[-2]].get("live", {}) if len(dates) >= 2 else {}

            # 수동 포인트
            mp_path = PERP_DEX_DIR / "points_manual.json"
            manual = {}
            if mp_path.exists():
                try:
                    manual = json.loads(mp_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            m_dates = sorted(manual.keys())
            m_today = manual[m_dates[-1]] if m_dates else {}
            m_yest = manual[m_dates[-2]] if len(m_dates) >= 2 else {}

            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            kst = _tz(_td(hours=9))
            now_str = _dt.now(tz=kst).strftime("%Y-%m-%d %H:%M")

            lines = [f"<b>📊 전 거래소 포인트/볼륨 — {now_str}</b>"]
            lines.append("")

            # ━━ 자동 조회 ━━
            lines.append("<b>━ 자동 조회 (API) ━</b>")

            # Ethereal (가장 중요)
            eth_sexy_cur = live.get("ethereal_sexy", {}).get("data", [])
            eth_sexy_prev = yest_live.get("ethereal_sexy", {}).get("data", [])
            if eth_sexy_cur and isinstance(eth_sexy_cur, list):
                d = eth_sexy_cur[0]
                total = float(d.get("totalPoints", 0))
                # previousTotalPoints = API가 제공하는 전일값
                prev_api = float(d.get("previousTotalPoints", 0))
                diff = total - prev_api
                pct = (diff / prev_api * 100) if prev_api > 0 else 0
                rank = d.get("rank", 0)
                prev_rank = d.get("previousRank", rank)
                rank_diff = prev_rank - rank
                sign = "+" if diff >= 0 else ""
                arrow = "↑" if rank_diff > 0 else ("↓" if rank_diff < 0 else "=")
                lines.append(
                    f"🔥 <b>Ethereal S{d.get('season')}</b>  "
                    f"<b>{total:,.0f}</b> pts ({sign}{diff:,.0f}, {sign}{pct:.1f}%)"
                )
                lines.append(f"    rank {rank:,} {arrow}{abs(rank_diff):,}")

            # Hyperliquid 3개 지갑
            for key, label in [
                ("hyperliquid_main", "HL main  "),
                ("hyperliquid_sexy", "HL sexy  "),
                ("hyperliquid_hl_b", "HL hl_b  "),
            ]:
                r = live.get(key, {})
                cv = r.get("cum_vlm", 0)
                dv = r.get("daily_volume", 0)
                yr = yest_live.get(key, {})
                ycv = yr.get("cum_vlm", 0)
                diff = cv - ycv
                pct = (diff / ycv * 100) if ycv > 0 else 0
                sign = "+" if diff >= 0 else ""
                if cv > 0 or dv > 0:
                    lines.append(
                        f"<code>{label}</code> ${cv:>10,.0f} ({sign}${diff:,.0f}, {sign}{pct:.2f}%) | today ${dv:,.0f}"
                    )
                else:
                    lines.append(f"<code>{label}</code> 대기 (stage: {r.get('ref_stage','-')})")

            # HyENA
            r = live.get("hyena", {})
            dv = r.get("daily_volume_all", 0)
            av = r.get("account_value", 0)
            lines.append(f"<code>HyENA    </code> 오늘 HL볼륨 ${dv:,.0f} | 계좌 ${av:.2f}")

            # Lighter
            r = live.get("lighter_hyena", {})
            if r.get("account_index"):
                lines.append(f"<code>Lighter  </code> collateral ${r.get('collateral',0):,.2f} (pts 수동)")

            # Pacifica
            r = live.get("pacifica_main", {})
            if r.get("rank"):
                lines.append(f"<code>Pacifica </code> rank {r['rank']} pts {r.get('points')}")
            else:
                lines.append(f"<code>Pacifica </code> 리더보드 외")

            # Miracle
            r = live.get("miracle", {})
            br = r.get("builder_rewards_total", 0)
            if br > 0:
                lines.append(f"<code>Miracle  </code> builder rewards ${br:.2f}")

            # Ostium (자동 실패해도 표시)
            r = live.get("ostium_main", {})
            if r.get("total_volume_usd") is not None:
                lines.append(f"<code>Ostium   </code> vol ${r['total_volume_usd']:,.0f} | trades {r.get('total_trades',0)}")

            # ━━ 수동 입력 ━━
            if m_today:
                lines.append("")
                lines.append("<b>━ 수동 입력 포인트 ━</b>")
                for ex in sorted(m_today.keys()):
                    info = m_today[ex]
                    t = float(info.get("points", 0))
                    y = float(m_yest.get(ex, {}).get("points", 0))
                    d = t - y
                    pct = (d / y * 100) if y > 0 else 0
                    sign = "+" if d >= 0 else ""
                    note = info.get("note", "")
                    nstr = f" <i>({note})</i>" if note else ""
                    if y > 0:
                        lines.append(f"<code>{ex:<10s}</code> {t:>10,.0f} ({sign}{d:,.0f}, {sign}{pct:.1f}%){nstr}")
                    else:
                        lines.append(f"<code>{ex:<10s}</code> {t:>10,.0f} <i>(첫 기록){nstr}</i>")

            # ━━ 미기록 (아직 /setpoint 안 한 거래소) ━━
            all_exchanges = {
                "nado", "standx", "katana", "edgex", "grvt",
                "reya", "aster", "treadfi", "hotstuff", "variational"
            }
            recorded = set(m_today.keys())
            missing = sorted(all_exchanges - recorded)
            if missing:
                lines.append("")
                lines.append(f"<b>━ 미기록 ({len(missing)}개) ━</b>")
                lines.append(", ".join(missing))
                lines.append("<i>→ /pointlinks 에서 URL 확인 후 /setpoint &lt;ex&gt; &lt;value&gt;</i>")

            msg = "\n".join(lines)
            # 4000자 넘으면 분할
            if len(msg) > 3900:
                for i in range(0, len(msg), 3900):
                    await self.send(session, msg[i:i+3900])
            else:
                await self.send(session, msg)
        except Exception as e:
            logger.error(f"/all 에러: {e}", exc_info=True)
            await self.send(session, f"에러: {e}")

    async def _cmd_report(self, session: aiohttp.ClientSession):
        """지금 즉시 일일 리포트 생성 + 전송."""
        script = PERP_DEX_DIR / "scripts" / "daily_points_report.py"
        if not script.exists():
            await self.send(session, "daily_points_report.py 없음")
            return
        await self.send(session, "리포트 생성 중...")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script),
            cwd=str(PERP_DEX_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            # 스크립트가 텔레그램 직접 전송하므로 여기선 완료만 알림
            await self.send(session, "✓ 리포트 전송 완료 (위 메시지 확인)")
        except asyncio.TimeoutError:
            proc.kill()
            await self.send(session, "리포트 타임아웃")

    def _manual_disable(self, exchange: str, reason: str):
        path = PERP_DEX_DIR / "auto_disabled_exchanges.json"
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        data[exchange] = {"reason": reason, "disabled_at": time.time()}
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _manual_enable(self, exchange: str):
        path = PERP_DEX_DIR / "auto_disabled_exchanges.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        data.pop(exchange, None)
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    async def run(self):
        if not BOT_TOKEN or not CHAT_ID:
            logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정")
            return
        logger.info(
            f"Commander 시작. PERP_DEX_DIR={PERP_DEX_DIR}, "
            f"POLYMARKET_DIR={POLYMARKET_DIR}"
        )
        async with aiohttp.ClientSession() as session:
            await self.send(session, "<b>Commander 온라인</b>\n/help 로 명령어 확인")
            while True:
                updates = await self.get_updates(session)
                for upd in updates:
                    self.offset = max(self.offset, upd.get("update_id", 0) + 1)
                    await self.handle(session, upd)


if __name__ == "__main__":
    try:
        asyncio.run(Commander().run())
    except KeyboardInterrupt:
        logger.info("종료")
