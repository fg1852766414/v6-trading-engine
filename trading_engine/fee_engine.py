"""
fee_engine.py — 7 天赎回费检查
================================
现在卖 vs 等满 7 天。所有参数从 config 注入。

场外基金特殊约束（SKILL.md 规则）：
  - 联接基金代码映射: etf_selector.py → get_fund_code()
  - T+1 净值确认: 场外基金按 T 日收盘净值成交，T+1 日确认份额
  - 7 天赎回费: 持有 <7 天赎回费 1.5%，从确认日开始计算（非交易日顺延）
  - 本引擎的 holding_days 应为从份额确认日起算的实际持有天数
  - 建仓/加仓/打满无费率；减仓/清仓前必做费率检查
"""

from __future__ import annotations
from typing import List, Optional, Tuple

from .config import EngineConfig, FeeConfig
from .models import Candle, MarketMode
from .utils import sma


class FeeEngine:
    """费率检查引擎"""

    def __init__(self, config: EngineConfig | None = None):
        self.cfg: FeeConfig = (config or EngineConfig.default()).fee

    # ==================================================================
    # 费率决策
    # ==================================================================

    def should_sell_now(
        self,
        current_price: float,
        support_level: float,
        holding_days: int = 0,
        profit_pct: float = 0.0,
        mode: MarketMode = MarketMode.A,
        trigger_probability: Optional[float] = None,
    ) -> Tuple[bool, str]:
        """
        费率检查：现在卖还是等满 7 天卖？
        返回 (sell_now, reason)
        """
        if trigger_probability is None:
            trigger_probability = self.cfg.default_probability

        threshold = (
            self.cfg.mode_b_threshold if mode == MarketMode.B
            else self.cfg.penalty_rate
        )

        # 浮盈 > N% 直接卖
        if profit_pct > self.cfg.profit_skip:
            return True, f"浮盈 {profit_pct:.1%}>{self.cfg.profit_skip:.0%}，直接卖"

        # 已满免赎回费天数
        if holding_days >= self.cfg.holding_days_free:
            return True, f"已持 {holding_days} 天，无赎回费"

        # 数据不足
        if current_price <= 0 or support_level <= 0:
            return False, "数据不足（价格或支撑位无效）"

        # 预估 N 日下跌
        estimated_loss = (current_price - support_level) / current_price
        expected_loss = estimated_loss * trigger_probability

        if expected_loss > threshold:
            return True, (
                f"预估下跌 {expected_loss:.2%} > {threshold:.1%}，不等"
                f"{self.cfg.holding_days_free}天（"
                f"当前{current_price:.2f} vs 支撑{support_level:.2f}，"
                f"概率{trigger_probability:.0%}）"
            )
        else:
            days_left = self.cfg.holding_days_free - holding_days
            return False, (
                f"预估下跌 {expected_loss:.2%} ≤ {threshold:.1%}，"
                f"等{days_left}天卖省赎回费"
            )

    # ==================================================================
    # 支撑位查找
    # ==================================================================

    def find_support_level(
        self,
        candles: List[Candle],
        weekly_ma120: Optional[float] = None,
    ) -> float:
        """
        找关键支撑位。
        优先级：斐波那契 N% > 近 N 日前低 > 周线 MA120。
        """
        lb = self.cfg.support_lookback
        if not candles or len(candles) < lb:
            return 0.0

        recent = candles[-lb:]
        high = max(c.high for c in recent)
        low = min(c.low for c in recent)

        if high <= 0 or low <= 0:
            return 0.0

        fib_val = high - (high - low) * self.cfg.fib_level
        if fib_val > 0:
            return fib_val
        if weekly_ma120 and weekly_ma120 > 0:
            return weekly_ma120
        return low
