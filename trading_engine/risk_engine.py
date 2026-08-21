"""
risk_engine.py — 组合风控
===========================
回撤级别 / 方向集中度 / 现金比例 / 单标的最大亏损 / 月度再平衡。
所有阈值从 config 注入。
"""

from __future__ import annotations
from typing import Dict, List, Optional

from .config import EngineConfig, RiskConfig
from .models import MarketMode, RiskCheck, SignalLevel


class RiskEngine:
    """组合风控引擎"""

    def __init__(self, config: EngineConfig | None = None):
        self.cfg: RiskConfig = (config or EngineConfig.default()).risk

    # ==================================================================
    # 回撤检查
    # ==================================================================

    def check_drawdown(self, portfolio_drawdown_pct: float) -> RiskCheck:
        """
        组合回撤级别判断。
        按 config.drawdown_levels 逐级匹配。
        """
        alerts: List[str] = []
        worst_level = SignalLevel.NORMAL
        action = False
        abs_dd = abs(portfolio_drawdown_pct)

        for threshold, level, desc, cap in self.cfg.drawdown_levels:
            if abs_dd >= threshold:
                alerts.append(
                    f"回撤 {abs_dd:.1f}% ≥ {threshold}% → {desc} (权益上限 {cap}%)"
                )
                if level == "critical":
                    worst_level = SignalLevel.CRITICAL
                elif level == "warning" and worst_level != SignalLevel.CRITICAL:
                    worst_level = SignalLevel.WARNING
                action = True

        return RiskCheck(
            level=worst_level,
            alerts=alerts if alerts else ["回撤正常"],
            action_required=action,
            detail=f"当前回撤: {portfolio_drawdown_pct:.1f}%",
        )

    # ==================================================================
    # 集中度 / 现金 / 单标亏损
    # ==================================================================

    def check_single_direction(
        self, direction_ratio: float, direction_name: str = "",
        cap: Optional[float] = None,
    ) -> RiskCheck:
        """单方向集中度检查"""
        limit = cap if cap is not None else 0.30  # 可覆盖
        if direction_ratio > limit:
            return RiskCheck(
                level=SignalLevel.WARNING,
                alerts=[f"{direction_name}占比 {direction_ratio:.1%} > {limit:.0%}上限"],
                action_required=True,
            )
        return RiskCheck(level=SignalLevel.NORMAL)

    def check_cash_ratio(self, cash_ratio: float, floor: Optional[float] = None) -> RiskCheck:
        """现金比例检查"""
        limit = floor if floor is not None else 0.20
        if cash_ratio < limit:
            return RiskCheck(
                level=SignalLevel.WARNING,
                alerts=[f"现金 {cash_ratio:.1%} < {limit:.0%}下限"],
                action_required=True,
            )
        return RiskCheck(level=SignalLevel.NORMAL)

    def check_single_position_loss(
        self, entry_price: float, current_price: float,
    ) -> RiskCheck:
        """单标的最大亏损检查"""
        max_loss = self.cfg.single_max_loss
        if entry_price <= 0:
            return RiskCheck(level=SignalLevel.NORMAL, detail="入场价无效")
        pnl = (current_price - entry_price) / entry_price
        if pnl <= max_loss:
            return RiskCheck(
                level=SignalLevel.CRITICAL,
                alerts=[f"单标的亏损 {pnl:.1%} ≤ {max_loss:.0%}，强制减仓"],
                action_required=True,
            )
        return RiskCheck(level=SignalLevel.NORMAL)

    # ==================================================================
    # 再平衡
    # ==================================================================

    def rebalance_check(
        self,
        positions: Dict[str, dict],
        cash_ratio: float,
        active_mode: MarketMode,
    ) -> Dict[str, RiskCheck]:
        """
        月度再平衡检查。
        实际仓位 vs 建议仓位偏离超过阈值 → 触发调仓。
        模式 C/D 下不触发加仓再平衡。
        """
        results: Dict[str, RiskCheck] = {}
        if active_mode in (MarketMode.C, MarketMode.D):
            return results

        for code, pos in positions.items():
            actual = pos.get("actual_pct", 0)
            suggested = pos.get("suggested_pct", 0)
            if suggested > 0 and abs(actual - suggested) / suggested > self.cfg.rebalance_deviation:
                direction = "超配→减仓" if actual > suggested else "低配→加仓"
                results[code] = RiskCheck(
                    level=SignalLevel.WARNING,
                    alerts=[f"{code}: 实际{actual:.1f}% vs 建议{suggested:.1f}% → {direction}"],
                    action_required=True,
                )

        cash_check = self.check_cash_ratio(cash_ratio)
        if cash_check.action_required:
            results["_cash"] = cash_check

        return results

    # ==================================================================
    # 综合检查
    # ==================================================================

    def comprehensive_check(
        self,
        total_equity_ratio: float,
        cash_ratio: float,
        portfolio_drawdown: float = 0.0,
        direction_groups: Optional[Dict[str, float]] = None,
        total_cap: Optional[float] = None,
        direction_cap: Optional[float] = None,
    ) -> List[RiskCheck]:
        """
        一键综合风控。
        所有参数均为比值（0.0-1.0）：
          total_equity_ratio: 总权益占比，如 0.65 表示 65%
          cash_ratio: 现金占比，如 0.40 表示 40%
          portfolio_drawdown: 回撤幅度（小数，如 -0.12 表示 -12%）
          direction_groups: {方向名: 占比}, 占比为比值
        """
        checks: List[RiskCheck] = []

        checks.append(self.check_drawdown(portfolio_drawdown))

        eq_limit = total_cap if total_cap is not None else 0.80
        if total_equity_ratio > eq_limit:
            checks.append(RiskCheck(
                level=SignalLevel.WARNING,
                alerts=[f"总权益 {total_equity_ratio:.1%} > {eq_limit:.0%}上限"],
                action_required=True,
            ))

        checks.append(self.check_cash_ratio(cash_ratio))

        if direction_groups:
            for name, ratio in direction_groups.items():
                d = self.check_single_direction(ratio, name, cap=direction_cap)
                if d.action_required:
                    checks.append(d)

        return checks
