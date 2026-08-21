"""
wyckoff_engine.py — 威科夫操盘法翻译层 V2

不再从原始K线检测信号，而是直接翻译 V5 四引擎的输出为威科夫语言。

引擎输入 → 威科夫概念：
  PA底分型(valid)                  → Spring雏形
  OBV底背离                        → 吸筹确认
  AD积累                           → 大资金吸筹
  VPA缩量企稳                      → ST (二次测试)
  stolgo向上突破 + 放量            → SOS (强势信号)
  缩量回调+份额流入+未破前低       → LPS (最后支撑点)
  顶分型 + AD派发                  → 派发期(C)
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional


@dataclass
class WyckoffPhase:
    name: str = ""
    letter: str = "?"
    substage: str = ""
    confidence: float = 0.0
    description: str = ""


@dataclass
class SpringSignal:
    detected: bool = False
    strength: float = 0.0
    description: str = ""


@dataclass
class SOSSignal:
    detected: bool = False
    strength: float = 0.0
    description: str = ""


@dataclass
class LPSSignal:
    detected: bool = False
    strength: float = 0.0
    description: str = ""


@dataclass
class WyckoffResult:
    phase: WyckoffPhase = field(default_factory=WyckoffPhase)
    spring: SpringSignal = field(default_factory=SpringSignal)
    sos: SOSSignal = field(default_factory=SOSSignal)
    lps: LPSSignal = field(default_factory=LPSSignal)
    summary: str = ""


def analyze_wyckoff(
    candles: List,
    vol_result: Optional[Dict] = None,
    pa_kind: str = "none",
    stolgo_breakout: Optional[Dict] = None,
    share_3d: bool = False,
    share_5d: bool = False,
) -> WyckoffResult:
    """
    威科夫翻译引擎 —— 把V5引擎输出翻译成威科夫语言。
    """
    result = WyckoffResult()

    # ── 从 vol_result 解包量价信号 ──
    obv_div = ""
    obv_trend = "neutral"
    ad_trend = "neutral"
    cmf_val = 0.0
    if vol_result:
        obv_div = vol_result.get("obv_divergence", "none")
        obv_trend = vol_result.get("obv_trend", "neutral")
        ad = vol_result.get("ad", {})
        ad_trend = ad.get("trend", "neutral") if isinstance(ad, dict) else "neutral"
        cmf = vol_result.get("cmf", {})
        cmf_val = cmf.get("value", 0.0) if isinstance(cmf, dict) else 0.0

    # 量价评分
    obv_bullish_div = (obv_div == "bullish_div")
    obv_bearish_div = (obv_div == "bearish_div")
    ad_accumulating = (ad_trend == "bullish")
    ad_distributing = (ad_trend == "bearish")
    cmf_positive = cmf_val > 0.05

    # ── 1. SOS 检测 ──
    #   SOS = stolgo向上突破 + (放量或OBV↑或AD积累)
    sos_score = 0
    if stolgo_breakout:
        is_up_breakout = stolgo_breakout.get("is_breakout", False) and stolgo_breakout.get("direction", "") == "up"
        if is_up_breakout: sos_score += 4
    if obv_trend == "bullish": sos_score += 2
    if ad_accumulating: sos_score += 2
    if cmf_positive: sos_score += 1
    result.sos.detected = sos_score >= 5
    result.sos.strength = min(sos_score / 9, 1.0)
    result.sos.description = f"SOS(强度{result.sos.strength:.0%})" if result.sos.detected else "无SOS信号"

    # ── 2. Spring 检测 ──
    #   Spring = PA底分型(valid) + OBV底背离 + (缩量或AD积累)
    spring_score = 0
    has_bottom_pa = (pa_kind == "bottom")
    if has_bottom_pa: spring_score += 3
    if obv_bullish_div: spring_score += 3
    if ad_accumulating: spring_score += 2
    if cmf_positive: spring_score += 1
    result.spring.detected = spring_score >= 5  # PA底分型 + OBV底背离 至少有一个 + 其他辅证
    result.spring.strength = min(spring_score / 9, 1.0)
    if result.spring.detected:
        details = []
        if has_bottom_pa: details.append("底分型")
        if obv_bullish_div: details.append("OBV底背离")
        if ad_accumulating: details.append("AD积累")
        result.spring.description = f"Spring({'; '.join(details)})"
    else:
        result.spring.description = "无Spring信号"

    # ── 3. LPS 检测 ──
    #   LPS = Spring已出现 + 份额5日流入 + (OBV看多或CMF>0)
    #   v3.2: 份额因子已删除(用户"份额没什么卵用"), LPS降为 Spring + 量价确认
    lps_score = 0
    if result.spring.detected and result.spring.strength >= 0.3:
        lps_score += 4  # 有Spring前提(权重提升, 原3+份额3=6分制改为纯量价4分制)
    if obv_trend == "bullish": lps_score += 2
    if cmf_positive: lps_score += 1
    if ad_accumulating: lps_score += 1
    result.lps.detected = lps_score >= 5  # v3.2: 阈值6→5 (移除份额3分后重新校准)
    result.lps.strength = min(lps_score / 8, 1.0)
    if result.lps.detected:
        details = ["有Spring前提"]
        if obv_trend == "bullish": details.append("OBV↑")
        result.lps.description = f"LPS({'; '.join(details)})"
    else:
        result.lps.description = "无LPS信号"

    # ── 4. 阶段判定 ──
    #   优先级: SOS > LPS > Spring > 顶分型(派发) > 默认
    #   v3.2: 移除"份额3日/5日未通过→D期"兜底, 无确认信号一律判"阶段不明"
    if result.sos.detected and result.sos.strength >= 0.5:
        result.phase = WyckoffPhase("加仓期", "B", "SOS", result.sos.strength, "B-加仓期(SOS确认)")
    elif result.lps.detected and result.lps.strength >= 0.5:
        result.phase = WyckoffPhase("吸筹→加仓过渡", "A→B", "LPS", result.lps.strength, "A→B过渡(LPS确认)")
    elif result.spring.detected and result.spring.strength >= 0.5:
        result.phase = WyckoffPhase("吸筹期", "A", "Spring", result.spring.strength, "A-吸筹期(Spring)")
    elif pa_kind == "top":
        result.phase = WyckoffPhase("派发期", "C", "PSY", 0.6, "C-派发期(顶分型)")
    else:
        result.phase = WyckoffPhase("未知", "?", "", 0.0, "阶段不明")

    # ── 5. 摘要 ──
    parts = [f"威科夫阶段: {result.phase.description}"]
    if result.spring.detected:
        parts.append(f"Spring({result.spring.strength:.0%})")
    if result.sos.detected:
        parts.append(f"SOS({result.sos.strength:.0%})")
    if result.lps.detected:
        parts.append(f"LPS({result.lps.strength:.0%})")
    result.summary = " | ".join(parts)

    return result
