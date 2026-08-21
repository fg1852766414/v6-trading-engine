"""
dow_engine.py — 道氏理论趋势分析引擎 (pandas_ta版)

与威科夫引擎配合使用：
  道氏 = 趋势方向（往哪走）
  威科夫 = 时机决策（什么时候动）

使用 pandas_ta 指标：
  ADX → 趋势强度（>25有趋势，>50强趋势）
  PSAR → 趋势方向（点在价格下方=多头，上方=空头）
  SMA20/60 → 均线排列确认趋势
  CMF/OBV → 量价趋势验证

主趋势用周K（日K合成），次级/小趋势用日K。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Dict
import pandas as pd
import pandas_ta as ta

try:
    from .models import Candle
except ImportError:
    from trading_engine.models import Candle


@dataclass
class DowTrend:
    direction: str = "range"       # "bull" / "bear" / "range"
    strength: float = 0.0          # 0-1
    adx: float = 0.0               # 原始ADX值
    ma_aligned: bool = False       # 均线是否同向排列
    description: str = ""


@dataclass
class DowResult:
    primary: DowTrend = field(default_factory=DowTrend)
    secondary: DowTrend = field(default_factory=DowTrend)
    volume_confirms: bool = True
    swing_high_count: int = 0
    swing_low_count: int = 0
    summary: str = ""


def candles_to_df(candles: List[Candle]) -> pd.DataFrame:
    """Candle[] → OHLCV DataFrame"""
    return pd.DataFrame({
        "open": [c.open for c in candles],
        "high": [c.high for c in candles],
        "low": [c.low for c in candles],
        "close": [c.close for c in candles],
        "volume": [c.volume for c in candles],
    }, index=pd.to_datetime([c.date for c in candles]))


def daily_to_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """日K → 周K（按周聚合：开=周一开，收=周五收，高=周最高，低=周最低，量=周累计）"""
    weekly = df.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna()
    return weekly


def _compute_trend(df: pd.DataFrame, label: str) -> DowTrend:
    """
    用 pandas_ta 指标分析趋势。
    
    综合判断（纯价格行为，无PSAR）：
      ① ADX → >20有趋势，用+DI/-DI判定方向（权重2分）
      ② SMA排列 → SMA20>SMA60=多头排列（权重2分）
      ③ 收盘价相对SMA20位置（权重1分）
    """
    if len(df) < 10:
        return DowTrend("range", 0.0, description=f"{label}:数据不足")
    
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    
    # ── ADX（趋势强度）+ +DI/-DI（方向）──
    adx_period = min(14, len(df) // 2)
    adx_series = ta.adx(high=highs, low=lows, close=closes, length=adx_period)
    adx_val = 0.0
    pdi_val = 25.0
    ndi_val = 25.0
    adx_rising = False  # ADX是否正在上升（斜率确认）
    if adx_series is not None and len(adx_series) > 0:
        # ⚠️ 精确匹配 ADX_xx，避免 "ADX" in c 误匹配 ADXR_xx（PSAR列名就栽过同类坑）
        adx_col = [c for c in adx_series.columns if c.startswith("ADX_")]
        if adx_col:
            raw = adx_series[adx_col[0]].iloc[-1]
            adx_val = round(raw, 1) if pd.notna(raw) else 0.0
            # ADX斜率: 最新3根中至少有2根上升 → 趋势在加速
            if len(adx_series) >= 4:
                last3_adx = adx_series[adx_col[0]].iloc[-4:].values
                valid = [v for v in last3_adx if pd.notna(v)]
                if len(valid) >= 3:
                    rises = sum(1 for i in range(1, len(valid)) if valid[i] > valid[i-1])
                    adx_rising = rises >= 2  # 3段中有2段上升
        pdi_col = [c for c in adx_series.columns if "DMP" in c]
        ndi_col = [c for c in adx_series.columns if "DMN" in c]
        if pdi_col:
            pv = adx_series[pdi_col[0]].iloc[-1]
            pdi_val = round(pv, 1) if pd.notna(pv) else 25.0
        if ndi_col:
            nv = adx_series[ndi_col[0]].iloc[-1]
            ndi_val = round(nv, 1) if pd.notna(nv) else 25.0
    
    # ── SMA5/20/60 三级排列（备份版SMA5+三级思路 + 新版价格同侧保护）──
    sma5 = ta.sma(closes, length=min(5, len(closes)))
    sma20 = ta.sma(closes, length=min(20, len(closes)))
    sma60 = ta.sma(closes, length=min(60, len(closes)))
    s5 = sma5.iloc[-1] if sma5 is not None and len(sma5) > 0 else None
    s20 = sma20.iloc[-1] if sma20 is not None and len(sma20) > 0 else None
    s60 = sma60.iloc[-1] if sma60 is not None and len(sma60) > 0 else None
    ma_aligned = False
    if s5 and s20 and s60 and all(pd.notna(x) and x > 0 for x in [s5, s20, s60]):
        ma_aligned = True
    
    # ── 综合判定（6分制：ADX 2分 + SMA排列 2分 + 价格位置 2分）──
    bull_score = 0
    bear_score = 0
    
    # ADX贡献：趋势强度+方向，需同时满足 ADX>25 + ADX正在上升 + DI方向
    # 行业标准: +DI>-DI交叉+ADX>25+ADX上升=确认趋势；ADX下降即使DI交叉也是假信号
    if adx_val > 25 and adx_rising:
        if pdi_val > ndi_val: bull_score += 2
        elif ndi_val > pdi_val: bear_score += 2
    elif adx_val > 25 and not adx_rising:
        # ADX>25但平躺/下降 → 趋势动能衰减，DI信号减半
        if pdi_val > ndi_val: bull_score += 1
        elif ndi_val > pdi_val: bear_score += 1
    elif adx_val > 20:
        # ADX 20-25=弱趋势，需要1.15倍差距确认
        if pdi_val > ndi_val * 1.15: bull_score += 1
        elif ndi_val > pdi_val * 1.15: bear_score += 1
    
    # SMA排列：三级排列(S5/S20/S60) + 价格同侧确认
    # 2分: S5>S20>S60且价格在S5上方 → 完整多头
    # 2分: S5<S20<S60且价格在S5下方 → 完整空头
    # 1分: S5>S20且价格在S5上方(中长线不完整) → 短期偏多
    # 1分: S5<S20且价格在S5下方(中长线不完整) → 短期偏空
    if s5 and s20 and s60 and all(pd.notna(x) for x in [s5, s20, s60]):
        last_c = closes.iloc[-1]
        if s5 > s20 * 1.005 and s20 > s60 * 1.005 and last_c > s5:
            bull_score += 2  # 完整多头排列
        elif s5 < s20 * 0.995 and s20 < s60 * 0.995 and last_c < s5:
            bear_score += 2  # 完整空头排列
        elif s5 > s20 * 1.005 and last_c > s5:
            bull_score += 1  # 短期反弹
        elif s5 < s20 * 0.995 and last_c < s5:
            bear_score += 1  # 短期回调
    
    # 收盘价相对SMA20位置（中周期，稳定）
    if s20 and pd.notna(s20) and s20 > 0:
        last_c = closes.iloc[-1]
        if last_c > s20 * 1.02:
            bull_score += 1
        elif last_c < s20 * 0.98:
            bear_score += 1
    
    total = bull_score + bear_score
    
    # 低趋势时提高门槛
    min_confidence = 2 if adx_val >= 20 else 3
    
    if bull_score >= min_confidence and bull_score > bear_score:
        direction = "bull"
        strength = bull_score / max(total, 6)  # V6.2六分制分母
    elif bear_score >= min_confidence and bear_score > bull_score:
        direction = "bear"
        strength = bear_score / max(total, 6)  # V6.2六分制分母
    else:
        direction = "range"
        strength = 0.0
    
    # 保险：价格深度偏离SMA20时修正方向
    # 价格远低于SMA20却判bull → 降为range
    # 价格远高于SMA20却判bear → 降为range
    if s20 and pd.notna(s20) and s20 > 0:
        last_c = closes.iloc[-1]
        if direction == "bull" and last_c < s20 * 0.93:
            direction = "range"
            strength = 0.0
        elif direction == "bear" and last_c > s20 * 1.07:
            direction = "range"
            strength = 0.0
    
    # 近期动量修正：对冲SMA滞后（价格反弹但均线还空头）
    if len(closes) >= 4:
        # 近2根K线（周K=近2周，日K=近2天）涨跌幅
        recent_chg = (closes.iloc[-1] / closes.iloc[-3] - 1) * 100
        margin = abs(bull_score - bear_score)
        if recent_chg > 3 and direction == "bear" and margin <= 2:
            direction = "range"
            strength = 0.0
        elif recent_chg < -3 and direction == "bull" and margin <= 2:
            direction = "range"
            strength = 0.0
    
    return DowTrend(
        direction=direction,
        strength=min(round(strength, 2), 1.0),
        adx=adx_val,
        ma_aligned=ma_aligned,
        description=f"{label}:{direction}(ADX={adx_val},强度{min(strength,1.0):.0%})"
    )


def _check_volume(df: pd.DataFrame) -> bool:
    """
    成交量验证：用 pandas_ta 的CMF和OBV辅助判断。
    上涨日量 > 下跌日量 = 趋势健康。
    """
    if len(df) < 10:
        return True
    
    # 简单统计：近21日涨跌日平均量对比
    up_vol, down_vol = [], []
    for i in range(1, min(21, len(df))):
        vol = df["volume"].iloc[-i]
        if df["close"].iloc[-i] > df["close"].iloc[-i-1]:
            up_vol.append(vol)
        else:
            down_vol.append(vol)
    
    avg_up = sum(up_vol) / len(up_vol) if up_vol else 0
    avg_down = sum(down_vol) / len(down_vol) if down_vol else 0
    
    if avg_down > avg_up * 1.2 and avg_up > 0:
        return False
    return True


def _find_swing_highs(df: pd.DataFrame, window: int = 5) -> int:
    """近window根K线内是否创新高（道氏确认趋势用）"""
    if len(df) < window * 2 + 1:
        return 0
    recent = df["high"].iloc[-(window*2+1):-window]
    current_window = df["high"].iloc[-window:]
    if len(current_window) == 0 or len(recent) == 0:
        return 0
    recent_high = recent.max()
    return sum(1 for h in current_window if h > recent_high)


def _find_swing_lows(df: pd.DataFrame, window: int = 5) -> int:
    """近window根K线内是否创新低"""
    if len(df) < window * 2 + 1:
        return 0
    recent = df["low"].iloc[-(window*2+1):-window]
    current_window = df["low"].iloc[-window:]
    if len(current_window) == 0 or len(recent) == 0:
        return 0
    recent_low = recent.min()
    return sum(1 for l in current_window if l < recent_low)


def analyze_dow(candles: List[Candle], weekly_df: pd.DataFrame = None) -> DowResult:
    """
    道氏趋势分析入口。
    
    主趋势 → 周K（优先使用 AKShare weekly_df，否则日K合成）
    次级/小趋势 → 日K
    所有指标用 pandas_ta 计算。
    """
    result = DowResult()
    if len(candles) < 20:
        result.summary = "数据不足（至少需要20根K线）"
        return result
    
    # 日K DataFrame
    df_day = candles_to_df(candles)
    
    # ── 主趋势：优先用AKShare周K，否则日K合成 ──
    if weekly_df is not None and len(weekly_df) >= 8:
        df_week = weekly_df
        result.primary = _compute_trend(df_week, f"主趋势(周{len(df_week)}根)")
    else:
        df_week = daily_to_weekly(df_day)
        result.primary = _compute_trend(df_week, "主趋势(周合成)")
    
    # ── 次级趋势：用日K（近30-40根 = 1.5-2个月，对应典型次级修正周期） ──
    sec_len = min(40, len(df_day))
    df_sec = df_day.tail(sec_len) if len(df_day) >= sec_len else df_day
    result.secondary = _compute_trend(df_sec, f"次级({sec_len}日)")
    
    # ── 成交量验证 ──
    result.volume_confirms = _check_volume(df_day)
    
    # ── 摆动点数 ──
    result.swing_high_count = _find_swing_highs(df_day, window=5)
    result.swing_low_count = _find_swing_lows(df_day, window=5)
    
    # ── 摘要 ──
    pi = "📈" if result.primary.direction == "bull" else ("📉" if result.primary.direction == "bear" else "➡️")
    si = "📈" if result.secondary.direction == "bull" else ("📉" if result.secondary.direction == "bear" else "➡️")
    # 次级vs主趋势方向关系
    if result.primary.direction == result.secondary.direction and result.primary.direction != "range":
        align = "同向→趋势加强"
    elif result.primary.direction != "range" and result.secondary.direction != "range":
        align = "反向→回调/反弹"
    else:
        align = ""
    parts = [
        f"主趋势:{pi}{result.primary.direction}(ADX={result.primary.adx})",
        f"次级:{si}{result.secondary.direction}(ADX={result.secondary.adx})",
    ]
    if align:
        parts.append(align)
    if not result.volume_confirms:
        parts.append("⚠️量价背离")
    if result.swing_high_count > result.swing_low_count:
        parts.append(f"↑创新高{result.swing_high_count}次")
    else:
        parts.append(f"↓创新低{result.swing_low_count}次")
    result.summary = " | ".join(parts)
    
    return result


def dow_quick(candles: List[Candle], weekly_df: pd.DataFrame = None) -> Dict:
    """一键道氏分析 → dict（兼容现有gen_html_report.py）"""
    r = analyze_dow(candles, weekly_df=weekly_df)
    return {
        "primary": {
            "direction": r.primary.direction,
            "strength": round(r.primary.strength, 2),
            "adx": r.primary.adx,
            "label": r.primary.description,
        },
        "secondary": {
            "direction": r.secondary.direction,
            "strength": round(r.secondary.strength, 2),
            "adx": r.secondary.adx,
            "label": r.secondary.description,
        },
        "volume_confirms": r.volume_confirms,
        "higher_highs": r.swing_high_count,
        "lower_lows": r.swing_low_count,
        "summary": r.summary,
    }
