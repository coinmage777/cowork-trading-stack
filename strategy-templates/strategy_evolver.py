"""
Strategy Evolver — autoresearch 스타일 전략 자가진화 엔진

Karpathy autoresearch 컨셉 적용:
- 고정 시간/데이터 예산으로 실험 (5분 훈련 → 7일 백테스트)
- 시그널 조합 + 가중치를 자동 탐색
- 결과가 좋으면 유지, 아니면 폐기
- 모든 실험을 journal에 기록

기존 auto_optimizer와의 차이:
- auto_optimizer: 파라미터 숫자만 조정 (close_trigger, stop_loss 등)
- strategy_evolver: 어떤 시그널을 쓸지, 가중치를 어떻게 배분할지 탐색
  → 두 개를 같이 돌리면: evolver가 시그널 조합 결정 → optimizer가 파라미터 미세 조정

사용법 (standalone):
    python -m strategies.strategy_evolver --config config.yaml --once
    python -m strategies.strategy_evolver --config config.yaml

multi_runner 통합:
    from strategies.strategy_evolver import StrategyEvolver
    evolver = StrategyEvolver(config_path)
    asyncio.create_task(evolver.run())
"""

import asyncio
import json
import logging
import math
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from .backtester import BacktestConfig, BacktestEngine, fetch_candles_bulk, load_config_from_yaml
from .signals import (
    ALL_SIGNALS,
    DEFAULT_WEIGHTS,
    SignalRegistry,
    CompositeSignal,
)

logger = logging.getLogger(__name__)

# OOS/IS 비율 최소 임계값 — 과적합 방지
MIN_OOS_IS_RATIO = 0.3  # OOS PnL이 IS PnL의 30% 이상이어야 채택


# ──────────────────────────────────────────
# 실험 저널
# ──────────────────────────────────────────

class ExperimentJournal:
    """
    autoresearch 스타일 실험 기록.
    모든 시도를 기록해서 패턴 분석 가능.
    """

    def __init__(self, path: str = "evolution_journal.json"):
        self.path = Path(path)
        self.entries: list[dict] = self._load()

    def _load(self) -> list:
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.entries, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"  EVOL  | 저널 저장 실패: {e}")

    def log_experiment(self, experiment: dict):
        experiment["timestamp"] = datetime.now().isoformat()
        experiment["id"] = len(self.entries) + 1
        self.entries.append(experiment)

        # 최근 500개만 유지
        if len(self.entries) > 500:
            self.entries = self.entries[-500:]
        self.save()

    def get_best_configs(self, top_n: int = 5) -> list[dict]:
        """성공한 실험 중 상위 N개"""
        successful = [e for e in self.entries if e.get("adopted")]
        successful.sort(key=lambda x: x.get("composite_score", 0), reverse=True)
        return successful[:top_n]

    def get_signal_win_rates(self) -> dict:
        """시그널별 채택률 — 어떤 시그널이 자주 성공하는지"""
        signal_stats = {}
        for entry in self.entries:
            weights = entry.get("weights", {})
            adopted = entry.get("adopted", False)
            for signal_name, weight in weights.items():
                if weight > 0:
                    if signal_name not in signal_stats:
                        signal_stats[signal_name] = {"tried": 0, "adopted": 0}
                    signal_stats[signal_name]["tried"] += 1
                    if adopted:
                        signal_stats[signal_name]["adopted"] += 1

        # 채택률 계산
        for name, stats in signal_stats.items():
            stats["win_rate"] = (
                round(stats["adopted"] / stats["tried"] * 100, 1)
                if stats["tried"] > 0 else 0
            )
        return signal_stats

    def get_summary(self) -> dict:
        total = len(self.entries)
        adopted = sum(1 for e in self.entries if e.get("adopted"))
        return {
            "total_experiments": total,
            "adopted": adopted,
            "adoption_rate": round(adopted / total * 100, 1) if total > 0 else 0,
            "signal_stats": self.get_signal_win_rates(),
        }


# ──────────────────────────────────────────
# 시그널 조합 백테스터 (signals + backtest 통합)
# ──────────────────────────────────────────

class SignalBacktestEngine:
    """
    기존 BacktestEngine을 확장.
    시그널 모듈로 방향을 결정하고, 나머지 로직(DCA, 익절, 손절)은 기존 그대로.
    """

    def __init__(self, config: BacktestConfig, registry: SignalRegistry):
        self.config = config
        self.registry = registry
        self.engine = BacktestEngine(config)

    def run(self, candles1: list, candles2: list) -> dict:
        """
        시그널 기반 백테스트.
        _analyze_direction만 시그널 모듈로 교체, 나머지는 BacktestEngine 그대로.
        """
        min_len = min(len(candles1), len(candles2))
        if min_len < self.config.min_candles + 10:
            return {"error": f"캔들 부족: {min_len}"}

        # BacktestEngine 초기화
        engine = BacktestEngine(self.config)
        start_idx = self.config.min_candles

        for i in range(start_idx, min_len, self.config.scan_interval):
            price1 = candles1[i]["close"]
            price2 = candles2[i]["close"]

            if engine.entry_count == 0:
                # 시그널 모듈로 방향 결정 (기존 모멘텀 대체)
                window1 = candles1[max(0, i - self.config.min_candles - 50):i + 1]
                window2 = candles2[max(0, i - self.config.min_candles - 50):i + 1]

                composite = self.registry.evaluate(
                    window1, window2,
                    min_candles=self.config.min_candles,
                    min_signal_strength=self.config.min_momentum_diff,
                )

                if composite.direction:
                    engine.direction = composite.direction
                    engine._open_entry(i, price1, price2)
            else:
                # 포지션 있을 때는 기존 로직 그대로
                pnl_pct = engine._calc_pnl(price1, price2)

                if pnl_pct < engine._trade_max_dd:
                    engine._trade_max_dd = pnl_pct
                if pnl_pct > engine._trade_max_profit:
                    engine._trade_max_profit = pnl_pct

                if pnl_pct >= self.config.close_trigger_percent:
                    engine._close_position(i, price1, price2, pnl_pct, "profit_target")
                elif pnl_pct <= -self.config.stop_loss_percent:
                    engine._close_position(i, price1, price2, pnl_pct, "stop_loss")
                elif (pnl_pct <= -self.config.entry_trigger_percent and
                      engine.entry_count < self.config.trading_limit_count):
                    if engine.equity > self.config.trading_margin:
                        engine._open_entry(i, price1, price2)

            engine.equity_curve.append(engine.equity)

        # 잔여 포지션 강제 청산
        if engine.entry_count > 0:
            price1 = candles1[min_len - 1]["close"]
            price2 = candles2[min_len - 1]["close"]
            pnl_pct = engine._calc_pnl(price1, price2)
            engine._close_position(min_len - 1, price1, price2, pnl_pct, "end_of_data")

        return engine._summary()


# ──────────────────────────────────────────
# Strategy Evolver 메인 클래스
# ──────────────────────────────────────────

class StrategyEvolver:
    """
    autoresearch 스타일 전략 진화 엔진.

    매 사이클마다:
    1. 시그널 가중치 조합 후보 생성 (랜덤 + 유전 알고리즘)
    2. 각 후보를 3단계 백테스트 검증
    3. 최고 성과 조합을 config에 반영
    4. 모든 실험을 저널에 기록
    """

    def __init__(
        self,
        config_path: str = "config.yaml",
        interval_hours: float = 12.0,
        lookback_days: int = 7,
        population_size: int = 20,
    ):
        self.config_path = config_path
        self.interval_hours = interval_hours
        self.lookback_days = lookback_days
        self.stability_days = 30
        self.population_size = population_size
        self.running = False

        # 저널
        journal_path = Path(config_path).parent / "evolution_journal.json"
        self.journal = ExperimentJournal(str(journal_path))

        # 현재 활성 가중치
        self._current_weights = self._load_weights()

        # 최소 가중치 플로어 (optional)
        self.min_weights = self._load_min_weights()

    def _load_weights(self) -> dict:
        """config.yaml에서 시그널 가중치 로드"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            evolver_cfg = raw.get("strategy_evolver", {})
            return evolver_cfg.get("signal_weights", DEFAULT_WEIGHTS.copy())
        except Exception:
            return DEFAULT_WEIGHTS.copy()

    def _load_min_weights(self) -> dict:
        """config.yaml에서 최소 가중치 플로어 로드 (선택사항)"""
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            evolver_cfg = raw.get("strategy_evolver", {})
            return evolver_cfg.get("min_signal_weights", {})
        except Exception:
            return {}

    # ──────────────────────────────────────
    # 후보 생성 (유전 알고리즘 + 랜덤)
    # ──────────────────────────────────────

    def _generate_candidates(self) -> list[dict]:
        """
        가중치 조합 후보 생성.
        - 50% 랜덤 탐색 (다양성)
        - 30% 돌연변이 (현재 최적 기반)
        - 20% 엘리트 교차 (과거 성공 조합 기반)
        """
        candidates = []
        signal_names = list(ALL_SIGNALS.keys())
        n = self.population_size

        # 현재 가중치도 후보에 포함 (baseline)
        candidates.append(self._current_weights.copy())

        # 1. 랜덤 탐색 (50%)
        for _ in range(int(n * 0.5)):
            weights = {}
            # 2~4개 시그널 랜덤 활성화
            active_count = random.randint(2, min(4, len(signal_names)))
            active = random.sample(signal_names, active_count)
            for name in signal_names:
                if name in active:
                    weights[name] = round(random.uniform(0.1, 1.0), 2)
                else:
                    weights[name] = 0.0
            # 정규화
            weights = self._normalize_weights(weights)
            candidates.append(weights)

        # 2. 돌연변이 (30%) — 현재 가중치에서 랜덤 변이
        for _ in range(int(n * 0.3)):
            weights = self._current_weights.copy()
            # 1~2개 시그널 가중치 변경
            mutations = random.randint(1, 2)
            for _ in range(mutations):
                target = random.choice(signal_names)
                current_val = weights.get(target, 0.0)
                # ±0.3 범위 변이
                new_val = max(0.0, min(1.0, current_val + random.uniform(-0.3, 0.3)))
                weights[target] = round(new_val, 2)
            # 간헐적으로 새 시그널 활성화
            if random.random() < 0.3:
                inactive = [n for n in signal_names if weights.get(n, 0) == 0]
                if inactive:
                    weights[random.choice(inactive)] = round(random.uniform(0.1, 0.5), 2)
            weights = self._normalize_weights(weights)
            candidates.append(weights)

        # 3. 엘리트 교차 (20%) — 과거 성공 조합끼리 교차
        best_configs = self.journal.get_best_configs(top_n=5)
        for _ in range(int(n * 0.2)):
            if len(best_configs) >= 2:
                parent1 = random.choice(best_configs).get("weights", DEFAULT_WEIGHTS)
                parent2 = random.choice(best_configs).get("weights", DEFAULT_WEIGHTS)
                child = {}
                for name in signal_names:
                    # 50% 확률로 부모1 or 부모2 가중치
                    if random.random() < 0.5:
                        child[name] = parent1.get(name, 0.0)
                    else:
                        child[name] = parent2.get(name, 0.0)
                child = self._normalize_weights(child)
                candidates.append(child)
            else:
                # 과거 데이터 부족 → 랜덤으로 대체
                weights = {}
                active = random.sample(signal_names, random.randint(2, 4))
                for name in signal_names:
                    weights[name] = round(random.uniform(0.2, 0.8), 2) if name in active else 0.0
                candidates.append(self._normalize_weights(weights))

        return candidates

    def _normalize_weights(self, weights: dict) -> dict:
        """
        가중치 합이 1.0이 되도록 정규화.
        min_signal_weights가 설정돼 있으면 최소 가중치 플로어 강제.
        """
        total = sum(v for v in weights.values() if v > 0)
        if total == 0:
            # 전부 0이면 momentum_diff만 활성화
            return DEFAULT_WEIGHTS.copy()

        # 기본 정규화
        normalized = {k: round(v / total, 3) if v > 0 else 0.0 for k, v in weights.items()}

        # NEW: 최소 가중치 플로어 강제
        if self.min_weights:
            # 플로어를 만족하는 시그널들 식별
            floored = {}
            for signal, min_w in self.min_weights.items():
                if signal in normalized:
                    if normalized[signal] < min_w:
                        floored[signal] = min_w
                    else:
                        floored[signal] = normalized[signal]

            floored_sum = sum(floored.values())

            # 플로어가 1.0을 초과하면 클립
            if floored_sum > 1.0:
                # 플로어 가중치를 비율대로 축소
                scale = 1.0 / floored_sum
                floored = {k: round(v * scale, 3) for k, v in floored.items()}
            else:
                # 플로어가 1.0 이하면, 남은 가중치를 플로어 없는 시그널에 분배
                free = {k: v for k, v in normalized.items() if k not in self.min_weights}
                free_sum = sum(free.values())
                remaining = 1.0 - floored_sum

                if free_sum > 0 and remaining > 0:
                    # 플로어 없는 시그널들의 상대 비율 유지하며 remaining 분배
                    free = {k: round(v / free_sum * remaining, 3) for k, v in free.items()}
                    normalized = {**floored, **free}
                    # 반올림 오차 보정
                    current_sum = sum(normalized.values())
                    if current_sum != 1.0:
                        diff = round(1.0 - current_sum, 3)
                        # 가장 큰 값에 보정 적용
                        max_key = max(normalized, key=normalized.get)
                        normalized[max_key] = round(normalized[max_key] + diff, 3)
                    return normalized
                else:
                    normalized = floored

        return normalized

    # ──────────────────────────────────────
    # 메인 진화 사이클
    # ──────────────────────────────────────

    async def evolve_cycle(self) -> bool:
        """
        한 사이클 실행:
        1. 후보 생성
        2. 7일 백테스트 (Stage 1)
        3. OOS 검증 (Stage 2)
        4. 30일 안정성 (Stage 3)
        5. 최적 조합 채택
        """
        tag = "EVOL "

        # 설정 로드
        base_config = load_config_from_yaml(self.config_path)

        # 캔들 다운로드
        download_days = max(self.lookback_days, self.stability_days) + 1
        logger.info(f"  {tag} | 캔들 다운로드 ({download_days}일)...")

        try:
            candles1_full, candles2_full = await asyncio.gather(
                fetch_candles_bulk(base_config.coin1, base_config.chart_time, download_days),
                fetch_candles_bulk(base_config.coin2, base_config.chart_time, download_days),
            )
        except Exception as e:
            logger.error(f"  {tag} | 캔들 다운로드 실패: {e}")
            return False

        if not candles1_full or not candles2_full:
            logger.error(f"  {tag} | 캔들 데이터 비어있음")
            return False

        candles_per_day = (24 * 60) // base_config.chart_time
        recent_count = self.lookback_days * candles_per_day
        candles1_recent = candles1_full[-recent_count:]
        candles2_recent = candles2_full[-recent_count:]

        # 후보 생성
        candidates = self._generate_candidates()
        logger.info(f"  {tag} | {len(candidates)}개 시그널 조합 후보 생성")

        # ━━━ Stage 1: In-Sample (7일) ━━━
        logger.info(f"  {tag} | [Stage 1] In-Sample 테스트...")

        # 현재 설정 baseline
        baseline_registry = SignalRegistry(self._current_weights)
        baseline_registry.register_active()
        baseline_engine = SignalBacktestEngine(base_config, baseline_registry)
        baseline_result = baseline_engine.run(candles1_recent, candles2_recent)
        baseline_pnl = baseline_result.get("total_pnl_usd", 0)

        logger.info(
            f"  {tag} | [Stage 1] Baseline: PnL=${baseline_pnl:+,.0f} "
            f"PF={baseline_result.get('profit_factor', 0):.2f} "
            f"({baseline_result.get('total_trades', 0)}건)"
        )

        stage1_passed = []
        for i, weights in enumerate(candidates):
            registry = SignalRegistry(weights)
            registry.register_active()
            engine = SignalBacktestEngine(base_config, registry)
            result = engine.run(candles1_recent, candles2_recent)

            # Stage 1 필터
            trades = result.get("total_trades", 0)
            pnl = result.get("total_pnl_usd", 0)
            pf = result.get("profit_factor", 0)
            wr = result.get("win_rate", 0)
            mdd = result.get("max_drawdown_percent", 100)

            if trades >= 5 and wr >= 50 and pf >= 1.3 and mdd <= 15:
                stage1_passed.append({
                    "weights": weights,
                    "result": result,
                    "pnl": pnl,
                })

        logger.info(f"  {tag} | [Stage 1] {len(stage1_passed)}/{len(candidates)}개 통과")

        if not stage1_passed:
            self.journal.log_experiment({
                "action": "s1_no_pass",
                "adopted": False,
                "weights": self._current_weights,
                "baseline_pnl": baseline_pnl,
                "candidates_tested": len(candidates),
            })
            return False

        # PnL 상위 10개만
        stage1_passed.sort(key=lambda x: x["pnl"], reverse=True)
        stage1_passed = stage1_passed[:10]

        # ━━━ Stage 2: OOS (70/30 분할) ━━━
        logger.info(f"  {tag} | [Stage 2] Out-of-Sample 검증...")

        split_idx = int(len(candles1_recent) * 0.7)
        oos1 = candles1_recent[split_idx:]
        oos2 = candles2_recent[split_idx:]

        stage2_passed = []
        for cand in stage1_passed:
            registry = SignalRegistry(cand["weights"])
            registry.register_active()
            engine = SignalBacktestEngine(base_config, registry)
            oos_result = engine.run(oos1, oos2)

            oos_pnl = oos_result.get("total_pnl_usd", 0)
            oos_trades = oos_result.get("total_trades", 0)
            oos_wr = oos_result.get("win_rate", 0)

            active_signals = [k for k, v in cand["weights"].items() if v > 0]
            logger.info(
                f"  {tag} | [Stage 2]   "
                f"signals={active_signals} → "
                f"OOS PnL=${oos_pnl:+,.0f} 승률={oos_wr:.0f}% "
                f"{'PASS' if (oos_trades >= 2 and oos_pnl >= 0 and oos_wr >= 40) else 'FAIL'}"
            )

            if oos_trades >= 2 and oos_pnl >= 0 and oos_wr >= 40:
                cand["oos_result"] = oos_result
                cand["oos_pnl"] = oos_pnl
                stage2_passed.append(cand)

        logger.info(f"  {tag} | [Stage 2] {len(stage2_passed)}/{len(stage1_passed)}개 통과")

        if not stage2_passed:
            self.journal.log_experiment({
                "action": "s2_all_fail",
                "adopted": False,
                "weights": stage1_passed[0]["weights"],
                "baseline_pnl": baseline_pnl,
                "best_s1_pnl": stage1_passed[0]["pnl"],
            })
            return False

        # ━━━ Stage 3: 30일 안정성 ━━━
        logger.info(f"  {tag} | [Stage 3] 장기 안정성 검증 ({self.stability_days}일)...")

        final_candidates = []
        for cand in stage2_passed:
            registry = SignalRegistry(cand["weights"])
            registry.register_active()
            engine = SignalBacktestEngine(base_config, registry)
            s3_result = engine.run(candles1_full, candles2_full)

            s3_pnl = s3_result.get("total_pnl_usd", 0)
            s3_trades = s3_result.get("total_trades", 0)
            s3_pf = s3_result.get("profit_factor", 0)
            s3_mdd = s3_result.get("max_drawdown_percent", 100)

            passed = s3_trades >= 8 and s3_pnl >= 0 and s3_pf >= 1.2 and s3_mdd <= 20

            active_signals = [k for k, v in cand["weights"].items() if v > 0]
            logger.info(
                f"  {tag} | [Stage 3]   "
                f"signals={active_signals} → "
                f"30일 PnL=${s3_pnl:+,.0f} PF={s3_pf:.2f} MDD={s3_mdd:.1f}% "
                f"{'PASS' if passed else 'FAIL'}"
            )

            if passed:
                cand["stability_result"] = s3_result
                cand["composite_score"] = (
                    cand["pnl"] * 0.4 +
                    cand["oos_pnl"] * 0.3 +
                    s3_pnl * 0.3
                )
                final_candidates.append(cand)

        logger.info(f"  {tag} | [Stage 3] {len(final_candidates)}/{len(stage2_passed)}개 최종 통과")

        if not final_candidates:
            self.journal.log_experiment({
                "action": "s3_all_fail",
                "adopted": False,
                "weights": stage2_passed[0]["weights"],
                "baseline_pnl": baseline_pnl,
            })
            return False

        # ━━━ 최종 채택 ━━━
        final_candidates.sort(key=lambda x: x["composite_score"], reverse=True)
        best = final_candidates[0]

        # OOS/IS 비율 체크 — 과적합 방지
        is_pnl = best["pnl"]
        oos_pnl = best["oos_pnl"]
        if is_pnl > 0:
            oos_is_ratio = oos_pnl / is_pnl
            if oos_is_ratio < MIN_OOS_IS_RATIO:
                logger.info(
                    f"  {tag} | OOS/IS 비율 {oos_is_ratio:.1%} < {MIN_OOS_IS_RATIO:.0%} → 과적합 위험, 기각"
                )
                self.journal.log_experiment({
                    "action": "oos_is_ratio_fail",
                    "adopted": False,
                    "weights": best["weights"],
                    "is_pnl": is_pnl,
                    "oos_pnl": oos_pnl,
                    "oos_is_ratio": round(oos_is_ratio, 3),
                })
                return False

        # 기존 대비 개선 확인
        if baseline_pnl > 0:
            improvement = (best["pnl"] - baseline_pnl) / baseline_pnl
        elif baseline_pnl < 0:
            improvement = (best["pnl"] - baseline_pnl) / abs(baseline_pnl)
        else:
            improvement = 1.0 if best["pnl"] > 0 else 0.0

        if improvement < 0.1 and baseline_pnl > 0:
            logger.info(f"  {tag} | 개선 {improvement*100:.1f}% < 10% → 현재 유지")
            self.journal.log_experiment({
                "action": "below_threshold",
                "adopted": False,
                "weights": best["weights"],
                "baseline_pnl": baseline_pnl,
                "best_pnl": best["pnl"],
                "improvement": round(improvement * 100, 1),
            })
            return False

        # 채택!
        active = [k for k, v in best["weights"].items() if v > 0]
        logger.info(f"  {tag} | 3단계 모두 통과! 새 시그널 조합 채택")
        logger.info(f"  {tag} |   활성 시그널: {active}")
        logger.info(f"  {tag} |   가중치: {best['weights']}")
        logger.info(
            f"  {tag} |   7일=${best['pnl']:+,.0f} OOS=${best['oos_pnl']:+,.0f} "
            f"종합={best['composite_score']:+,.0f}"
        )

        # config.yaml 업데이트
        self._update_config(best["weights"])
        self._current_weights = best["weights"]

        # 저널 기록
        self.journal.log_experiment({
            "action": "adopted",
            "adopted": True,
            "weights": best["weights"],
            "active_signals": active,
            "baseline_pnl": baseline_pnl,
            "best_pnl": best["pnl"],
            "oos_pnl": best["oos_pnl"],
            "composite_score": best["composite_score"],
            "improvement": round(improvement * 100, 1),
            "stages_passed": "S1+S2+S3",
            "s1_result": {
                "pnl": best["result"].get("total_pnl_usd"),
                "trades": best["result"].get("total_trades"),
                "win_rate": best["result"].get("win_rate"),
                "profit_factor": best["result"].get("profit_factor"),
            },
        })

        return True

    def _update_config(self, weights: dict):
        """config.yaml에 시그널 가중치 저장 (lock 보호)"""
        from .config_lock import ConfigLock
        tag = "EVOL "
        lock = ConfigLock(self.config_path)
        if not lock.acquire("evolver"):
            logger.error(f"  {tag} | config 잠금 획득 실패 — 업데이트 건너뜀")
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f)

            if "strategy_evolver" not in config:
                config["strategy_evolver"] = {}

            config["strategy_evolver"]["signal_weights"] = weights
            config["strategy_evolver"]["last_updated"] = datetime.now().isoformat()

            with open(self.config_path, "w", encoding="utf-8") as f:
                yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

            logger.info(f"  {tag} | config.yaml 시그널 가중치 업데이트 완료")
        except Exception as e:
            logger.error(f"  {tag} | config 업데이트 실패: {e}")
        finally:
            lock.release("evolver")

    # ──────────────────────────────────────
    # 메인 루프
    # ──────────────────────────────────────

    async def run(self):
        """데몬 모드 — 주기적으로 진화 사이클 실행"""
        self.running = True
        tag = "EVOL "
        logger.info(f"  {tag} | Strategy Evolver 시작 | 주기={self.interval_hours}h")

        # 첫 실행은 20분 후 (봇 + optimizer 안정화 대기)
        await asyncio.sleep(1200)

        while self.running:
            try:
                logger.info(f"  {tag} | === 진화 사이클 시작 ===")
                changed = await self.evolve_cycle()

                if changed:
                    logger.info(f"  {tag} | 시그널 조합 업데이트 완료!")
                else:
                    logger.info(f"  {tag} | 현재 조합 유지")

            except Exception as e:
                logger.error(f"  {tag} | 진화 사이클 에러: {e}")

            await asyncio.sleep(self.interval_hours * 3600)

    def stop(self):
        self.running = False

    def get_status(self) -> dict:
        summary = self.journal.get_summary()
        return {
            "running": self.running,
            "current_weights": self._current_weights,
            "active_signals": [k for k, v in self._current_weights.items() if v > 0],
            "journal": summary,
        }


# ──────────────────────────────────────────
# standalone 실행
# ──────────────────────────────────────────

async def async_main():
    import argparse

    parser = argparse.ArgumentParser(description="Strategy Evolver (autoresearch-style)")
    parser.add_argument("--config", default="config.yaml", help="설정 파일")
    parser.add_argument("--interval", type=float, default=12.0, help="진화 주기 (시간)")
    parser.add_argument("--days", type=int, default=7, help="단기 백테스트 기간")
    parser.add_argument("--population", type=int, default=20, help="후보 수")
    parser.add_argument("--once", action="store_true", help="1회만 실행")
    parser.add_argument("--journal", action="store_true", help="저널 요약 출력")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    evolver = StrategyEvolver(
        config_path=args.config,
        interval_hours=args.interval,
        lookback_days=args.days,
        population_size=args.population,
    )

    if args.journal:
        summary = evolver.journal.get_summary()
        print(f"\n{'═' * 60}")
        print(f"  Evolution Journal Summary")
        print(f"{'═' * 60}")
        print(f"  총 실험: {summary['total_experiments']}회")
        print(f"  채택: {summary['adopted']}회 ({summary['adoption_rate']}%)")
        print(f"\n  시그널별 성과:")
        for name, stats in summary.get("signal_stats", {}).items():
            print(f"    {name}: {stats['tried']}회 시도, {stats['adopted']}회 채택 ({stats['win_rate']}%)")

        best = evolver.journal.get_best_configs(5)
        if best:
            print(f"\n  Top 5 조합:")
            for i, b in enumerate(best):
                active = [k for k, v in b.get("weights", {}).items() if v > 0]
                print(f"    #{i+1}: signals={active} score={b.get('composite_score', 0):+,.0f}")
        print(f"{'═' * 60}\n")
        return

    if args.once:
        changed = await evolver.evolve_cycle()
        if changed:
            print("\n  새 시그널 조합 채택!")
            print(f"  활성: {evolver.get_status()['active_signals']}")
        else:
            print("\n  현재 조합 유지")
    else:
        print(f"\n  Strategy Evolver 데몬 시작")
        print(f"  주기: {args.interval}시간, 후보: {args.population}개")
        print(f"  Ctrl+C로 종료\n")

        evolver.running = True
        while evolver.running:
            try:
                await evolver.evolve_cycle()
                await asyncio.sleep(args.interval * 3600)
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"  에러: {e}")
                await asyncio.sleep(60)


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
