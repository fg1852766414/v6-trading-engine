"""
volume_engine.py — pandas_ta 量价分析引擎适配层

使用 pandas_ta 库的成熟量价指标, 补充 V5 的 VPA 分析。

逻辑关系:
  V5 VPA          = 缩量/放量的二元判断 (门禁)
  pandas_ta 量价指标 = MFI/CMF/OBV 趋势确认 (加分项扩展)

提供的指标:
  CMF (Chaikin Money Flow)  — 量价流强度, >0.2强势流入, <-0.2强势流出
  MFI (Money Flow Index)    — 量价RSI, >80过热超买, <20缩量超卖
  OBV (On-Balance Volume)   — 量能累积趋势, 与价格同步/背离
  AD (Accumulation/Dist)    — 累积/派发线, 确认资金方向
  VWAP                      — 成交量加权均价, 机构成本参考线
"""

import sys
from typing import List, Dict, Optional, Any
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandas_ta as ta

try:
    from .models import Candle
except ImportError:
    from trading_engine.models import Candle  # type: ignore


@dataclass
class VolumeResult:
    """量价分析结果"""
    cmf: Optional[float] = None        # Chaikin Money Flow
    cmf_signal: str = "neutral"        # bullish / bearish / neutral
    mfi: Optional[float] = None        # Money Flow Index
    mfi_signal: str = "neutral"
    obv_trend: str = "neutral"         # bullish / bearish / neutral (OBV走势方向)
    obv_divergence: str = "none"       # bullish_div / bearish_div / none
    ad: Optional[float] = None
    ad_trend: str = "neutral"
    vwap: Optional[float] = None
    vwap_dist_pct: Optional[float] = None  # 价格距VWAP的偏离%
    ambush: Optional[dict] = None      # 埋伏信号: {detected, stage, reason} 见 _detect_ambush
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "cmf": {"value": self.cmf, "signal": self.cmf_signal},
            "mfi": {"value": self.mfi, "signal": self.mfi_signal},
            "obv_trend": self.obv_trend,
            "obv_divergence": self.obv_divergence,
            "ad": {"value": self.ad, "trend": self.ad_trend},
            "vwap": self.vwap,
            "vwap_dist_pct": self.vwap_dist_pct,
            "ambush": self.ambush,
            "summary": self.summary,
        }


def candles_to_dataframe(candles: List[Candle], symbol: str = "ETF") -> pd.DataFrame:
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
    return df


class VolumeAnalyzer:
    """pandas_ta 量价分析器"""

    def __init__(self):
        self._df: Optional[pd.DataFrame] = None
        self._candles: List[Candle] = []

    def load(self, candles: List[Candle], symbol: str = "ETF") -> "VolumeAnalyzer":
        self._candles = candles
        self._df = candles_to_dataframe(candles, symbol)
        return self

    @property
    def df(self) -> pd.DataFrame:
        if self._df is None:
            raise ValueError("请先调用 load(candles)")
        return self._df

    def analyze(self, candles: Optional[List[Candle]] = None) -> VolumeResult:
        if candles:
            self.load(candles)
        df = self.df

        result = VolumeResult()

        # 0. 埋伏信号 (Ambush): 缩量下跌→地量→止跌企稳 的低吸埋伏窗口
        #    捕捉V型反转前"卖压枯竭"的提前布局机会（如传媒7/24-7/30→7/31放量+6.92%）
        if len(df) >= 40:
            try:
                result.ambush = _detect_ambush(df)
            except Exception:
                result.ambush = None

        # 1. CMF (Chaikin Money Flow, 21周期)
        if len(df) >= 21:
            try:
                cmf = ta.cmf(high=df["high"], low=df["low"],
                             close=df["close"], volume=df["volume"], length=21)
                if cmf is not None and len(cmf) > 0 and pd.notna(cmf.iloc[-1]):
                    result.cmf = round(float(cmf.iloc[-1]), 4)
                    if result.cmf > 0.2:
                        result.cmf_signal = "bullish"
                    elif result.cmf < -0.2:
                        result.cmf_signal = "bearish"
                    else:
                        result.cmf_signal = "neutral"
            except Exception:
                pass

        # 2. MFI (Money Flow Index, 14周期)
        if len(df) >= 14:
            try:
                mfi = ta.mfi(high=df["high"], low=df["low"],
                             close=df["close"], volume=df["volume"], length=14)
                if mfi is not None and len(mfi) > 0 and pd.notna(mfi.iloc[-1]):
                    result.mfi = round(float(mfi.iloc[-1]), 1)
                    if result.mfi > 80:
                        result.mfi_signal = "overbought"
                    elif result.mfi < 20:
                        result.mfi_signal = "oversold"
                    else:
                        result.mfi_signal = "neutral"
            except Exception:
                pass

        # 3. OBV (On-Balance Volume) + 背离检测
        if len(df) >= 20:
            try:
                obv = ta.obv(close=df["close"], volume=df["volume"])
                if obv is not None and len(obv) > 0:
                    # OBV近期趋势: 最近5日OBV变化 ÷ 5日均量（标准化斜率）
                    # ⚠️ 不用 (last-first)/first 的原因是OBV是累积量，分母=累积基座，
                    #     K线数量不同(50根vs250根)时基座差几十倍→斜率被摊薄→阈值敏感度漂移
                    #     标准化后只依赖近5日数据，与总K线长度无关，任何数据源/长度结果一致
                    obv_last5 = obv.iloc[-5:].dropna()
                    if len(obv_last5) >= 3:
                        obv_chg5 = obv_last5.iloc[-1] - obv_last5.iloc[0]
                        vol5_avg = float(df["volume"].iloc[-5:].mean() or 0)
                        norm_slope = obv_chg5 / vol5_avg if vol5_avg > 0 else 0.0
                        if norm_slope > 0.05:
                            result.obv_trend = "bullish"
                        elif norm_slope < -0.05:
                            result.obv_trend = "bearish"
                        else:
                            result.obv_trend = "neutral"

                    # OBV与价格背离检测
                    close_last5 = df["close"].iloc[-5:]
                    obv_last5_chg = obv_last5.iloc[-1] - obv_last5.iloc[0]
                    price_last5_chg = close_last5.iloc[-1] - close_last5.iloc[0]

                    if obv_last5_chg > 0 and price_last5_chg < 0:
                        result.obv_divergence = "bullish_div"  # 价跌OBV涨 → 底背离
                    elif obv_last5_chg < 0 and price_last5_chg > 0:
                        result.obv_divergence = "bearish_div"  # 价涨OBV跌 → 顶背离
                    else:
                        result.obv_divergence = "none"
            except Exception:
                pass

        # 4. AD (Accumulation/Distribution)
        if len(df) >= 14:
            try:
                ad = ta.ad(high=df["high"], low=df["low"],
                           close=df["close"], volume=df["volume"])
                if ad is not None and len(ad) > 0:
                    result.ad = round(float(ad.iloc[-1]), 2)
                    # AD趋势：双重确认 = 当前值正负 + 近10日标准化斜率
                    # ⚠️ 不能用 (last-first)/|10天前| 做斜率：分母是AD累积基座，
                    #    AD深度为负时"亏空收窄"会被误判成bullish（如有色-19亿回升到-11亿）
                    #    正确逻辑: AD<0说明累计仍净流出,回升只是"流出放缓"→ 应为neutral而非bullish
                    ad_last10 = ad.iloc[-10:].dropna()
                    if len(ad_last10) >= 5:
                        ad_chg10 = ad_last10.iloc[-1] - ad_last10.iloc[0]
                        vol10_avg = float(df["volume"].iloc[-10:].mean() or 0)
                        norm_slope = ad_chg10 / vol10_avg if vol10_avg > 0 else 0.0
                        ad_now = float(ad_last10.iloc[-1])
                        if ad_now > 0 and norm_slope > 0.05:
                            result.ad_trend = "bullish"   # 累计净流入 + 加速流入
                        elif ad_now < 0 and norm_slope < -0.05:
                            result.ad_trend = "bearish"   # 累计净流出 + 加速流出
                        else:
                            result.ad_trend = "neutral"   # 含"AD负但回升"→流出放缓≠流入
            except Exception:
                pass

        # 5. VWAP
        if len(df) >= 5:
            try:
                vwap = ta.vwap(high=df["high"], low=df["low"],
                               close=df["close"], volume=df["volume"])
                if vwap is not None and len(vwap) > 0 and pd.notna(vwap.iloc[-1]):
                    result.vwap = round(float(vwap.iloc[-1]), 3)
                    last_close = float(df["close"].iloc[-1])
                    result.vwap_dist_pct = round((last_close / result.vwap - 1) * 100, 1)
            except Exception:
                pass

        # 摘要
        parts = []
        if result.cmf_signal == "bullish":
            parts.append(f"CMF+({result.cmf:.2f})资金流入")
        elif result.cmf_signal == "bearish":
            parts.append(f"CMF-({result.cmf:.2f})资金流出")

        if result.mfi_signal == "oversold":
            parts.append(f"MFI={result.mfi}超卖")
        elif result.mfi_signal == "overbought":
            parts.append(f"MFI={result.mfi}过热")

        if result.obv_divergence == "bullish_div":
            parts.append("OBV底背离(价跌OBV涨)")
        elif result.obv_divergence == "bearish_div":
            parts.append("OBV顶背离(价涨OBV跌)")

        if result.obv_trend == "bullish":
            parts.append("OBV趋势↑")
        elif result.obv_trend == "bearish":
            parts.append("OBV趋势↓")

        if result.ad_trend == "bullish":
            parts.append("AD累积↑")
        elif result.ad_trend == "bearish":
            parts.append("AD派发↓")

        if result.vwap_dist_pct is not None:
            parts.append(f"距VWAP={result.vwap_dist_pct:+.1f}%")

        if result.ambush and result.ambush.get("detected"):
            parts.append(f"🪤埋伏:{result.ambush['stage']}")

        result.summary = "; ".join(parts) if parts else "无明显量价信号"

        return result


def _detect_ambush(df: pd.DataFrame) -> Optional[dict]:
    """埋伏信号检测：跌够 + 卖压枯竭 + 止跌企稳

    逻辑（捕捉V型反转前的低吸埋伏窗口，阈值经 29ETF×4年1023根 敏感性回测校准 2026-08-01）：
      ① 低位: 价格距60日高点回撤 > 15%（4年回测: 浅回调强势股比深跌熊股更赚钱，-15%档20日中位+0.42%为正）
      ② 卖压枯竭: 近6日均量 / 前12日均量 < 0.85（量能萎缩15%+）
      ③ 地量: 今日量 <= 近20日最低量的1.3倍（阶段地量，-15%档下1.3区分度优于1.6）
      ④ 止跌: 连续3日未创新低 或 价格站上5日均线（下跌动能衰竭）

    阶段区分: 近12日仍在下跌(>5%) → "缩量下跌中"；深跌后地量横盘 → "地量止跌"

    Returns:
        {detected, stage, reason}
        stage: "缩量下跌中" / "地量止跌" / None
    """
    closes = df["close"].dropna()
    volumes = df["volume"].dropna()
    if len(closes) < 40 or len(volumes) < 40:
        return None

    last_c = float(closes.iloc[-1])
    highs60 = float(closes.tail(60).max())
    # ① 低位: 距60日高点回撤
    drawdown = (last_c / highs60 - 1) * 100 if highs60 > 0 else 0

    # ② 缩量下跌: 近12日跌幅 + 量能萎缩
    chg12 = (last_c / float(closes.iloc[-13]) - 1) * 100 if len(closes) >= 14 else 0
    vol6 = float(volumes.tail(6).mean())
    vol12 = float(volumes.tail(12).mean())
    shrink = vol6 / vol12 if vol12 > 0 else 1.0

    # ③ 地量: 今日量接近阶段低量
    vol_today = float(volumes.iloc[-1])
    vol_low20 = float(volumes.tail(20).min())
    at_floor = vol_today <= vol_low20 * 1.3 if vol_low20 > 0 else False

    # ④ 止跌: 连续3日未创新低
    lows5 = df["low"].tail(5).dropna() if "low" in df.columns else None
    no_new_low3 = False
    if lows5 is not None and len(lows5) >= 4:
        recent = float(df["low"].iloc[-1])
        no_new_low3 = recent >= float(df["low"].iloc[-4])  # 今日低点不低于3日前低点
    sma5 = float(closes.tail(5).mean())
    above_sma5 = last_c >= sma5

    # 判定
    # 核心三要素: ①跌够了(回撤深) ②卖压枯竭(缩量) ③止跌迹象(地量/不创新低)
    # 注: "近12日仍在下跌"从硬门槛改为阶段区分——
    #     深跌后地量横盘(近期不跌)同样满足埋伏条件, 属于"地量止跌"阶段(更接近底部)
    # 阈值来源: 2026-08-01 敏感性回测(29ETF×4年1023根, 5264检测点)
    #   -15%/0.85/1.3: 74次触发, 20日胜率51.4%, 20日均+3.58%, 中位+0.42%(双正=可复制盈利)
    #   对比: -22%深跌档20日中位为负(长期熊股埋伏=接飞刀); -15%浅回调档(强势股回调)最赚钱
    #   注: 曾试 -22%/0.90(1年样本87.5%胜率), 4年样本证伪为过拟合, 已回滚
    low_enough = drawdown < -15.0
    sold_out = shrink < 0.85          # 卖压枯竭：量能萎缩15%+
    stabilising = no_new_low3 or above_sma5
    still_falling = chg12 < -5.0      # 近12日仍在下跌 → "缩量下跌中"；否则 → "地量止跌"

    if not low_enough:
        return None  # 未跌够，不构成埋伏价值
    if not sold_out:
        return None  # 未缩量，卖压未枯竭（放量/量平不构成埋伏）

    if at_floor and stabilising:
        return {
            "detected": True,
            "stage": "地量止跌",
            "reason": f"距高点-{abs(drawdown):.0f}% · 12日{chg12:+.1f}% · 量能萎缩至{shrink:.0%} · 地量企稳",
        }
    if still_falling:
        return {
            "detected": True,
            "stage": "缩量下跌中",
            "reason": f"距高点-{abs(drawdown):.0f}% · 12日跌{chg12:.1f}% · 量能萎缩至{shrink:.0%} · 等止跌",
        }
    return None


def analyze_volume(candles: List[Candle]) -> Dict:
    return VolumeAnalyzer().load(candles).analyze().to_dict()
