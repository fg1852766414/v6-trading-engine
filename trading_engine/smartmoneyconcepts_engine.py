"""
smartmoneyconcepts_engine.py — SMC 市场结构分析（精简版，只保留有用指标）

对 A股 ETF 有用的 SMC 指标:
  1. Previous High/Low (周线) — 知道价格在周线级别的什么位置
  2. Retracements (回撤百分比) — 快速判断回调深度是否在合理范围

其余 ICT 外汇概念 (FVG/OB/BOS/流动性) 已在 ETF 场景验证为冗余/无用，已删除。

与 V5 的关系:
  前高前低 → 验证 PA 底分型的支撑位是否与周线级别方向一致
  回撤%   → 加分项: 回调在38-62%内为健康，>78.6%趋势转弱
"""

import sys
from typing import List, Dict, Optional
from dataclasses import dataclass

import pandas as pd

try:
    from .models import Candle
except ImportError:
    from trading_engine.models import Candle  # type: ignore

try:
    from smartmoneyconcepts import smc
    HAS_SMC = True
except ImportError:
    HAS_SMC = False
    smc = None  # type: ignore


def candles_to_dataframe(candles: List[Candle], symbol: str = "ETF") -> pd.DataFrame:
    """Candle[] → SMC-compatible OHLCV DataFrame"""
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame({
        "open": [c.open for c in candles],
        "high": [c.high for c in candles],
        "low": [c.low for c in candles],
        "close": [c.close for c in candles],
        "volume": [c.volume for c in candles],
    }, index=pd.to_datetime([c.date for c in candles]))
    df.index.name = "timestamp"
    df.attrs["symbol"] = symbol
    return df


@dataclass
class SMCResult:
    """精简版 SMC 结果"""
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    broken_high: bool = False
    broken_low: bool = False
    retracement_pct: float = 0.0
    retracement_direction: str = "none"
    deepest_retracement_pct: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "previous_hl": {
                "high": self.prev_high,
                "low": self.prev_low,
                "broken_high": self.broken_high,
                "broken_low": self.broken_low,
            },
            "retracement": {
                "current_pct": self.retracement_pct,
                "deepest_pct": self.deepest_retracement_pct,
                "direction": self.retracement_direction,
            },
            "summary": self.summary,
        }


class SMCAnalyzer:
    """SMC 分析器（精简版: 仅保留前高前低 + 回撤）"""

    def __init__(self):
        if not HAS_SMC:
            raise ImportError("smartmoneyconcepts 未安装。pip install smartmoneyconcepts")
        self._df = None
        self._candles = []

    def load(self, candles: List[Candle], symbol: str = "ETF") -> "SMCAnalyzer":
        self._candles = candles
        self._df = candles_to_dataframe(candles, symbol)
        return self

    @property
    def df(self) -> pd.DataFrame:
        if self._df is None:
            raise ValueError("请先调用 load(candles)")
        return self._df

    def previous_high_low(self, time_frame: str = "1W") -> Dict:
        """获取上一时间框架的高/低点"""
        df = self.df
        if len(df) < 5:
            return {"high": None, "low": None, "broken_high": False, "broken_low": False}
        phl = smc.previous_high_low(df, time_frame=time_frame)
        last = phl.iloc[-1]
        return {
            "high": round(float(last["PreviousHigh"]), 3) if pd.notna(last["PreviousHigh"]) else None,
            "low": round(float(last["PreviousLow"]), 3) if pd.notna(last["PreviousLow"]) else None,
            "broken_high": bool(last["BrokenHigh"] == 1) if pd.notna(last["BrokenHigh"]) else False,
            "broken_low": bool(last["BrokenLow"] == 1) if pd.notna(last["BrokenLow"]) else False,
        }

    def retracements(self) -> Dict:
        """从最近摆动点计算回撤百分比"""
        df = self.df
        if len(df) < 20:
            return {"current_pct": 0, "deepest_pct": 0, "direction": "none"}
        sw = smc.swing_highs_lows(df, swing_length=10)
        ret = smc.retracements(df, sw)
        last = ret.iloc[-1]
        direction = "none"
        if pd.notna(last["Direction"]):
            direction = "bullish" if last["Direction"] == 1 else "bearish"
        return {
            "current_pct": round(float(last["CurrentRetracement%"]), 1) if pd.notna(last["CurrentRetracement%"]) else 0,
            "deepest_pct": round(float(last["DeepestRetracement%"]), 1) if pd.notna(last["DeepestRetracement%"]) else 0,
            "direction": direction,
        }

    def analyze(self, candles: Optional[List[Candle]] = None) -> SMCResult:
        """一站式分析"""
        if candles:
            self.load(candles)

        result = SMCResult()

        # 前H/L(周线)
        phl = self.previous_high_low("1W")
        result.prev_high = phl["high"]
        result.prev_low = phl["low"]
        result.broken_high = phl["broken_high"]
        result.broken_low = phl["broken_low"]

        # 回撤
        ret = self.retracements()
        result.retracement_pct = ret["current_pct"]
        result.deepest_retracement_pct = ret["deepest_pct"]
        result.retracement_direction = ret["direction"]

        # 摘要
        parts = []
        if result.broken_high:
            parts.append("破前高↑")
        if result.broken_low:
            parts.append("破前低↓")
        pct = result.retracement_pct
        if pct > 0:
            level = "健康" if pct < 62 else ("危险" if pct > 78 else "中性")
            parts.append(f"回撤{pct:.0f}%({level})")
        result.summary = "; ".join(parts) if parts else "无显著SMC信号"
        return result


def analyze_smc(candles: List[Candle]) -> Dict:
    """便捷入口"""
    return SMCAnalyzer().load(candles).analyze().to_dict()
