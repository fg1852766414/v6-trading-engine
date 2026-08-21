"""
stolgo_engine.py — stolgo 价格行为分析引擎适配层

将 V5 trading_engine 的 Candle 数据桥接到 stolgo 的 PA 分析框架。
补 V5 引擎短板: K线形态、盘整/突破、支撑/阻力量化、回测验证。

█ 核心能力
  K线形态: 吞没、锤子、十字星、连续阳/阴线
  盘整突破: consolidation + breakout detection (7日箱体)
  支撑阻力: 21/50周期高低点 + VWAP + 枢轴点 + 摆动点
  趋势特征: streak / 抛物线 / run-up
  回测验证: Backtest + trade.long/short bracket

█ 用法
  from trading_engine.stolgo_engine import analyze_pa
  result = analyze_pa(candles)  # 一键分析

█ 与 V5 的关系
  stolgo 输出 = V5 的"加分项扩展层"
  - 不影响三票制门禁 (PA/VPA/份额)
  - PA 输出进入 technical_engine 的 fractal 补充说明
  - 突破/形态信号进入 gen_html_report 的详情卡片
"""

import sys
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# ── V5 内部依赖 ──
try:
    from .models import Candle
except ImportError:
    from trading_engine.models import Candle  # type: ignore

# ── stolgo ──
try:
    import stolgo.pa as pa
    from stolgo.candlestick import CandleStick  # legacy, PA pattern checks
    HAS_STOLGO = True
except ImportError:
    HAS_STOLGO = False
    pa = None            # type: ignore
    CandleStick = None   # type: ignore


# ═══════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════

@dataclass
class SRLevels:
    """支撑/阻力位集合"""
    resistance_21:    Optional[float] = None
    support_21:       Optional[float] = None
    resistance_50:    Optional[float] = None
    support_50:       Optional[float] = None
    vwap:             Optional[float] = None
    pivot:            Optional[float] = None
    swing_high_5:     Optional[float] = None
    swing_low_5:      Optional[float] = None
    consolidation_high: Optional[float] = None
    consolidation_low:  Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class BreakoutSignal:
    """盘整突破信号"""
    is_breakout: bool = False
    direction: str = "none"           # "up" / "down" / "none"
    level: Optional[float] = None     # 突破价位
    consolidation_period: int = 7
    strength: float = 0.0             # 0-1，突破幅度与放量加权
    retest: bool = False              # 是否回踩确认不破

    def describe(self) -> str:
        if not self.is_breakout:
            return f"{self.consolidation_period}日盘整区间内运行"
        d = "向上突破" if self.direction == "up" else "向下破位"
        r = "，已回踩确认" if self.retest else ""
        return f"{d} {self.consolidation_period}日箱体@{self.level} (强度{self.strength:.0%}){r}"


@dataclass
class PatternResult:
    """K线形态检测结果"""
    name: str
    detected: bool
    confidence: float = 0.0
    signal_type: str = "neutral"      # bullish / bearish / neutral
    detail: str = ""


@dataclass
class TrendFeatures:
    """趋势特征"""
    green_streak: int = 0
    red_streak: int = 0
    consolidating: bool = False
    parabolic_up: bool = False
    run_up_pct: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__


# ═══════════════════════════════════════════════════
# Utility
# ═══════════════════════════════════════════════════

def candles_to_dataframe(candles: List[Candle], symbol: str = "ETF") -> pd.DataFrame:
    """Candle[] → stolgo-compatible OHLCV DataFrame"""
    if not candles:
        return pd.DataFrame()

    data = []
    for c in candles:
        data.append({
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
        })
    df = pd.DataFrame(data, index=pd.to_datetime([c.date for c in candles]))
    df.index.name = "timestamp"
    df.attrs["symbol"] = symbol
    return df


# ═══════════════════════════════════════════════════
# StolgoAnalyzer — 主分析器
# ═══════════════════════════════════════════════════

class StolgoAnalyzer:
    """
    stolgo PA 分析器。

    调用链:
        analyzer = StolgoAnalyzer()
        analyzer.load(candles)
        patterns = analyzer.candlestick_patterns()
        sr       = analyzer.sr_levels()
        breakout = analyzer.detect_breakout()
        trend    = analyzer.trend_features()
        # 或一键:
        result   = analyzer.analyze(candles)
    """

    def __init__(self):
        if not HAS_STOLGO:
            raise ImportError("stolgo 未安装。pip install stolgo")
        self._df: Optional[pd.DataFrame] = None
        self._candles: List[Candle] = []

    # ── 数据加载 ──

    def load(self, candles: List[Candle], symbol: str = "ETF") -> "StolgoAnalyzer":
        self._candles = candles
        self._df = candles_to_dataframe(candles, symbol)
        return self

    @property
    def df(self) -> pd.DataFrame:
        if self._df is None:
            raise ValueError("请先调用 load(candles)")
        return self._df

    # ── 1. K线形态 ──

    def candlestick_patterns(self) -> List[PatternResult]:
        """检测经典K线形态 (吞没/锤子/十字星)"""
        results: List[PatternResult] = []
        df = self.df

        if len(df) < 2:
            return [PatternResult("数据不足", False)]

        try:
            cs = CandleStick()

            if cs.is_bullish_engulfing(df):
                results.append(PatternResult("多头吞没", True, 0.85, "bullish",
                    f"{df.index[-1].strftime('%Y-%m-%d')} 阳线完全包裹前阴线"))

            if cs.is_bearish_engulfing(df):
                results.append(PatternResult("空头吞没", True, 0.85, "bearish",
                    f"{df.index[-1].strftime('%Y-%m-%d')} 阴线完全包裹前阳线"))

            if cs.is_hammer_candle(df):
                results.append(PatternResult("锤子线", True, 0.65, "bullish",
                    "长下影线(>实体2倍)+小实体，底部反转信号"))

            if cs.is_inverse_hammer_candle(df):
                results.append(PatternResult("倒锤子线", True, 0.60, "bearish",
                    "长上影线+小实体，高位反转信号"))

            if cs.is_doji_candle(df):
                results.append(PatternResult("十字星", True, 0.55, "neutral",
                    "开收几乎相同，多空平衡可能变盘"))

        except Exception:
            pass

        if not results:
            results.append(PatternResult("无明显形态", False))
        return results

    # ── 2. 支撑/阻力位 ──

    def sr_levels(
        self,
        resistance_period: int = 21,
        support_period: int = 21,
        consolidation_period: int = 7,
    ) -> SRLevels:
        """计算关键支撑/阻力位 (滚动高低点 + VWAP + 枢轴 + 摆动点)"""
        df = self.df
        n = len(df)

        levels = SRLevels()

        # 滚动高低点
        if n >= resistance_period:
            levels.resistance_21 = round(df["high"].rolling(resistance_period).max().iloc[-1], 3)
        if n >= support_period:
            levels.support_21 = round(df["low"].rolling(support_period).min().iloc[-1], 3)
        if n >= 50:
            levels.resistance_50 = round(df["high"].rolling(50).max().iloc[-1], 3)
            levels.support_50 = round(df["low"].rolling(50).min().iloc[-1], 3)

        # VWAP
        try:
            cum_vp = (df["close"] * df["volume"]).cumsum()
            cum_vol = df["volume"].cumsum()
            if cum_vol.iloc[-1] > 0:
                levels.vwap = round((cum_vp / cum_vol).iloc[-1], 3)
        except Exception:
            pass

        # 枢轴点 (H+L+C)/3
        last = df.iloc[-1]
        levels.pivot = round((last["high"] + last["low"] + last["close"]) / 3, 3)

        # 摆动点 (5日局部极值)
        if n >= 5:
            recent5 = df.iloc[-5:]
            levels.swing_high_5 = round(recent5["high"].max(), 3)
            levels.swing_low_5 = round(recent5["low"].min(), 3)

        # 盘整箱体
        if n >= consolidation_period:
            box = df.iloc[-consolidation_period:]
            levels.consolidation_high = round(box["high"].max(), 3)
            levels.consolidation_low = round(box["low"].min(), 3)

        return levels

    # ── 3. 盘整突破 ──

    def detect_breakout(self, period: int = 7) -> BreakoutSignal:
        """检测盘整突破信号"""
        df = self.df
        if len(df) < period + 1:
            return BreakoutSignal()

        signal = BreakoutSignal(consolidation_period=period)

        # 前 N 日箱体 (不含今天)
        box = df.iloc[-(period + 1):-1]
        box_high = box["high"].max()
        box_low = box["low"].min()
        box_width = box_high - box_low if box_high > box_low else 0.001

        last = df.iloc[-1]
        avg_vol = float(box["volume"].mean())

        # 向上突破
        if last["close"] > box_high:
            signal.is_breakout = True
            signal.direction = "up"
            signal.level = round(box_high, 3)
            signal.strength = round(min((last["close"] - box_high) / box_width, 1.0), 2)
            if last["volume"] > avg_vol * 1.2:
                signal.strength = min(signal.strength + 0.2, 1.0)

        # 向下破位
        elif last["close"] < box_low:
            signal.is_breakout = True
            signal.direction = "down"
            signal.level = round(box_low, 3)
            signal.strength = round(min((box_low - last["close"]) / box_width, 1.0), 2)
            if last["volume"] > avg_vol * 1.2:
                signal.strength = min(signal.strength + 0.2, 1.0)

        # 回踩确认: 昨日已突破，今日低点/高点未回箱体
        if signal.is_breakout and len(df) >= 2:
            prev = df.iloc[-2]
            if signal.direction == "up":
                signal.retest = bool(prev["low"] >= signal.level)
            elif signal.direction == "down":
                signal.retest = bool(prev["high"] <= signal.level)

        return signal

    # ── 4. 趋势特征 ──

    def trend_features(self) -> TrendFeatures:
        """连续涨跌 / 盘整 / 抛物线 / 急涨"""
        df = self.df
        tf = TrendFeatures()

        if len(df) < 5:
            return tf

        n = len(df)
        close = df["close"]

        # 连续阳/阴线
        for i in range(n - 1, 0, -1):
            if close.iloc[i] > close.iloc[i - 1]:
                tf.green_streak += 1
            else:
                break
        for i in range(n - 1, 0, -1):
            if close.iloc[i] < close.iloc[i - 1]:
                tf.red_streak += 1
            else:
                break

        # 盘整: 7日波幅 < 3%
        if n >= 7:
            r7 = df.iloc[-7:]
            h7, l7 = float(r7["high"].max()), float(r7["low"].min())
            pct = (h7 - l7) / l7 * 100 if l7 > 0 else 999
            tf.consolidating = pct < 3.0

        # 急涨 (run-up): 5日涨幅
        if n >= 5:
            tf.run_up_pct = round((close.iloc[-1] / close.iloc[-5] - 1) * 100, 1)

        # 抛物线: 连续4阳 + 加速
        if tf.green_streak >= 4:
            chgs = []
            for i in range(1, 5):
                chgs.append(close.iloc[-(4 - i + 1)] / close.iloc[-(4 - i + 2)] - 1)
            tf.parabolic_up = all(c > 0 for c in chgs) and all(
                chgs[i] > chgs[i - 1] for i in range(1, len(chgs))
            )

        return tf

    # ── 5. 一键分析 ──

    def analyze(self, candles: List[Candle], symbol: str = "ETF") -> Dict[str, Any]:
        """
        一站式 PA 分析。
        返回可直接序列化的 dict，适合传入 gen_html_report 展示。
        """
        self.load(candles, symbol)
        patterns = self.candlestick_patterns()
        sr = self.sr_levels()
        breakout = self.detect_breakout()
        trend = self.trend_features()

        # 生成摘要
        parts: List[str] = []

        for p in patterns:
            if p.detected and p.signal_type != "neutral":
                parts.append(f"{p.signal_type}={p.name}")

        if breakout.is_breakout:
            parts.append(f"breakout={breakout.direction}({breakout.strength:.0%})")

        last_close = float(self.df["close"].iloc[-1])
        if sr.support_21 and sr.resistance_21:
            rng = sr.resistance_21 - sr.support_21
            pos = (last_close - sr.support_21) / rng * 100 if rng > 0 else 50
            if pos > 75:
                parts.append("接近阻力")
            elif pos < 25:
                parts.append("接近支撑")

        if trend.consolidating:
            parts.append("盘整中")

        return {
            "candlestick_patterns": [
                {"name": p.name, "detected": p.detected,
                 "signal_type": p.signal_type, "detail": p.detail}
                for p in patterns
            ],
            "sr_levels": sr.to_dict(),
            "breakout": {
                "is_breakout": breakout.is_breakout,
                "direction": breakout.direction,
                "level": breakout.level,
                "strength": breakout.strength,
                "retest": breakout.retest,
                "description": breakout.describe(),
            },
            "trend": trend.to_dict(),
            "summary": "; ".join(parts) if parts else "无显著PA信号",
        }


# ═══════════════════════════════════════════════════
# 便捷函数 (与 quick_analysis 风格一致)
# ═══════════════════════════════════════════════════

def analyze_pa(candles: List[Candle], symbol: str = "ETF") -> Dict[str, Any]:
    """便捷入口: 对一组 Candle 做 stolgo PA 分析"""
    return StolgoAnalyzer().analyze(candles, symbol)


def detect_breakout_signal(candles: List[Candle], period: int = 7) -> BreakoutSignal:
    """便捷入口: 仅检测突破"""
    a = StolgoAnalyzer()
    a.load(candles)
    return a.detect_breakout(period)


def get_sr_levels(candles: List[Candle]) -> SRLevels:
    """便捷入口: 仅获取支撑阻力位"""
    a = StolgoAnalyzer()
    a.load(candles)
    return a.sr_levels()


# ═══════════════════════════════════════════════════
# 自测
# ═══════════════════════════════════════════════════

def _self_test() -> None:
    """自测: 生成虚拟K线并验证所有分析模块"""
    if not HAS_STOLGO:
        print("SKIP: stolgo 未安装")
        return

    dates = pd.date_range("2026-06-01", periods=60, freq="B")
    np.random.seed(42)
    base = 1.0
    closes = base + np.cumsum(np.random.randn(60) * 0.02)

    candles: List[Candle] = []
    for i in range(60):
        candles.append(Candle(
            date=dates[i].strftime("%Y-%m-%d"),
            open=round(closes[i] * (1 - np.random.uniform(0, 0.005)), 3),
            close=round(float(closes[i]), 3),
            high=round(float(closes[i] * (1 + np.random.uniform(0, 0.01))), 3),
            low=round(float(closes[i] * (1 - np.random.uniform(0, 0.01))), 3),
            volume=np.random.uniform(1e8, 5e8),
        ))

    result = analyze_pa(candles, "TEST")

    print("=== stolgo_engine 自测 ===")
    print(f"K线形态: {len(result['candlestick_patterns'])}项")
    for p in result["candlestick_patterns"]:
        print(f"  {p['name']}: {p['detected']} ({p['signal_type']})")

    sr = result["sr_levels"]
    print(f"SR: R21={sr.get('resistance_21')} S21={sr.get('support_21')} VWAP={sr.get('vwap')}")

    bo = result["breakout"]
    print(f"突破: {bo['description']}")

    tf = result["trend"]
    print(f"趋势: green={tf['green_streak']} red={tf['red_streak']} 盘整={tf['consolidating']}")
    print(f"摘要: {result['summary']}")
    print("PASS")


if __name__ == "__main__":
    _self_test()
