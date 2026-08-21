"""
mode_engine.py — Phase 0 行情模式识别
======================================
标的类型分类 + 大盘状态识别 + 模式判定。
所有参数通过 config 注入，零硬编码。

修复记录 (2026-07-10):
  - 周线下跌检测：用日 K 每 5 根聚合为周 K，取周五收盘价，不再每隔 5 天采样
  - 模式 D 条件 3：新增 positions_all_triggered 参数
"""

from __future__ import annotations
from typing import List, Tuple

from .config import EngineConfig, ModeConfig
from .models import Candle, ETFType, MarketMode, ModeResult, BlackSwanLevel
from .utils import sma


class ModeEngine:
    """行情模式识别器"""

    def __init__(self, config: EngineConfig | None = None):
        self.cfg: ModeConfig = (config or EngineConfig.default()).mode

    # ==================================================================
    # 日K → 周K 聚合（公共工具方法）
    # ==================================================================

    @staticmethod
    def daily_to_weekly(daily_candles: List[Candle], days_per_week: int = 5) -> List[Candle]:
        """
        将日 K 聚合为周 K。
        每组 days_per_week 根日 K 合成 1 根周 K：
          open  = 第一根日 K 的开盘价
          high  = 期间最高价
          low   = 期间最低价
          close = 最后一根日 K 的收盘价（周五收盘）
          volume = 期间成交量之和
        """
        if not daily_candles:
            return []
        weekly: List[Candle] = []
        for i in range(0, len(daily_candles), days_per_week):
            group = daily_candles[i : i + days_per_week]
            if not group:
                continue
            weekly.append(Candle(
                date=group[-1].date,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
            ))
        return weekly

    # ---------- 标的分类 ----------

    def classify_etf(self, code: str) -> ETFType:
        """
        标的类型识别。
        检查代码是否在 event_driven_codes 中 → EVENT_DRIVEN
        检查代码是否在 valuation_codes 中 → VALUATION
        都不在时，按代码前缀推断：
          588xxx（科创板）/ 1595xx（深市科技）/ 515xxx（通信）→ EVENT_DRIVEN
          其他 → VALUATION
        """
        code_clean = code.upper()
        # 去除 .SH/.SZ 后缀再比较
        code_base = code_clean.replace(".SH", "").replace(".SZ", "")
        for ec in self.cfg.event_driven_codes:
            ec_clean = ec.upper().replace(".SH", "").replace(".SZ", "")
            if ec_clean == code_base:
                return ETFType.EVENT_DRIVEN
        for vc in self.cfg.valuation_codes:
            vc_clean = vc.upper().replace(".SH", "").replace(".SZ", "")
            if vc_clean == code_base:
                return ETFType.VALUATION
        # 前缀推断
        if code_base.startswith("588") or code_base.startswith("515"):
            return ETFType.EVENT_DRIVEN
        return ETFType.VALUATION

    # ---------- 大盘状态 ----------

    def identify_market_state(
        self,
        hs300_candles: List[Candle],
        portfolio_drawdown: float = 0.0,
        cash_ratio: float = 0.0,
        positions_all_triggered: bool = False,
    ) -> Tuple[str, List[MarketMode]]:
        """
        大盘状态识别。
        返回: (state_label, [overlay_modes])
        state_label: "normal" | "bear" | "empty" | "black_swan"

        positions_all_triggered: 所有持仓标的均已触发减仓信号（模式 D 条件 3）。
          调用方需从 RiskEngine 或持仓系统获取此信息传入。
        """
        if not hs300_candles or len(hs300_candles) < 20:
            return "normal", []

        closes = [c.close for c in hs300_candles]
        overlays: List[MarketMode] = []

        # --- F: 黑天鹅 ---
        if len(hs300_candles) >= 2:
            prev_close = hs300_candles[-2].close
            daily_change = (hs300_candles[-1].close - prev_close) / prev_close * 100
            if daily_change < self.cfg.black_swan_daily_pct:
                return "black_swan", [MarketMode.F]

        # --- C: 熊市 ---
        bearish = False

        # (1) MA20 死叉 MA60 且持续
        ma20 = sma(closes, 20)
        ma60 = sma(closes, 60) if len(closes) >= 60 else None
        if ma60:
            check_len = min(self.cfg.death_cross_days, len(ma20), len(ma60))
            below_count = sum(
                1 for i in range(-check_len, 0)
                if ma20[i] > 0 and ma60[i] > 0 and ma20[i] < ma60[i]
            )
            if check_len > 0 and below_count / check_len >= self.cfg.death_cross_ratio:
                bearish = True

        # (2) 连续 N 周下跌（正确聚合日K为周K）
        weekly_candles = self.daily_to_weekly(hs300_candles)
        if len(weekly_candles) >= self.cfg.weekly_decline_weeks:
            weekly_closes = [c.close for c in weekly_candles]
            recent_weeks = weekly_closes[-self.cfg.weekly_decline_weeks:]
            if all(recent_weeks[i] < recent_weeks[i - 1]
                   for i in range(1, len(recent_weeks))):
                bearish = True

        # (3) 跌破年线持续
        ma250 = sma(closes, 250) if len(closes) >= 250 else None
        if ma250:
            check_len = min(self.cfg.below_year_line_days, len(ma250))
            below_ma250 = sum(
                1 for i in range(-check_len, 0)
                if ma250[i] > 0 and closes[i] < ma250[i]
            )
            if check_len > 0 and below_ma250 / check_len >= self.cfg.below_year_line_ratio:
                bearish = True

        if bearish:
            overlays.append(MarketMode.C)

        # --- D: 空仓（三个条件全部满足）---
        # 条件 1: 已处于模式 C → bearish=True
        # 条件 2: 连跌 8 周 或 从高点回撤 >20%
        # 条件 3: 所有持仓标的均已触发减仓信号 + 现金 >50%
        if bearish and len(weekly_candles) >= self.cfg.deep_bear_weeks:
            peak = max(closes)
            last_close = hs300_candles[-1].close
            drawdown = (peak - last_close) / peak * 100 if peak > 0 else 0

            weekly_closes_all = [c.close for c in weekly_candles]
            recent_8 = weekly_closes_all[-self.cfg.deep_bear_weeks:]
            weeks_declining = (
                len(recent_8) >= self.cfg.deep_bear_weeks
                and all(recent_8[i] < recent_8[i - 1]
                        for i in range(1, len(recent_8)))
            )
            drawdown_hit = drawdown > self.cfg.deep_bear_drawdown * 100

            if (weeks_declining or drawdown_hit) and (
                positions_all_triggered and cash_ratio > self.cfg.mode_d_cash_ratio
            ):
                overlays.append(MarketMode.D)

        state = "bear" if bearish else "normal"
        return state, overlays

    # ---------- 综合判定 ----------

    def determine_mode(
        self,
        etf_code: str,
        hs300_candles: List[Candle],
        portfolio_drawdown: float = 0.0,
        cash_ratio: float = 0.0,
        positions_all_triggered: bool = False,
    ) -> ModeResult:
        """
        综合模式判定，一次调用返回完整结果。

        positions_all_triggered: 所有持仓标的均已触发减仓信号。

        注意：模式 E（宏观叠加层）不在本引擎中实现。
        模式 E 的输入（政策事件等级 S/A/B/C/X、M2/社融/DR007、北向资金信号）
        是主观判断数据，需由 AI 层 / SKILL.md 根据新闻语义判断后叠加在 A/B 之上。
        """
        etf_type = self.classify_etf(etf_code)
        state, overlays = self.identify_market_state(
            hs300_candles, portfolio_drawdown, cash_ratio, positions_all_triggered,
        )

        reasons = [f"标的类型: {etf_type.value}"]
        black_swan_level = BlackSwanLevel.NONE.value

        if MarketMode.F in overlays:
            active = MarketMode.F
            # 计算黑天鹅分级
            if hs300_candles and len(hs300_candles) >= 2:
                prev_close = hs300_candles[-2].close
                daily_change = (hs300_candles[-1].close - prev_close) / prev_close * 100
                if daily_change < -7:
                    black_swan_level = BlackSwanLevel.L2.value
                elif daily_change < -5:
                    black_swan_level = BlackSwanLevel.L1.value
            # L3（熔断）由调用方根据 market_circuit_breaker 参数覆盖
            reasons.append(f"黑天鹅触发 {black_swan_level} → 模式 F")
        elif MarketMode.D in overlays:
            active = MarketMode.D
            reasons.append("空仓态 → 模式 D（3条件全部满足）")
            reasons.append("  - 已处于模式 C")
            reasons.append("  - 连跌8周或回撤>20%")
            reasons.append(f"  - 全部持仓触发减仓 + 现金{cash_ratio:.0%}>50%")
        elif MarketMode.C in overlays:
            active = MarketMode.C
            reasons.append("熊市态 → 模式 C")
            if positions_all_triggered:
                reasons.append(
                    f"  注: 持仓已全部触发减仓，现金{cash_ratio:.0%}，"
                    f"但连跌/回撤未达模式 D 阈值"
                )
        else:
            active = (
                MarketMode.A if etf_type == ETFType.VALUATION else MarketMode.B
            )
            reasons.append(f"正常态 → 模式 {active.value}")

        base = MarketMode.A if etf_type == ETFType.VALUATION else MarketMode.B
        return ModeResult(
            etf_code=etf_code,
            etf_type=etf_type,
            base_mode=base,
            overlay_modes=[m for m in overlays if m != active],
            active_mode=active,
            black_swan_level=black_swan_level,
            reasoning=reasons,
        )
