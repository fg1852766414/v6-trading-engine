"""
technical_engine.py — 技术指标 + PA 分型
=========================================
MA / MACD / RSI / 分型检测与质量验证。
所有周期、阈值均从 config 注入。
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple

from .config import EngineConfig, TechnicalConfig
from .models import Candle, FractalKind, FractalResult, MarketMode, TechnicalIndicators
from .utils import ema, rma, sma


class TechnicalEngine:
    """技术指标计算器"""

    def __init__(self, config: EngineConfig | None = None):
        self.cfg: TechnicalConfig = (config or EngineConfig.default()).technical

    # ==================================================================
    # 均线
    # ==================================================================

    def calc_all_ma(self, closes: List[float]) -> TechnicalIndicators:
        """一次性计算全部均线"""
        periods = self.cfg.ma_periods
        result = TechnicalIndicators()

        if len(periods) >= 1:
            result.ma5 = sma(closes, periods[0])
        if len(periods) >= 2:
            result.ma10 = sma(closes, periods[1])
        if len(periods) >= 3:
            result.ma20 = sma(closes, periods[2])
        if len(periods) >= 4 and len(closes) >= periods[3]:
            result.ma60 = sma(closes, periods[3])
        else:
            result.ma60 = None  # 数据不足，显式标记
        if len(periods) >= 5 and len(closes) >= periods[4]:
            result.ma120 = sma(closes, periods[4])
        else:
            result.ma120 = None

        return result

    # ==================================================================
    # MACD
    # ==================================================================

    def calc_macd(
        self, closes: List[float]
    ) -> Tuple[List[float], List[float], List[float]]:
        """
        MACD 标准公式
        DIF = EMA(fast) - EMA(slow)
        DEA = EMA(DIF, signal)
        MACD 柱 = (DIF - DEA) × 2
        """
        f = self.cfg.macd_fast
        s = self.cfg.macd_slow
        sig = self.cfg.macd_signal

        ema_fast = ema(closes, f)
        ema_slow = ema(closes, s)
        dif = [
            ef - es if ef > 0 and es > 0 else 0.0
            for ef, es in zip(ema_fast, ema_slow)
        ]
        dea = ema(dif, sig)
        hist = [
            (d - e) * 2 if d != 0 and e != 0 else 0.0
            for d, e in zip(dif, dea)
        ]
        return dif, dea, hist

    def check_macd_golden_cross(
        self, dif: List[float], dea: List[float]
    ) -> Tuple[bool, int]:
        """检查近 N 根 K 线内是否有金叉。返回 (has_cross, bars_ago)"""
        return self._check_cross(dif, dea, self.cfg.macd_golden_lookback, "golden")

    def check_macd_dead_cross(
        self, dif: List[float], dea: List[float]
    ) -> Tuple[bool, int]:
        """检查死叉"""
        return self._check_cross(dif, dea, self.cfg.macd_dead_lookback, "dead")

    def _check_cross(
        self, dif: List[float], dea: List[float], lookback: int, direction: str
    ) -> Tuple[bool, int]:
        for i in range(len(dif) - 1, max(len(dif) - lookback - 1, 0), -1):
            # 只跳过未初始化的值（精确为0），不跳过负值（DIF/DEA在零轴下是正常的）
            if dif[i] == 0.0 and dea[i] == 0.0:
                continue
            if i == 0:
                continue
            if dif[i - 1] == 0.0 and dea[i - 1] == 0.0:
                continue
            if direction == "golden":
                if dif[i] > dea[i] and dif[i - 1] <= dea[i - 1]:
                    return True, len(dif) - 1 - i
            else:
                if dif[i] < dea[i] and dif[i - 1] >= dea[i - 1]:
                    return True, len(dif) - 1 - i
        return False, -1

    # ==================================================================
    # RSI
    # ==================================================================

    def calc_rsi(self, closes: List[float]) -> float:
        """RSI(Wilder) = 100 - 100/(1 + RS)"""
        period = self.cfg.rsi_period
        if len(closes) < period + 1:
            return 50.0

        gains, losses = [], []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))

        avg_gain = rma(gains, period)
        avg_loss = rma(losses, period)

        if not avg_gain or avg_gain[-1] == 0:
            return 0.0 if (avg_loss and avg_loss[-1] > 0) else 50.0
        if not avg_loss or avg_loss[-1] == 0:
            return 100.0

        rs = avg_gain[-1] / avg_loss[-1]
        return 100.0 - (100.0 / (1.0 + rs))

    # ==================================================================
    # PA 分型检测
    # ==================================================================

    def _fractal_trend_context(
        self, candles: List[Candle], tops: List[int], bottoms: List[int]
    ) -> FractalResult:
        """
        趋势上下文裁决：当顶底分型太近时，用趋势方向决定哪个有效。
        上涨趋势中 → 底 > 顶（底优先）；下跌趋势中 → 顶 > 底。
        """
        closes = [c.close for c in candles]
        from .utils import ema
        ema20 = ema(closes, 20)[-1] if len(closes) >= 20 else 0
        last_close = closes[-1] if closes else 0
        uptrend = last_close > ema20  # 价在EMA20之上=上涨趋势

        # 顶底冲突判定
        nearest_top = tops[0] if tops else None
        nearest_bottom = bottoms[0] if bottoms else None
        conflict_gap = 5  # 5根K线内算冲突

        if nearest_top is not None and nearest_bottom is not None:
            if abs(nearest_top - nearest_bottom) <= conflict_gap:
                # 冲突：用趋势方向决定
                if uptrend:
                    # 上涨趋势中顶分型很可能是回调假象，用底分型
                    return FractalResult(
                        kind=FractalKind.BOTTOM, index=nearest_bottom,
                        detail=f"趋势裁决底分型 @ idx={nearest_bottom}（上涨趋势，附近顶分型已抑制）"
                    )
                else:
                    return FractalResult(
                        kind=FractalKind.TOP, index=nearest_top,
                        detail=f"趋势裁决顶分型 @ idx={nearest_top}（下跌趋势，附近底分型已抑制）"
                    )

        # 无冲突，返回离当前最近的
        if nearest_top is not None and nearest_bottom is not None:
            if nearest_top > nearest_bottom:
                return FractalResult(kind=FractalKind.TOP, index=nearest_top,
                                     detail=f"顶分型 @ idx={nearest_top}")
            else:
                return FractalResult(kind=FractalKind.BOTTOM, index=nearest_bottom,
                                     detail=f"底分型 @ idx={nearest_bottom}")
        if nearest_top is not None:
            return FractalResult(kind=FractalKind.TOP, index=nearest_top,
                                 detail=f"顶分型 @ idx={nearest_top}")
        if nearest_bottom is not None:
            return FractalResult(kind=FractalKind.BOTTOM, index=nearest_bottom,
                                 detail=f"底分型 @ idx={nearest_bottom}")
        return FractalResult(kind=FractalKind.NONE, detail="最近无分型")

    def detect_fractal(self, candles: List[Candle]) -> FractalResult:
        """
        检测最近的分型（顶/底/无）
        先扫描全部K线收集所有顶底分型，再用趋势上下文裁决冲突。
        """
        search_depth = self.cfg.fractal_search_depth
        if len(candles) < search_depth * 2 + 1:
            return FractalResult(kind=FractalKind.NONE, detail=f"K线不足{search_depth*2+1}根")

        tops: List[int] = []
        bottoms: List[int] = []

        # scan R→L (new→old); therefore tops[0]/bottoms[0] = nearest to current bar
        for idx in range(len(candles) - 2, search_depth, -1):
            left = candles[idx - 1]
            mid = candles[idx]
            right = candles[idx + 1]

            if mid.high > left.high and mid.high > right.high:
                tops.append(idx)
            if mid.low < left.low and mid.low < right.low:
                bottoms.append(idx)

        return self._fractal_trend_context(candles, tops, bottoms)

    # ==================================================================
    # 分型质量验证
    # ==================================================================

    def validate_bottom_fractal(
        self,
        candles: List[Candle],
        fractal: FractalResult,
        weekly_ma120: Optional[float] = None,
    ) -> FractalResult:
        """
        底分型质量验证
        真底 ≥ 2/3 条：
          ① 底部在关键支撑位（前低 / MA120 / 斐波那契 61.8%）
          ② 缩量（成交量 < N 日均量）
          ③ 右腿收盘超左腿高点 ≥ N%
        """
        if fractal.kind != FractalKind.BOTTOM:
            return FractalResult(kind=FractalKind.NONE, is_valid=False, detail="无底分型")
        if fractal.index < 1 or fractal.index >= len(candles) - 1:
            return FractalResult(kind=FractalKind.NONE, is_valid=False, detail="分型位置无效")

        left = candles[fractal.index - 1]
        mid = candles[fractal.index]
        right = candles[fractal.index + 1]
        checks: Dict[str, bool] = {}

        # ① 关键支撑位
        near_support = False
        lookback = min(self.cfg.support_lookback, fractal.index)
        recent_low = min(
            c.low for c in candles[max(0, fractal.index - lookback): fractal.index + 1]
        )
        if recent_low > 0 and abs(mid.low - recent_low) / recent_low <= self.cfg.bottom_near_support_pct:
            near_support = True
        if weekly_ma120 is not None and weekly_ma120 > 0:
            if abs(mid.low - weekly_ma120) / weekly_ma120 <= self.cfg.bottom_near_support_pct:
                near_support = True
        checks["near_support"] = near_support

        # ② 缩量
        avg_vol = self._avg_volume(candles, fractal.index, self.cfg.bottom_volume_ma_period)
        checks["volume_shrink"] = avg_vol > 0 and mid.volume < avg_vol

        # ③ 右腿力度
        checks["right_leg_strength"] = (
            right.close > left.high * (1 + self.cfg.bottom_right_leg_pct)
        )

        score = sum(1 for v in checks.values() if v)
        is_valid = score >= self.cfg.fractal_min_quality

        return FractalResult(
            kind=FractalKind.BOTTOM, index=fractal.index,
            quality_score=score, is_valid=is_valid, checks=checks,
            detail="; ".join(f"{k}={v}" for k, v in checks.items()),
        )

    def validate_top_fractal(
        self,
        candles: List[Candle],
        fractal: FractalResult,
        boll_upper: Optional[float] = None,
    ) -> FractalResult:
        """
        顶分型质量验证
        真顶 ≥ 2/3 条：
          ① 顶部在关键阻力位（前高 / 布林上轨）
          ② 放量（成交量 > N 日均量 × M）
          ③ 右腿收盘低左腿低点 ≥ N%
        """
        if fractal.kind != FractalKind.TOP:
            return FractalResult(kind=FractalKind.NONE, is_valid=False, detail="无顶分型")
        if fractal.index < 1 or fractal.index >= len(candles) - 1:
            return FractalResult(kind=FractalKind.NONE, is_valid=False, detail="分型位置无效")

        left = candles[fractal.index - 1]
        mid = candles[fractal.index]
        right = candles[fractal.index + 1]
        checks: Dict[str, bool] = {}

        # ① 阻力位
        near_resistance = False
        lookback = min(self.cfg.support_lookback, fractal.index)
        recent_high = max(
            c.high for c in candles[max(0, fractal.index - lookback): fractal.index + 1]
        )
        if recent_high > 0 and abs(mid.high - recent_high) / recent_high <= self.cfg.top_near_resistance_pct:
            near_resistance = True
        if boll_upper is not None and boll_upper > 0:
            if abs(mid.high - boll_upper) / boll_upper <= self.cfg.top_near_resistance_pct:
                near_resistance = True
        checks["near_resistance"] = near_resistance

        # ② 放量
        avg_vol = self._avg_volume(candles, fractal.index, self.cfg.bottom_volume_ma_period)
        checks["volume_surge"] = avg_vol > 0 and mid.volume > avg_vol * self.cfg.top_volume_surge_mult

        # ③ 右腿力度
        checks["right_leg_strength"] = (
            right.close < left.low * (1 + self.cfg.top_right_leg_pct)
            if left.low > 0 else False
        )

        score = sum(1 for v in checks.values() if v)
        is_valid = score >= self.cfg.fractal_min_quality

        return FractalResult(
            kind=FractalKind.TOP, index=fractal.index,
            quality_score=score, is_valid=is_valid, checks=checks,
            detail="; ".join(f"{k}={v}" for k, v in checks.items()),
        )

    # ==================================================================
    # 辅助方法
    # ==================================================================

    def _avg_volume(self, candles: List[Candle], end_idx: int, period: int) -> float:
        start = max(0, end_idx - period)
        if start >= end_idx:
            return 0.0
        return sum(c.volume for c in candles[start:end_idx]) / (end_idx - start)

    # ==================================================================
    # 一键分析
    # ==================================================================

    def analyze(
        self, candles: List[Candle], mode: MarketMode
    ) -> dict:
        """根据行情模式返回完整技术面数据"""
        closes = [c.close for c in candles]
        result: dict = {
            "mode": mode.value,
            "data_points": len(candles),
            "latest_close": closes[-1] if closes else None,
        }

        ind = self.calc_all_ma(closes)
        result["ma5"] = ind.ma5[-1] if ind.ma5 else None
        result["ma10"] = ind.ma10[-1] if ind.ma10 else None
        result["ma20"] = ind.ma20[-1] if ind.ma20 else None
        result["ma60"] = ind.ma60[-1] if ind.ma60 else None
        result["ma120"] = ind.ma120[-1] if ind.ma120 else None

        # MA 排列
        valid = [v for v in [result[k] for k in ["ma5", "ma10", "ma20"]] if v is not None]
        if len(valid) >= 2:
            result["ma_alignment"] = (
                "多头排列" if all(valid[i] > valid[i + 1] for i in range(len(valid) - 1))
                else "空头排列"
            )
        else:
            result["ma_alignment"] = "数据不足"

        # MACD
        dif, dea, hist = self.calc_macd(closes)
        result["macd_dif"] = dif[-1] if dif else None
        result["macd_dea"] = dea[-1] if dea else None
        result["macd_hist"] = hist[-1] if hist else None
        has_golden, golden_ago = self.check_macd_golden_cross(dif, dea)
        has_dead, dead_ago = self.check_macd_dead_cross(dif, dea)
        result["macd_golden_cross"] = has_golden
        result["macd_golden_bars_ago"] = golden_ago
        result["macd_dead_cross"] = has_dead
        result["macd_dead_bars_ago"] = dead_ago

        # RSI
        result["rsi14"] = self.calc_rsi(closes)

        # 分型
        fractal = self.detect_fractal(candles)
        result["fractal_kind"] = fractal.kind.value
        result["fractal_index"] = fractal.index
        if fractal.kind == FractalKind.BOTTOM:
            validated = self.validate_bottom_fractal(candles, fractal, result.get("ma120"))
            result["fractal_valid"] = validated.is_valid
            result["fractal_score"] = validated.quality_score
            result["fractal_detail"] = validated.detail
        elif fractal.kind == FractalKind.TOP:
            validated = self.validate_top_fractal(candles, fractal)
            result["fractal_valid"] = validated.is_valid
            result["fractal_score"] = validated.quality_score
            result["fractal_detail"] = validated.detail
        else:
            result["fractal_valid"] = False
            result["fractal_score"] = 0
            result["fractal_detail"] = "无分型"

        # 趋势
        from .utils import ema
        ema20_list = ema(closes, 20) if len(closes) >= 20 else []
        result["ema20"] = ema20_list[-1] if ema20_list else None
        if result["ma20"] and result["latest_close"]:
            above_ma20 = result["latest_close"] > result["ma20"]
            result["trend"] = "上升" if above_ma20 else "下跌"
            if mode == MarketMode.A:
                days = self.cfg.mode_a_stable_days
                ratio = self.cfg.mode_a_stable_ratio
            else:
                days = self.cfg.mode_b_stable_days
                ratio = self.cfg.mode_b_stable_ratio
            above_count = sum(
                1 for i in range(-days, 0)
                if ind.ma20[i] and closes[i] > ind.ma20[i]
            )
            result["above_ma20_stable"] = above_count >= days * ratio if days > 0 else False

        result["avg_volume_5"] = self._avg_volume(candles, len(candles), 5)
        return result
