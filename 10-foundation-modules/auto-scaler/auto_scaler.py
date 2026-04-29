"""
Auto Scaler — 자본 기반 전략 파라미터 자동 조정

원래 전략 설정값(config.yaml strategy 섹션)은 "기준 자본" 기준 최적값.
실제 계좌 잔고를 조회한 뒤, 자본 비율에 따라
trading_margin과 trading_limit_count만 비례 조정.

나머지 파라미터(leverage, close_trigger, stop_loss, momentum 등)는
원래 값 100% 유지.

사용법:
    scaler = AutoScaler(config)
    scaled_config = scaler.scale_config(base_config, equity, exchange_name)
"""

import logging
from .pair_trader import PairTraderConfig

logger = logging.getLogger(__name__)


class AutoScaler:
    """자본 기반 전략 파라미터 스케일러"""

    def __init__(self, config: dict):
        scaling = config.get("scaling", {})

        # 기준 자본 ($) — strategy 값이 이 자본 기준으로 최적화됨
        self.reference_equity = scaling.get("reference_equity", 20000)

        # 계좌 잔고 중 전략에 사용할 비율 (0.0 ~ 1.0)
        self.allocation_ratio = scaling.get("allocation_ratio", 0.8)

        # 최소 마진 ($) — 이보다 작으면 최소값으로 고정
        self.min_margin = scaling.get("min_margin", 5.0)

        # 최소 마진 비율 — 사용가능 자본의 최소 N%를 1회 마진으로 보장 (소액 계좌 보호)
        self.min_margin_pct = scaling.get("min_margin_pct", 0.1)

        # 최대 위험 비율 — DCA 총 마진이 사용가능 자본의 이 비율까지만
        self.max_risk_ratio = scaling.get("max_risk_ratio", 0.5)

        # 활성화 여부
        self.enabled = scaling.get("enabled", True)

    async def get_equity(self, wrapper, exchange_name: str) -> float:
        """
        거래소 잔고 조회.
        여러 메서드를 순서대로 시도하여 계좌 잔고(equity)를 반환.
        실패 시 -1 반환 (호출 측에서 manual_equity 사용).
        """
        methods = ["get_balance", "get_collateral", "get_account_info", "get_account_value"]

        for method_name in methods:
            method = getattr(wrapper, method_name, None)
            if not method or not callable(method):
                continue

            try:
                result = await method()

                # 숫자 직접 반환
                if isinstance(result, (int, float)) and result > 0:
                    logger.info(f"[{exchange_name}] 잔고 조회 ({method_name}): ${float(result):.2f}")
                    return float(result)

                # dict 반환 — 다양한 키 패턴 매칭
                if isinstance(result, dict):
                    key_priority = [
                        "total_collateral", "available_collateral",
                        "equity", "totalEquity", "total_equity",
                        "balance", "availableBalance", "free",
                        "totalMarginBalance", "accountValue",
                        "portfolio_value",
                    ]
                    for key in key_priority:
                        if key in result and result[key] is not None:
                            val = float(result[key])
                            if val > 0:
                                logger.info(
                                    f"[{exchange_name}] 잔고 조회 "
                                    f"({method_name}.{key}): ${val:.2f}"
                                )
                                return val

            except Exception as e:
                logger.debug(f"[{exchange_name}] {method_name} 실패: {e}")
                continue

        return -1

    def scale_config(
        self, base_config: PairTraderConfig, equity: float, exchange_name: str
    ) -> PairTraderConfig:
        """
        실제 자본에 맞게 PairTraderConfig 조정.

        조정 대상 (2개만):
          - trading_margin: 기준자본 대비 비례 축소/확대
          - trading_limit_count: 총 위험이 잔고의 max_risk_ratio를 넘지 않게

        유지 (원래 값 100% 보존):
          - leverage
          - close_trigger_percent
          - entry_trigger_percent
          - stop_loss_percent
          - min_momentum_diff
          - 기타 모든 파라미터
        """
        if not self.enabled:
            logger.info(f"[{exchange_name}] 오토스케일링 비활성화 — 원래 설정 사용")
            return base_config

        # 사용 가능 자본
        usable_equity = equity * self.allocation_ratio

        # 기준 자본 대비 비율
        ratio = usable_equity / self.reference_equity
        ratio = max(ratio, 0.001)  # 최소 0.1%

        logger.info(
            f"[{exchange_name}] 오토스케일링: "
            f"잔고=${equity:.2f}, 배분={self.allocation_ratio:.0%}, "
            f"사용가능=${usable_equity:.2f}, "
            f"기준=${self.reference_equity}, 비율={ratio:.4f}"
        )

        # ── 1. trading_margin 스케일링 ──
        scaled_margin = base_config.trading_margin * ratio
        # 소액 계좌 보호: 사용가능 자본의 min_margin_pct 이상 보장
        pct_floor = usable_equity * self.min_margin_pct
        scaled_margin = max(scaled_margin, self.min_margin, pct_floor)
        scaled_margin = round(scaled_margin, 2)

        # ── 2. trading_limit_count 조정 ──
        # 총 마진이 사용 가능 자본의 max_risk_ratio를 넘지 않게
        max_total_margin = usable_equity * self.max_risk_ratio
        if scaled_margin > 0:
            max_count = max(int(max_total_margin / scaled_margin), 1)
        else:
            max_count = 1
        scaled_count = min(base_config.trading_limit_count, max_count)

        # ── 새 config 생성 ──
        new_config = PairTraderConfig(
            # 스케일링 적용 (이 2개만 변경)
            trading_margin=scaled_margin,
            trading_limit_count=scaled_count,

            # 원래 값 100% 유지
            coin1=base_config.coin1,
            coin2=base_config.coin2,
            leverage=base_config.leverage,
            entry_trigger_percent=base_config.entry_trigger_percent,
            close_trigger_percent=base_config.close_trigger_percent,
            stop_loss_percent=base_config.stop_loss_percent,
            momentum_option=base_config.momentum_option,
            min_momentum_diff=base_config.min_momentum_diff,
            chart_time=base_config.chart_time,
            candle_limit=base_config.candle_limit,
            min_candles=base_config.min_candles,
            scan_interval=base_config.scan_interval,
            limit_order=base_config.limit_order,
            risk=base_config.risk,
        )

        # ── 수수료 경고 (정보 제공용, 값 변경 안 함) ──
        fee_rate = 0.035  # 보수적 가정 (taker)
        fee_pct = 4 * base_config.leverage * fee_rate
        if base_config.close_trigger_percent < fee_pct:
            logger.warning(
                f"[{exchange_name}] ⚠ 수수료 주의: "
                f"익절 {base_config.close_trigger_percent}% < "
                f"예상 수수료 {fee_pct:.1f}% (마진대비, taker {fee_rate}% 가정). "
                f"Maker 주문이나 거래소 수수료 할인 확인 권장."
            )

        # ── 변경 사항 로깅 ──
        changes = []
        if new_config.trading_margin != base_config.trading_margin:
            changes.append(f"마진: ${base_config.trading_margin} → ${new_config.trading_margin}")
        if new_config.trading_limit_count != base_config.trading_limit_count:
            changes.append(f"DCA: {base_config.trading_limit_count}회 → {new_config.trading_limit_count}회")

        if changes:
            logger.info(f"[{exchange_name}] 스케일링 적용: {', '.join(changes)}")
        else:
            logger.info(f"[{exchange_name}] 스케일링 불필요 — 원래 설정과 동일")

        # 최종 설정 요약
        total_risk = new_config.trading_margin * new_config.trading_limit_count
        notional = total_risk * new_config.leverage
        logger.info(
            f"[{exchange_name}] 최종 설정: "
            f"${new_config.trading_margin}/회 × {new_config.trading_limit_count}회 "
            f"= 총마진${total_risk:.0f} (잔고의 {total_risk/equity*100:.1f}%), "
            f"명목=${notional:,.0f}, "
            f"레버={new_config.leverage}x, "
            f"익절={new_config.close_trigger_percent}%, "
            f"손절={new_config.stop_loss_percent}%"
        )

        return new_config
