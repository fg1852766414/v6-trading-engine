"""
ticket_engine.py — 三票制入场判定
===================================
票① PA 底分型 + 票② VPA 量价 + 票③ 份额验证
所有阈值从 config 注入。
"""

from __future__ import annotations
from typing import List, Optional

from .config import EngineConfig, PositionConfig, TicketConfig
from .models import Candle, ThreeTicketDecision, TicketResult, TicketStatus
from .technical_engine import TechnicalEngine


class TicketEngine:
    """三票制判定引擎"""

    def __init__(self, config: EngineConfig | None = None):
        cfg = config or EngineConfig.default()
        self.cfg: TicketConfig = cfg.ticket
        self._pos_cfg: PositionConfig = cfg.position
        self._tech = TechnicalEngine(config)

    # ---------- 票① ----------

    def ticket_pa(self, candles: List[Candle]) -> TicketResult:
        """票① PA 底分型确认"""
        fractal = self._tech.detect_fractal(candles)
        if fractal.kind.value != "bottom":
            return TicketResult(TicketStatus.FAIL, f"无底分型: {fractal.kind.value}")

        validated = self._tech.validate_bottom_fractal(candles, fractal)
        min_q = self.cfg.pa_min_quality
        if validated.is_valid and validated.quality_score >= min_q:
            return TicketResult(
                TicketStatus.PASS,
                f"真底分型 (得分{validated.quality_score}/3): {validated.detail}",
            )
        elif validated.quality_score == 1:
            return TicketResult(
                TicketStatus.WEAK,
                f"弱势底分型 (得分1/3): {validated.detail}",
            )
        else:
            return TicketResult(
                TicketStatus.FAIL,
                f"假底/下跌中继: {validated.detail}",
            )

    # ---------- 票② ----------

    def ticket_vpa(self, candles: List[Candle]) -> TicketResult:
        """
        票② VPA 量价确认
        a) 缩量企稳: 连续 N 日成交量 < 均量 + 价格不创新低
        b) 放量突破: 当日成交量 > 均量 × M + 收盘涨超 P%
        """
        min_bars = self.cfg.vpa_shrink_days + self.cfg.vpa_volume_ma_period + 1
        if len(candles) < min_bars:
            return TicketResult(TicketStatus.FAIL, f"K线不足{min_bars}根")

        avg_vol = self._tech._avg_volume(candles, len(candles), self.cfg.vpa_volume_ma_period)
        if avg_vol <= 0:
            return TicketResult(TicketStatus.FAIL, "均量数据异常")

        last = candles[-1]
        prev = candles[-2] if len(candles) >= 2 else None

        # 条件 a
        vol_shrink_n = True
        if prev:
            for i in range(self.cfg.vpa_shrink_days):
                idx = -(i + 1)
                if abs(idx) > len(candles):
                    vol_shrink_n = False
                    break
                if candles[idx].volume >= avg_vol:
                    vol_shrink_n = False
                    break
        else:
            vol_shrink_n = False

        price_not_new_low = prev is not None and last.low >= prev.low
        condition_a = vol_shrink_n and price_not_new_low

        # 条件 b
        vol_surge = last.volume > avg_vol * self.cfg.vpa_surge_volume_mult
        price_surge = prev is not None and (last.close - prev.close) / prev.close > self.cfg.vpa_surge_price_pct
        condition_b = vol_surge and price_surge

        if condition_a:
            return TicketResult(
                TicketStatus.PASS,
                f"缩量企稳: 连续{self.cfg.vpa_shrink_days}日<均量({avg_vol:.0f}), 价不创新低"
            )
        elif condition_b:
            change_pct = (last.close - prev.close) / prev.close * 100
            return TicketResult(
                TicketStatus.PASS,
                f"放量突破: 量>{avg_vol * self.cfg.vpa_surge_volume_mult:.0f}, 涨{change_pct:.1f}%"
            )
        elif vol_shrink_n and not price_not_new_low:
            return TicketResult(TicketStatus.WEAK, "缩量但价格未企稳（创了新低）")
        elif vol_surge and not price_surge:
            return TicketResult(TicketStatus.WEAK, "放量但涨幅不达标（<2%）")
        else:
            return TicketResult(TicketStatus.FAIL, "量价背离：无缩量企稳也无放量突破")

    # ---------- 票③ ----------

    def ticket_share(
        self,
        share_changes: List[float],
        premium: Optional[float] = None,
    ) -> TicketResult:
        """
        票③ 份额验证（3日扳机 + 5日保险）
        3 日扳机：连续 N 日净申购 → 决定"做不做"
        5 日保险：连续 M 日净申购 → 决定"做多少"
        """
        trigger_days = self.cfg.share_trigger_days
        insurance_days = self.cfg.share_insurance_days
        needed = max(trigger_days, insurance_days)

        if not share_changes or len(share_changes) < trigger_days:
            return TicketResult(TicketStatus.FAIL, f"份额数据不足（需至少{trigger_days}日）")

        # 3 日扳机
        last_trigger = share_changes[-trigger_days:]
        trigger_all_pos = all(s > 0 for s in last_trigger)
        trigger_all_neg = all(s < 0 for s in last_trigger)

        # 5 日保险（数据够才检查）
        insurance_pass = False
        insurance_detail = "N/A"
        if len(share_changes) >= insurance_days:
            last_insurance = share_changes[-insurance_days:]
            insurance_pos_days = sum(1 for s in last_insurance if s > 0)
            insurance_pass = insurance_pos_days >= insurance_days * 0.8  # 80%天数净申购
            insurance_detail = f"{insurance_pos_days}/{insurance_days}日净申购"

        # 组装输出
        trigger_icon = "🟢" if trigger_all_pos else "🔴"
        insurance_icon = "🟢" if insurance_pass else "🔴"
        detail = (
            f"3日扳机{trigger_icon} {[f'{s:+.2f}%' for s in last_trigger]} | "
            f"5日保险{insurance_icon} {insurance_detail}"
        )
        if premium is not None and premium > self.cfg.share_premium_warning:
            detail += f" | 溢价{premium:.1f}%（成本风险）"

        if trigger_all_pos:
            return TicketResult(TicketStatus.PASS, detail)
        elif trigger_all_neg:
            return TicketResult(TicketStatus.FAIL, detail)
        else:
            positive_days = sum(1 for s in last_trigger if s > 0)
            return TicketResult(
                TicketStatus.WEAK if positive_days >= trigger_days - 1 else TicketStatus.FAIL,
                detail,
            )

    # ---------- 综合 ----------

    def evaluate(
        self,
        candles: List[Candle],
        share_changes: List[float],
        premium: Optional[float] = None,
        has_catalyst: bool = False,
    ) -> ThreeTicketDecision:
        """三票制综合判定

        参数：
            candles:        K线列表（最新在最后）
            share_changes:  逐日份额变化率（百分比，最新在最后）
                            例如 [+0.5, +1.2, -0.3] 表示
                            第1日+0.5%, 第2日+1.2%, 第3日-0.3%
                            注意：是变化率不是绝对份额数，调用方需自行计算
                            (today - yesterday) / yesterday * 100
            premium:        当日溢价率%（如 3.06 表示溢价3.06%）
            has_catalyst:   AI判断的近3日是否有催化剂
        """
        t1 = self.ticket_pa(candles)
        t2 = self.ticket_vpa(candles)
        t3 = self.ticket_share(share_changes, premium)

        passed = sum(1 for t in [t1, t2, t3] if t.status == TicketStatus.PASS)
        has_weak = any(t.status == TicketStatus.WEAK for t in [t1, t2, t3])

        decision = ThreeTicketDecision(
            ticket_pa=t1, ticket_vpa=t2, ticket_share=t3,
            passed_count=passed,
        )

        if passed == 3:
            decision.can_enter = True
            decision.decision = "可入场"
        elif passed == 2 and has_catalyst:
            # 2/3 边界底仓规则：未通过的票有催化剂驱动特征 → 试探仓
            decision.can_enter = False
            base = self._pos_cfg.base_position
            bp = round(base * self._pos_cfg.boundary_base_ratio, 1)  # 如 30% × 0.25 = 7.5%
            sl = self._pos_cfg.boundary_stop_loss_pct * 100  # 0.05 → 5%
            decision.boundary_position = bp
            decision.boundary_stop_pct = sl
            decision.decision = (
                f"观察（2/3+催化剂 → 可建{bp:.0f}%试探仓，止损前低-{sl:.0f}%）"
            )
        elif passed == 2 and has_weak:
            decision.can_enter = False
            decision.decision = "观察（2票通过但第三票边界）"
        elif passed == 2:
            decision.can_enter = False
            decision.decision = "观察（2票通过，第三票不通过）"
        else:
            decision.can_enter = False
            decision.decision = "不入场"

        return decision
