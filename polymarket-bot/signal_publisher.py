"""
Signal Publisher — 폴리마켓 시그널을 외부 봇(perp-dex)에 브릿지

폴리마켓 봇이 트레이드 진입할 때 시그널을 JSON 파일로 발행.
perp-dex 봇의 DirectionalTrader가 이 파일을 읽어서 미러 진입.

발행 조건:
- 폴리마켓 봇이 실제 진입(paper/live)할 때만 발행
- blended_prob >= min_publish_prob (기본 0.65)
- 시그널 유효시간 내에서만 perp 봇이 사용

파일 위치: signal_bridge.json (양쪽 봇이 접근 가능한 경로)
"""

import json
import time
import logging
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger("polybot")

# 기본 브릿지 파일 경로 — config에서 오버라이드 가능
DEFAULT_BRIDGE_PATH = Path(__file__).parent / "signal_bridge.json"


@dataclass
class PublishedSignal:
    """perp-dex 봇에 전달할 시그널 데이터"""
    timestamp: float              # 발행 시각 (unix)
    asset: str                    # "BTC" or "ETH"
    direction: str                # "long" or "short"
    blended_prob: float           # 블렌딩 확률 (0-1)
    rule_prob: float              # 룰 기반 확률
    ml_prob: float                # ML 확률
    market_price: float           # 폴리마켓 진입가
    edge: float                   # model_prob - market_price
    entry_price_binance: float    # 바이낸스 현재가
    minutes_to_expiry: float      # 만기까지 남은 분
    window_duration: float        # 윈도우 길이 (분, e.g. 15)
    confidence_tier: str          # "high" / "medium" / "low"
    rsi: float                    # RSI
    bb_position: float            # 볼린저 %B
    trend_strength: float         # 트렌드 강도
    vol_regime: str               # "low" / "medium" / "high"
    signal_id: str                # 고유 ID (추적용)
    mode: str                     # "paper" / "live" / "shadow"
    consumed: bool = False        # perp 봇이 읽었는지
    consumed_at: float = 0.0      # 읽은 시각


class SignalPublisher:
    """폴리마켓 시그널을 JSON 파일로 발행"""

    def __init__(self, bridge_path: str = None, min_publish_prob: float = 0.65):
        self.bridge_path = Path(bridge_path) if bridge_path else DEFAULT_BRIDGE_PATH
        self.min_publish_prob = min_publish_prob
        self._signal_counter = 0
        self._history: list[dict] = []  # 최근 발행 이력 (대시보드용)

    def publish(
        self,
        asset: str,
        direction: str,
        blended_prob: float,
        rule_prob: float,
        ml_prob: float,
        market_price: float,
        entry_price_binance: float,
        minutes_to_expiry: float,
        window_duration: float,
        signal_data: dict = None,
        mode: str = "paper",
    ) -> Optional[PublishedSignal]:
        """
        시그널 발행. 조건 미달이면 None 반환.

        Parameters:
            asset: "BTC" or "ETH"
            direction: "long" or "short" (UP=long, DOWN=short)
            blended_prob: ML+룰 블렌딩 확률
            rule_prob: 룰 기반 확률
            ml_prob: ML 모델 확률
            market_price: 폴리마켓 토큰 가격 (0-1)
            entry_price_binance: 바이낸스 현재 BTC/ETH 가격
            minutes_to_expiry: 만기까지 분
            window_duration: 윈도우 길이 (15, 60 등)
            signal_data: PriceEngine.get_signal_dict() 출력
            mode: paper/live/shadow
        """
        # 최소 확률 필터
        if blended_prob < self.min_publish_prob:
            return None

        # 만기 너무 가까우면 perp에서 진입해봤자 의미 없음
        if minutes_to_expiry < 3.0:
            return None

        edge = blended_prob - market_price
        sig = signal_data or {}

        # 컨피던스 티어 분류
        if blended_prob >= 0.75:
            tier = "high"
        elif blended_prob >= 0.65:
            tier = "medium"
        else:
            tier = "low"

        self._signal_counter += 1
        signal_id = f"poly_{asset}_{int(time.time())}_{self._signal_counter}"

        published = PublishedSignal(
            timestamp=time.time(),
            asset=asset.upper(),
            direction=direction.lower(),
            blended_prob=round(blended_prob, 4),
            rule_prob=round(rule_prob, 4),
            ml_prob=round(ml_prob, 4),
            market_price=round(market_price, 4),
            edge=round(edge, 4),
            entry_price_binance=round(entry_price_binance, 2),
            minutes_to_expiry=round(minutes_to_expiry, 1),
            window_duration=window_duration,
            confidence_tier=tier,
            rsi=round(sig.get("rsi", 0), 2),
            bb_position=round(sig.get("direction_bias", 0), 4),
            trend_strength=round(sig.get("trend_strength", 0), 4),
            vol_regime=sig.get("vol_regime", "medium"),
            signal_id=signal_id,
            mode=mode,
        )

        self._write_bridge(published)
        self._history.append(asdict(published))
        if len(self._history) > 100:
            self._history = self._history[-50:]

        logger.info(
            f"[PUBLISH] {asset} {direction.upper()} "
            f"prob={blended_prob:.3f} edge={edge:.3f} "
            f"tier={tier} expiry={minutes_to_expiry:.0f}m "
            f"id={signal_id}"
        )
        return published

    def _write_bridge(self, signal: PublishedSignal):
        """브릿지 파일에 시그널 기록 (atomic write)"""
        try:
            # 기존 시그널 로드 → active 리스트 유지
            existing = self._read_bridge()
            active = existing.get("active_signals", [])

            # 만료된 시그널 제거 (15분 초과)
            now = time.time()
            active = [s for s in active if now - s["timestamp"] < s.get("window_duration", 15) * 60]

            # 같은 asset의 이전 시그널 제거 (최신만 유지)
            active = [s for s in active if s["asset"] != signal.asset]

            # 새 시그널 추가
            active.append(asdict(signal))

            data = {
                "last_updated": now,
                "publisher": "polymarket-bot",
                "active_signals": active,
            }

            # atomic write: tmp → rename
            tmp_path = self.bridge_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_path.replace(self.bridge_path)

        except Exception as e:
            logger.error(f"[PUBLISH] Bridge write failed: {e}")

    def _read_bridge(self) -> dict:
        """브릿지 파일 읽기"""
        try:
            if self.bridge_path.exists():
                return json.loads(self.bridge_path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"active_signals": []}

    def clear_expired(self):
        """만료 시그널 정리 (주기적 호출)"""
        try:
            data = self._read_bridge()
            now = time.time()
            active = data.get("active_signals", [])
            before = len(active)
            active = [s for s in active if now - s["timestamp"] < s.get("window_duration", 15) * 60]
            if len(active) != before:
                data["active_signals"] = active
                data["last_updated"] = now
                tmp_path = self.bridge_path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                tmp_path.replace(self.bridge_path)
        except Exception:
            pass

    def get_stats(self) -> dict:
        """발행 통계 (대시보드용)"""
        return {
            "total_published": self._signal_counter,
            "recent_history": self._history[-10:],
            "bridge_path": str(self.bridge_path),
            "min_publish_prob": self.min_publish_prob,
        }
