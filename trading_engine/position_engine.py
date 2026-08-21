"""
position_engine.py — 仓位计算
===============================
PE 分位查表 + 辅助加分制 + 左侧底仓 + 风控约束。
所有阈值、权重、比例均从 config 注入。
"""

from __future__ import annotations
from typing import Dict, Optional

from .config import EngineConfig, PositionConfig
from .models import PositionAdvice, ThreeTicketDecision, TicketStatus


class PositionEngine:
    """仓位计算器"""

    def __init__(self, config: EngineConfig | None = None):
        self.cfg: PositionConfig = (config or EngineConfig.default()).position

    # ==================================================================
    # PE 分位 → 仓位
    # ==================================================================

    def pe_to_position(self, pe_percentile: float) -> PositionAdvice:
        """
        PE 分位查表 → 建议仓位（阶梯函数，取区间中值）
        SKILL.md 原文是区间范围（如 PE<20% → 80-100%），
        引擎取中值作为确定性建议，避免线性插值产生的伪精度。
        """
        for row in self.cfg.pe_position_table:
            pe_min = row.get("pe_min", 0)
            pe_max = row.get("pe_max", 100)
            if pe_percentile < pe_max:
                return PositionAdvice(
                    suggested_pct=float(row["position"]),
                    pe_percentile=pe_percentile,
                    action=row["action"],
                )
        # fallback: 最后一个区间
        last = self.cfg.pe_position_table[-1]
        return PositionAdvice(
            suggested_pct=float(last["position"]),
            pe_percentile=pe_percentile,
            action=last["action"],
        )

    # ==================================================================
    # 辅助加分
    # ==================================================================

    def calc_bonus(
        self,
        macd_golden: bool,
        above_ma5: bool,
        pe_or_ps_low: bool,
        has_catalyst: bool,
    ) -> Dict[str, float]:
        """
        计算辅助加分项
        返回 {加分项名称: 加分数值%}
        """
        bonuses: Dict[str, float] = {}
        weights = self.cfg.bonus_weights

        if macd_golden and "MACD金叉" in weights:
            bonuses["MACD金叉"] = weights["MACD金叉"]
        if above_ma5 and "站上MA5" in weights:
            bonuses["站上MA5"] = weights["站上MA5"]
        if pe_or_ps_low and "PE/PS低估" in weights:
            bonuses["PE/PS低估"] = weights["PE/PS低估"]
        if has_catalyst and "催化剂" in weights:
            bonuses["催化剂"] = weights["催化剂"]

        return bonuses

    # ==================================================================
    # 综合仓位计算
    # ==================================================================

    def calculate(
        self,
        decision: ThreeTicketDecision,
        bonuses: Optional[Dict[str, float]] = None,
        current_total_equity: float = 0.0,
    ) -> ThreeTicketDecision:
        """
        计算最终仓位。
        基础 N% + 加分（上限 M%），受单方向和总权益约束。
        修改传入的 decision 对象并返回。
        """
        if not decision.can_enter:
            decision.base_position = 0.0
            decision.final_position = 0.0
            return decision

        base = self.cfg.base_position
        bonus_total = 0.0

        if bonuses:
            decision.bonus_items = bonuses
            bonus_total = sum(bonuses.values())

        theoretical = min(base + bonus_total, self.cfg.theoretical_max)

        # 弱点位折扣
        has_weak = (
            decision.ticket_pa.status == TicketStatus.WEAK
            or decision.ticket_vpa.status == TicketStatus.WEAK
            or decision.ticket_share.status == TicketStatus.WEAK
        )
        if has_weak:
            theoretical *= self.cfg.weakness_discount
            decision.discount_applied = True

        # 风控约束
        remaining = self.cfg.total_equity_cap - current_total_equity
        final = min(theoretical, self.cfg.single_direction_cap, max(remaining, 0))

        decision.base_position = base
        decision.final_position = round(max(final, 0), 1)
        return decision

    # ==================================================================
    # 左侧底仓
    # ==================================================================

    def left_side_base_position(
        self,
        pe_percentile: float,
        macd_golden: bool,
        above_ma20_stable: bool,
        current_batch: int = 0,
    ) -> Optional[PositionAdvice]:
        """
        左侧底仓规则。
        PE < 阈值 且（金叉未出现 或 仍在下跌趋势）→ 分批建仓。

        current_batch: 当前处于第几批（0-indexed）：
          0 → 第 1 批：建议仓位下限 × 40%
          1 → 第 2 批：建议仓位下限 × 70%（1 周后 PE 仍在阈值下触发）
          2 → 第 3 批：建议仓位下限 × 100%（金叉出现或站稳 MA20 后触发）

        返回 None 表示不适用此规则（PE 不够低/已金叉+上升/批次已用完）。
        """
        threshold = self.cfg.left_side_pe_threshold
        batches = self.cfg.left_side_batches

        if pe_percentile >= threshold:
            return None
        if current_batch >= len(batches):
            return None  # 三批全部结束
        if current_batch == 2 and macd_golden and above_ma20_stable:
            pass  # 第 3 批需要金叉+站稳确认，由调用方控制
        elif current_batch < 2 and macd_golden and above_ma20_stable:
            return None  # 已金叉+上升 → 走标准查表，不再用左侧规则

        standard = self.pe_to_position(pe_percentile)
        multiplier = batches[current_batch]
        batch_num = current_batch + 1

        return PositionAdvice(
            suggested_pct=round(standard.suggested_pct * multiplier, 1),
            pe_percentile=pe_percentile,
            action=f"左侧底仓第{batch_num}批 (×{multiplier:.0%}, {round(standard.suggested_pct * multiplier, 1)}%)",
        )
