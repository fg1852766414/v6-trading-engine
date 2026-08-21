"""
exit_engine.py — 退出信号引擎
===============================
清仓/减仓信号检测 + 3 日扳机×5 日保险仓位调整 +
熊市上限 + 黑天鹅分级操作。

整合三个数据源：
  1. 顶分型验证（technical_engine.py）
  2. 份额变化趋势（ticket_engine.py 的 share 数据）
  3. PE 分位 / 大盘状态（position_engine.py + mode_engine.py）
"""

from __future__ import annotations
from typing import Dict, List, Optional

from .config import EngineConfig
from .fee_engine import FeeEngine
from .models import (
    BearStage, BlackSwanAction, BlackSwanLevel,
    Candle, ExitDecision, ExitSignal, ExitType, MarketMode,
)
from .technical_engine import TechnicalEngine
from .utils import sma


class ExitEngine:
    """退出信号检测 + 降仓计划"""

    def __init__(self, config: EngineConfig | None = None):
        cfg = config or EngineConfig.default()
        self.cfg = cfg
        self._tech = TechnicalEngine(config)
        self._fee = FeeEngine(config)

    # ==================================================================
    # 硬清仓：顶分型 + 放量下跌
    # ==================================================================

    def _check_top_fractal_clear(self, candles: List[Candle]) -> Optional[ExitSignal]:
        """
        硬清仓①: 日线顶分型确认 + 放量下跌 > 3%（真顶验证通过）。
        返回 None 表示不触发。
        """
        fractal = self._tech.detect_fractal(candles)
        if fractal.kind.value != "top":
            return None

        validated = self._tech.validate_top_fractal(candles, fractal)
        if not validated.is_valid:
            return None

        last = candles[-1]
        prev = candles[-2] if len(candles) >= 2 else None
        if not prev or prev.close <= 0:
            return None

        drop_pct = (last.close - prev.close) / prev.close * 100
        if drop_pct < -3:
            return ExitSignal(
                exit_type=ExitType.HARD_CLEAR,
                triggers=["顶分型+放量下跌"],
                target_ratio=0.0,
                batch_count=3,
                batch_interval_days=7,
                need_fee_check=False,
                detail=f"真顶（{validated.quality_score}/3）+ 放量跌{drop_pct:.1f}%: {validated.detail}",
            )
        return None

    # ==================================================================
    # 硬清仓：产业逻辑证伪
    # ==================================================================

    def _check_industry_logic(self, logic_invalidated: bool) -> Optional[ExitSignal]:
        """
        硬清仓②: 产业逻辑被证伪。
        由 AI 层判断后传入，引擎只做 boolean 检查。
        """
        if not logic_invalidated:
            return None
        return ExitSignal(
            exit_type=ExitType.HARD_CLEAR,
            triggers=["产业逻辑证伪"],
            target_ratio=0.0,
            batch_count=1,
            need_fee_check=False,
            detail="AI 判定产业逻辑被颠覆 → 立即清仓",
        )

    # ==================================================================
    # 硬清仓：份额连续 5 日赎回
    # ==================================================================

    def _check_share_redemption(self, share_changes: List[float]) -> Optional[ExitSignal]:
        """
        硬清仓③: 份额连续 5 日净赎回。
        模式 B 中也为软清仓条件。
        """
        n = self.cfg.ticket.share_insurance_days
        if not share_changes or len(share_changes) < n:
            return None
        last_n = share_changes[-n:]
        if all(s < 0 for s in last_n):
            return ExitSignal(
                exit_type=ExitType.HARD_CLEAR,
                triggers=["份额连续5日净赎回"],
                target_ratio=0.0,
                batch_count=3,
                batch_interval_days=7,
                need_fee_check=True,
                detail=f"近{n}日全部净赎回: {[f'{s:+.2f}%' for s in last_n]}",
            )
        return None

    # ==================================================================
    # 软清仓
    # ==================================================================

    def _check_pe_high(
        self, pe_percentile: Optional[float], mode: MarketMode,
    ) -> Optional[ExitSignal]:
        """软清仓: PE >= 80% (模式 A)"""
        if pe_percentile is None or mode != MarketMode.A:
            return None
        if pe_percentile >= 80:
            return ExitSignal(
                exit_type=ExitType.SOFT_CLEAR,
                triggers=["PE高估"],
                target_ratio=0.0,
                batch_count=1,
                need_fee_check=True,
                detail=f"PE {pe_percentile:.0f}% >= 80% → 清仓窗口",
            )
        return None

    def _check_market_breakdown(self, candles: List[Candle]) -> Optional[ExitSignal]:
        """
        软清仓: 大盘系统性风险。
        周线 MA20 / MA60 同时失守 + 布林下轨跌破。
        """
        if len(candles) < 60:
            return None
        closes = [c.close for c in candles]
        ma20_list = sma(closes, 20)
        ma60_list = sma(closes, 60)
        if not ma20_list or not ma60_list:
            return None
        last = closes[-1]
        ma20 = ma20_list[-1]
        ma60 = ma60_list[-1]
        recent_20 = closes[-20:]
        avg_20 = sum(recent_20) / len(recent_20)
        variance = sum((c - avg_20) ** 2 for c in recent_20) / len(recent_20)
        boll_lower = avg_20 - 2 * (variance ** 0.5)

        if last < ma20 and last < boll_lower:
            return ExitSignal(
                exit_type=ExitType.SOFT_CLEAR,
                triggers=["大盘系统性破位"],
                target_ratio=0.0,
                batch_count=1,
                need_fee_check=True,
                detail=f"价{last:.3f} < MA20={ma20:.3f} BB下={boll_lower:.3f}",
            )
        if ma60 > 0 and last < ma20 and last < ma60:
            return ExitSignal(
                exit_type=ExitType.SOFT_CLEAR,
                triggers=["大盘均线破位"],
                target_ratio=0.0,
                batch_count=1,
                need_fee_check=True,
                detail=f"价{last:.3f} < MA20={ma20:.3f} MA60={ma60:.3f}",
            )
        return None

    def _check_mode_b_share_soft(
        self, share_changes: List[float], mode: MarketMode,
    ) -> Optional[ExitSignal]:
        """软清仓: 模式 B 份额连续 5 日赎回"""
        if mode != MarketMode.B:
            return None
        return self._check_share_redemption(share_changes)

    # ==================================================================
    # 综合退出评估
    # ==================================================================

    def evaluate(
        self,
        code: str,
        candles: List[Candle],
        share_changes: List[float],
        mode: MarketMode,
        pe_percentile: Optional[float] = None,
        industry_logic_invalidated: bool = False,
        holding_days: int = 0,
        entry_price: Optional[float] = None,
    ) -> ExitDecision:
        """
        综合退出信号评估。
        """
        decision = ExitDecision(code=code)
        all_signals: List[ExitSignal] = []

        # 硬清仓
        for checker in [
            lambda: self._check_top_fractal_clear(candles),
            lambda: self._check_industry_logic(industry_logic_invalidated),
            lambda: self._check_share_redemption(share_changes),
        ]:
            s = checker()
            if s:
                all_signals.append(s)

        # 软清仓
        for checker in [
            lambda: self._check_pe_high(pe_percentile, mode),
            lambda: self._check_market_breakdown(candles),
            lambda: self._check_mode_b_share_soft(share_changes, mode),
        ]:
            s = checker()
            if s:
                all_signals.append(s)

        decision.signals = all_signals

        hard = [s for s in all_signals if s.exit_type == ExitType.HARD_CLEAR]
        soft = [s for s in all_signals if s.exit_type == ExitType.SOFT_CLEAR]

        decision.hard_triggered = len(hard) > 0
        decision.soft_triggered = len(soft) > 0
        decision.should_exit = decision.hard_triggered or decision.soft_triggered
        decision.hard_reasons = [s.detail for s in hard]
        decision.soft_reasons = [s.detail for s in soft]

        if decision.hard_triggered:
            decision.summary = "硬清仓: " + ", ".join(
                t for s in hard for t in s.triggers
            )
        elif decision.soft_triggered:
            decision.summary = "软清仓: " + ", ".join(
                t for s in soft for t in s.triggers
            )
        else:
            decision.summary = "无退出信号"

        # 费率检查
        if decision.should_exit and candles and entry_price:
            current_price = candles[-1].close
            support = self._fee.find_support_level(candles)
            profit_pct = (
                (current_price - entry_price) / entry_price if entry_price > 0 else 0
            )
            sell_now, reason = self._fee.should_sell_now(
                current_price=current_price,
                support_level=support,
                holding_days=holding_days,
                profit_pct=profit_pct,
                mode=mode,
            )
            decision.fee_reason = reason

        return decision

    # ==================================================================
    # 3 日扳机 × 5 日保险 仓位调整
    # ==================================================================

    @staticmethod
    def adjust_position_by_insurance(
        theoretical_position: float,
        ticket_3d_pass: bool,
        ticket_5d_pass: bool,
    ) -> float:
        """
        3 日扳机 ✅ + 5 日保险 ✅ → 正常仓位
        3 日扳机 ✅ + 5 日保险 ❌ → 仓位 × 50%
        3 日扳机 ❌ → 0
        """
        if not ticket_3d_pass:
            return 0.0
        if ticket_5d_pass:
            return theoretical_position
        return theoretical_position * 0.5

    # ==================================================================
    # 模式 C 熊市仓位上限
    # ==================================================================

    def bear_market_cap(self, hs300_drawdown_pct: float) -> BearStage:
        """
        返回熊市阶段 + 权益上限（比值）。
        从 config.bear.bear_drawdown_thresholds 读取，零硬编码。
        """
        thresholds = self.cfg.bear.bear_drawdown_thresholds
        # 按阈值从大到小排序匹配
        for thresh in sorted(thresholds.keys(), reverse=True):
            if hs300_drawdown_pct >= thresh:
                stage_str = thresholds[thresh]
                return BearStage(stage_str)
        return BearStage.INITIAL

    def bear_market_equity_cap(self, stage: BearStage) -> float:
        """熊市阶段 → 权益上限（从 config.bear.bear_stages 读取）"""
        for row in self.cfg.bear.bear_stages:
            if row["stage"] == stage.value:
                return row["equity_cap"] / 100.0
        return 0.40  # fallback

    @staticmethod
    def bear_market_stock_cap(
        pe_percentile: Optional[float],
        etf_type: str,
        above_ma20: bool,
        above_ma60: bool,
    ) -> float:
        """
        熊市单标的仓位上限（SKILL.md C.2）。
        """
        if etf_type == "valuation":
            if pe_percentile is None:
                return 0.20
            if pe_percentile > 60:
                return 0.0
            if pe_percentile > 40:
                return 0.20
            if pe_percentile < 20:
                return 0.15
            return 0.10
        else:
            if not above_ma60:
                return 0.0
            if not above_ma20:
                return 0.10
            return 0.20

    # ==================================================================
    # 模式 F 黑天鹅分级操作
    # ==================================================================

    @staticmethod
    def black_swan_action(
        hs300_daily_change_pct: float,
        portfolio_daily_change_pct: float = 0.0,
        market_circuit_breaker: bool = False,
    ) -> BlackSwanAction:
        """
        黑天鹅分级操作指令（SKILL.md F.1）。
        """
        if market_circuit_breaker:
            return BlackSwanAction(
                level=BlackSwanLevel.L3,
                target_ratio=0.0,
                action="全部清仓→100%货基",
                force_mode_d=True,
            )
        if portfolio_daily_change_pct < -7:
            return BlackSwanAction(
                level=BlackSwanLevel.L2,
                target_ratio=0.30,
                action="减至当前30%",
                force_mode_d=True,
            )
        if hs300_daily_change_pct < -5:
            return BlackSwanAction(
                level=BlackSwanLevel.L1,
                target_ratio=0.50,
                action="减至当前50%",
            )
        return BlackSwanAction(
            level=BlackSwanLevel.NONE,
            target_ratio=1.0,
            action="无操作",
        )
