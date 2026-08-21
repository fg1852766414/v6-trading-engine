"""
livermore_engine.py — 杰西·利弗莫尔持仓手册
============================================
独立模块，零依赖其他 V5 引擎。
输入: K 线数据 (list[Candle])
输出: dict, 包含关键点/趋势/最小阻力线/金字塔建议/六问检查表

设计原则:
- 与 V5 完全解耦，不互相改写决策
- 只读 K 线数据，不依赖任何网络/数据库
- 输出结构化 dict, 便于 gen_html_report.py 并列展示
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime


# ============================================================================
# 数据结构
# ============================================================================

@dataclass
class PivotalPoint:
    """关键点 (利弗莫尔核心概念)"""
    date: str
    price: float
    pp_type: str          # 'high' | 'low'
    strength: int = 1     # 1-3, 基于形成过程的K线数量
    days_ago: int = 0     # 距今天数


@dataclass
class LivermoreState:
    """利弗莫尔综合分析状态"""
    # 关键点
    pivotal_points: List[PivotalPoint] = field(default_factory=list)
    last_pivot_high: Optional[PivotalPoint] = None
    last_pivot_low: Optional[PivotalPoint] = None

    # 突破状态
    breakout_state: str = "none"        # none / breaking_up / breaking_down / confirmed_up / confirmed_down / failed
    breakout_days_ago: int = 999        # 突破距今天数
    natural_retrace_ok: bool = False    # 自然回撤是否守住

    # 最小阻力线
    resistance_line_slope: float = 0.0  # >0 上升, <0 下降, ≈0 震荡
    resistance_line_support: float = 0.0  # 当前最小阻力线价位
    resistance_line_state: str = "flat"  # up / down / flat

    # 趋势判断
    trend: str = "sideways"             # uptrend / downtrend / sideways

    # 当前价相对位置
    position_pct_above_last_pivot: float = 0.0  # 当前价相对最近PP的%位置

    # 六问
    six_questions: Dict[str, bool] = field(default_factory=dict)
    six_passed_count: int = 0

    # 金字塔建议
    pyramid_action: str = "hold"        # add_tier1 / add_tier2 / add_tier3 / hold / trim / exit
    pyramid_suggested_pct: float = 0.0  # 建议仓位百分比
    pyramid_stop_price: float = 0.0     # 建议止损价

    # 综合摘要
    summary: str = ""


# ============================================================================
# 核心函数 1: 关键点检测 (Pivotal Point)
# ============================================================================

def find_pivotal_points(candles: list, lookback: int = 60, min_separation: int = 5) -> List[PivotalPoint]:
    """
    找出最近 lookback 根 K 线中的关键转折点。

    算法:
    - 局部高点: 高点 > 前后各 min_separation 根K线的高点
    - 局部低点: 低点 < 前后各 min_separation 根K线的低点
    - strength: 基于被打破的次数 (1=普通, 2=二次测试, 3=三次测试)

    利弗莫尔原文: "关键点是经过多次测试的价格, 测试次数越多越重要"
    """
    if len(candles) < min_separation * 2 + 1:
        return []

    pp_list: List[PivotalPoint] = []
    n = len(candles)

    for i in range(min_separation, n - min_separation):
        center_high = candles[i].high
        center_low = candles[i].low

        # 检查是否为局部高点
        is_high = True
        for j in range(1, min_separation + 1):
            if candles[i - j].high >= center_high or candles[i + j].high >= center_high:
                is_high = False
                break

        # 检查是否为局部低点
        is_low = True
        for j in range(1, min_separation + 1):
            if candles[i - j].low <= center_low or candles[i + j].low <= center_low:
                is_low = False
                break

        if is_high:
            pp_list.append(PivotalPoint(
                date=candles[i].date,
                price=center_high,
                pp_type='high',
                strength=1,
                days_ago=n - 1 - i
            ))
        elif is_low:
            pp_list.append(PivotalPoint(
                date=candles[i].date,
                price=center_low,
                pp_type='low',
                strength=1,
                days_ago=n - 1 - i
            ))

    # strength 计算: 检查每个 PP 是否被后续价格多次回测
    for pp in pp_list:
        test_count = 0
        for c in candles:
            if pp.pp_type == 'high' and abs(c.high - pp.price) / pp.price < 0.005:
                test_count += 1
            elif pp.pp_type == 'low' and abs(c.low - pp.price) / pp.price < 0.005:
                test_count += 1
        pp.strength = min(3, max(1, test_count // 3))

    # 按时间倒序排序, 取最近 lookback 根内的
    pp_list.sort(key=lambda x: x.days_ago)
    return [pp for pp in pp_list if pp.days_ago <= lookback]


# ============================================================================
# 核心函数 2: 突破 + 自然回撤检测
# ============================================================================

def detect_natural_breakout(candles: list, pp_list: List[PivotalPoint]) -> Dict:
    """
    检测是否突破关键点 + 是否完成自然回撤。

    利弗莫尔原文: "不要在突破时追涨, 等自然回撤不破再入场"
    利弗莫尔原文: "假突破会迅速回到原区间, 3 天内不回来才是真突破"

    返回:
        broken: bool
        direction: 'up' / 'down' / 'none'
        days_since: int
        retest_ok: bool (回撤是否守住)
        retest_low: float (回撤最低价)
    """
    if not pp_list:
        return {
            'broken': False, 'direction': 'none', 'days_since': 999,
            'retest_ok': False, 'retest_low': 0.0, 'retest_high': 0.0
        }

    n = len(candles)
    last_close = candles[-1].close

    # 从最近的 PP 开始检查
    highs = [pp for pp in pp_list if pp.pp_type == 'high']
    lows = [pp for pp in pp_list if pp.pp_type == 'low']

    for pp in highs + lows:
        # 找到 PP 之后的 K 线
        idx = n - pp.days_ago - 1
        if idx < 0 or idx >= n - 1:
            continue

        after = candles[idx + 1:]

        if pp.pp_type == 'high':
            # 检查向上突破
            broke = False
            first_break_idx = None
            for j, c in enumerate(after):
                if c.close > pp.price * 1.001:  # 突破幅度 0.1%
                    broke = True
                    first_break_idx = j
                    break

            if not broke:
                continue

            # 检查回撤: 突破后是否回到 PP 附近但未跌破
            retest_ok = True
            retest_low = min(c.low for c in after[first_break_idx:])
            for c in after[first_break_idx:]:
                if c.low < pp.price * 0.998:  # 跌破 0.2% 视为假突破
                    retest_ok = False
                    break

            return {
                'broken': True,
                'direction': 'up',
                'pp_price': pp.price,
                'pp_date': pp.date,
                'pp_strength': pp.strength,
                'days_since': len(after) - first_break_idx,
                'retest_ok': retest_ok,
                'retest_low': retest_low,
            }

        else:  # low
            broke = False
            first_break_idx = None
            for j, c in enumerate(after):
                if c.close < pp.price * 0.999:
                    broke = True
                    first_break_idx = j
                    break

            if not broke:
                continue

            retest_ok = True
            retest_high = max(c.high for c in after[first_break_idx:])
            for c in after[first_break_idx:]:
                if c.high > pp.price * 1.002:
                    retest_ok = False
                    break

            return {
                'broken': True,
                'direction': 'down',
                'pp_price': pp.price,
                'pp_date': pp.date,
                'pp_strength': pp.strength,
                'days_since': len(after) - first_break_idx,
                'retest_ok': retest_ok,
                'retest_high': retest_high,
            }

    return {
        'broken': False, 'direction': 'none', 'days_since': 999,
        'retest_ok': False, 'retest_low': 0.0, 'retest_high': 0.0
    }


# ============================================================================
# 核心函数 3: 最小阻力线 (Line of Least Resistance)
# ============================================================================

def detect_minimum_resistance_line(candles: list, window: int = 20) -> Dict:
    """
    识别最小阻力线方向。

    利弗莫尔原文: "在阻力最小的方向上建仓"
    利弗莫尔原文: "如果高点抬高 + 低点抬高 = 上升阻力最小"
    利弗莫尔原文: "如果高点降低 + 低点降低 = 下降阻力最小"

    算法:
    - 取最近 window 根 K 线
    - 比较前半段和后半段的高点/低点趋势
    - 输出斜率 + 当前价位
    """
    if len(candles) < window:
        return {'slope': 0.0, 'state': 'flat', 'support': 0.0, 'resistance': 0.0}

    recent = candles[-window:]
    half = window // 2

    first_half = recent[:half]
    second_half = recent[half:]

    first_high = max(c.high for c in first_half)
    second_high = max(c.high for c in second_half)
    first_low = min(c.low for c in first_half)
    second_low = min(c.low for c in second_half)

    high_rising = second_high > first_high * 1.005  # 抬升 0.5%
    high_falling = second_high < first_high * 0.995
    low_rising = second_low > first_low * 1.005
    low_falling = second_low < first_low * 0.995

    # 简单线性回归估算斜率
    closes = [c.close for c in recent]
    n = len(closes)
    x_mean = (n - 1) / 2.0
    y_mean = sum(closes) / n
    numerator = sum((i - x_mean) * (closes[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    slope = numerator / denominator if denominator != 0 else 0.0

    if high_rising and low_rising:
        state = 'up'
    elif high_falling and low_falling:
        state = 'down'
    else:
        state = 'flat'

    return {
        'slope': slope,
        'state': state,
        'support': second_low,
        'resistance': second_high,
        'high_rising': high_rising,
        'low_rising': low_rising,
    }


# ============================================================================
# 核心函数 4: 六问检查 (利弗莫尔入场前的自我提问)
# ============================================================================

def check_six_questions(
    candles: list,
    pp_list: List[PivotalPoint],
    breakout: Dict,
    resistance: Dict,
    market_candles: Optional[list] = None,
) -> Dict[str, bool]:
    """
    利弗莫尔的六个核心问题 (简化为可量化的版本):

    Q1: 当前市场是否处于上升趋势? (最小阻力线向上)
    Q2: 个股是否处于上升趋势? (近 20 日 close > SMA20 且 SMA20 上扬)
    Q3: 是否刚突破关键点? (突破 ≤ 4 周内)
    Q4: 自然回撤是否守住? (突破后回撤未跌破 PP)
    Q5: 量价是否配合? (突破时放量 > 5日均量, 回调时缩量)
    Q6: 是否逆大盘? (个股强于同期大盘)
    """
    questions = {}

    # Q1: 最小阻力线
    questions['Q1_resistance_up'] = resistance['state'] == 'up'

    # Q2: 个股趋势
    if len(candles) >= 20:
        sma20 = sum(c.close for c in candles[-20:]) / 20
        sma20_prev = sum(c.close for c in candles[-25:-5]) / 20
        questions['Q2_price_above_sma20'] = candles[-1].close > sma20
        questions['Q2_sma20_rising'] = sma20 > sma20_prev
    else:
        questions['Q2_price_above_sma20'] = False
        questions['Q2_sma20_rising'] = False

    # Q3: 关键点突破 (≤ 28 天)
    questions['Q3_recent_breakout'] = (
        breakout.get('broken', False) and
        breakout.get('days_since', 999) <= 28 and
        breakout.get('direction') == 'up'
    )

    # Q4: 自然回撤守住
    questions['Q4_natural_retrace_ok'] = (
        breakout.get('broken', False) and
        breakout.get('retest_ok', False)
    )

    # Q5: 量价配合
    if len(candles) >= 10:
        recent_5_vol = sum(c.volume for c in candles[-5:]) / 5
        breakout_day_vol = 0
        days_since = breakout.get('days_since', 999)
        if days_since < 5 and days_since < len(candles):
            breakout_day_vol = candles[-(days_since + 1)].volume

        # 突破日放量, 当前价在 PP 上方
        questions['Q5_volume_confirm'] = (
            breakout_day_vol > recent_5_vol * 1.2 and
            breakout.get('retest_low', 0) >= breakout.get('pp_price', 0)
        )
    else:
        questions['Q5_volume_confirm'] = False

    # Q6: 不逆大盘 (使用最近 20 日涨幅对比)
    if market_candles and len(market_candles) >= 20:
        etf_20d_return = (candles[-1].close - candles[-20].close) / candles[-20].close
        mkt_20d_return = (market_candles[-1].close - market_candles[-20].close) / market_candles[-20].close
        questions['Q6_not_against_market'] = etf_20d_return >= mkt_20d_return * 0.8
    else:
        questions['Q6_not_against_market'] = True  # 无大盘数据时放行

    return questions


# ============================================================================
# 核心函数 5: 金字塔加仓建议
# ============================================================================

def suggest_pyramid_action(
    state: LivermoreState,
    current_price: float,
    current_position_pct: float = 0.0,
) -> Dict:
    """
    金字塔加仓规则 (利弗莫尔核心仓位管理):

    1. 第一仓: 关键点突破 + 自然回撤不破 → 25%
    2. 第二仓: 突破后上涨 3-5% → +25% (盈利加仓, 总 50%)
    3. 第三仓: 突破后上涨 6-10% → +25% (金字塔最厚, 总 75%)
    4. 超过 10% 不再加仓 → 持有
    5. 止损: 跌破最近 PP 或从最高点回撤 7%

    利弗莫尔原文: "只在盈利的仓位上加仓, 永远不在亏损的仓位上加仓"
    利弗莫尔原文: "盈利 10% 后停止加仓, 让利润奔跑"
    """
    if state.trend == 'downtrend':
        return {'action': 'exit', 'suggested_pct': 0.0,
            'reason': '下降趋势, 规避',
            'stop_price': state.last_pivot_low.price if state.last_pivot_low else 0.0}

    if state.trend == 'sideways':
        return {'action': 'hold', 'suggested_pct': current_position_pct,
            'reason': '震荡区间, 不加仓',
            'stop_price': state.last_pivot_low.price if state.last_pivot_low else 0.0}

    # uptrend
    if not state.six_questions.get('Q3_recent_breakout'):
        return {'action': 'hold', 'suggested_pct': current_position_pct,
            'reason': '上升趋势但无新突破, 持有现有仓位',
            'stop_price': state.last_pivot_low.price if state.last_pivot_low else 0.0}

    if state.last_pivot_high is None or state.last_pivot_high.price <= 0:
        return {'action': 'hold', 'suggested_pct': current_position_pct,
            'reason': '无法定位关键点, 持有', 'stop_price': 0.0}

    pp_price = state.last_pivot_high.price
    gain_pct = (current_price / pp_price - 1) * 100
    stop = pp_price * 0.98  # 跌破关键点 2% 止损

    if current_position_pct == 0:
        # 无仓位 → 按涨幅决定进场档位
        if gain_pct < 1:
            return {'action': 'hold', 'suggested_pct': 0,
                'reason': f'待突破确认(距PP {gain_pct:+.1f}%)', 'stop_price': stop}
        elif gain_pct < 5:
            return {'action': 'add_tier1', 'suggested_pct': 25,
                'reason': f'关键点突破+回撤守住→入第一仓({gain_pct:+.1f}%)', 'stop_price': stop}
        elif gain_pct < 10:
            return {'action': 'add_tier2', 'suggested_pct': 50,
                'reason': f'盈利中加仓第二档({gain_pct:+.1f}%)', 'stop_price': stop}
        else:
            return {'action': 'hold', 'suggested_pct': 0,
                'reason': f'涨幅已超10%({gain_pct:+.1f}%),等回调再进', 'stop_price': stop}
    else:
        # 已有仓位 → 决定是否加仓/减仓
        if gain_pct < -5:
            return {'action': 'exit', 'suggested_pct': 0,
                'reason': f'回撤{abs(gain_pct):.1f}%,触发止损', 'stop_price': stop}
        elif gain_pct < 2:
            return {'action': 'hold', 'suggested_pct': current_position_pct,
                'reason': f'涨幅不足({gain_pct:+.1f}%),不加仓', 'stop_price': stop}

        if current_position_pct < 25 and gain_pct >= 3:
            return {'action': 'add_tier1', 'suggested_pct': 25,
                'reason': f'盈利{gain_pct:+.1f}%→加仓至25%', 'stop_price': stop}
        elif current_position_pct < 50 and gain_pct >= 6:
            return {'action': 'add_tier2', 'suggested_pct': 50,
                'reason': f'盈利{gain_pct:+.1f}%→加仓至50%', 'stop_price': stop}
        elif current_position_pct < 75 and gain_pct >= 10:
            return {'action': 'add_tier3', 'suggested_pct': 75,
                'reason': f'盈利{gain_pct:+.1f}%→加仓至75%', 'stop_price': stop}
        else:
            return {'action': 'hold', 'suggested_pct': current_position_pct,
                'reason': f'持有({gain_pct:+.1f}%),当前仓位{current_position_pct:.0f}%', 'stop_price': stop}


# ============================================================================
# 主入口: livermore_analyze()
# ============================================================================

def livermore_analyze(
    code: str,
    candles: list,
    market_candles: Optional[list] = None,
    current_position_pct: float = 0.0,
) -> Dict:
    """
    利弗莫尔持仓手册综合分析入口。

    Args:
        code: ETF 代码
        candles: 个股 K 线 list[Candle], 至少 30 根
        market_candles: 大盘 K 线 (用于 Q6 对比), 可选
        current_position_pct: 当前持仓百分比 (0-100)

    Returns:
        dict 包含所有分析结果, 供 gen_html_report.py 直接渲染
    """
    state = LivermoreState()
    state.pivotal_points = find_pivotal_points(candles)

    # 最近的关键点
    highs = [pp for pp in state.pivotal_points if pp.pp_type == 'high']
    lows = [pp for pp in state.pivotal_points if pp.pp_type == 'low']
    state.last_pivot_high = highs[0] if highs else None
    state.last_pivot_low = lows[0] if lows else None

    # 突破状态
    breakout = detect_natural_breakout(candles, state.pivotal_points)
    if breakout['broken']:
        if breakout['direction'] == 'up':
            if breakout['days_since'] <= 3:
                state.breakout_state = 'breaking_up'
            elif breakout['retest_ok']:
                state.breakout_state = 'confirmed_up'
            else:
                state.breakout_state = 'failed'
        else:
            if breakout['days_since'] <= 3:
                state.breakout_state = 'breaking_down'
            elif breakout['retest_ok']:
                state.breakout_state = 'confirmed_down'
            else:
                state.breakout_state = 'failed'
        state.breakout_days_ago = breakout['days_since']
        state.natural_retrace_ok = breakout.get('retest_ok', False)

    # 最小阻力线
    resistance = detect_minimum_resistance_line(candles)
    state.resistance_line_slope = resistance['slope']
    state.resistance_line_support = resistance['support']
    state.resistance_line_state = resistance['state']

    # 趋势判断: 综合 PP 和最小阻力线
    if state.last_pivot_high and state.last_pivot_low:
        if candles[-1].close > state.last_pivot_high.price:
            state.trend = 'uptrend'
        elif candles[-1].close < state.last_pivot_low.price:
            state.trend = 'downtrend'
        else:
            state.trend = 'sideways'
    else:
        # 仅靠最小阻力线判断
        if resistance['state'] == 'up':
            state.trend = 'uptrend'
        elif resistance['state'] == 'down':
            state.trend = 'downtrend'
        else:
            state.trend = 'sideways'

    # 当前价相对 PP 位置
    if state.last_pivot_high:
        state.position_pct_above_last_pivot = (
            (candles[-1].close - state.last_pivot_high.price) / state.last_pivot_high.price * 100
        )

    # 六问
    state.six_questions = check_six_questions(
        candles, state.pivotal_points, breakout, resistance, market_candles
    )
    state.six_passed_count = sum(1 for v in state.six_questions.values() if v)

    # 金字塔建议 (v2: 传入当前价修复placeholder bug)
    pyramid = suggest_pyramid_action(state, candles[-1].close, current_position_pct)
    state.pyramid_action = pyramid['action']
    state.pyramid_suggested_pct = pyramid['suggested_pct']
    state.pyramid_stop_price = pyramid['stop_price']

    # 摘要
    pp_h = f"{state.last_pivot_high.price:.3f}" if state.last_pivot_high else "—"
    pp_l = f"{state.last_pivot_low.price:.3f}" if state.last_pivot_low else "—"
    state.summary = (
        f"PP-H:{pp_h} PP-L:{pp_l} | "
        f"突破:{state.breakout_state} ({state.breakout_days_ago}天前) | "
        f"最小阻力:{state.resistance_line_state} | "
        f"六问:{state.six_passed_count}/7"
    )

    # 输出 dict (供 gen_html_report.py 直接使用)
    return {
        'code': code,
        'pivotal_points': [
            {'date': pp.date, 'price': pp.price, 'type': pp.pp_type,
             'strength': pp.strength, 'days_ago': pp.days_ago}
            for pp in state.pivotal_points[:10]  # 只返回最近 10 个
        ],
        'last_pivot_high': {
            'date': state.last_pivot_high.date,
            'price': state.last_pivot_high.price,
            'strength': state.last_pivot_high.strength,
            'days_ago': state.last_pivot_high.days_ago,
        } if state.last_pivot_high else None,
        'last_pivot_low': {
            'date': state.last_pivot_low.date,
            'price': state.last_pivot_low.price,
            'strength': state.last_pivot_low.strength,
            'days_ago': state.last_pivot_low.days_ago,
        } if state.last_pivot_low else None,
        'breakout_state': state.breakout_state,
        'breakout_days_ago': state.breakout_days_ago,
        'natural_retrace_ok': state.natural_retrace_ok,
        'resistance_line_slope': state.resistance_line_slope,
        'resistance_line_support': state.resistance_line_support,
        'resistance_line_state': state.resistance_line_state,
        'trend': state.trend,
        'position_pct_above_last_pivot': state.position_pct_above_last_pivot,
        'six_questions': state.six_questions,
        'six_passed_count': state.six_passed_count,
        'pyramid_action': state.pyramid_action,
        'pyramid_suggested_pct': state.pyramid_suggested_pct,
        'pyramid_stop_price': state.pyramid_stop_price,
        'summary': state.summary,
    }


# ============================================================================
# 自测
# ============================================================================

if __name__ == '__main__':
    from models import Candle

    # 构造测试数据: 上升趋势 + 关键点突破 + 自然回撤
    test_candles = []
    base = 1.0
    for i in range(80):
        if i < 30:
            close = base + i * 0.005 + (i % 3) * 0.002  # 缓慢上升
        elif i < 40:
            close = base + 30 * 0.005 - (i - 30) * 0.008  # 回调形成低点
        elif i < 50:
            close = base + 30 * 0.005 + (i - 40) * 0.012  # 突破上升
        elif i < 60:
            close = base + 30 * 0.005 + 10 * 0.012 - (i - 50) * 0.005  # 自然回撤
        else:
            close = base + 30 * 0.005 + 10 * 0.012 + (i - 60) * 0.015  # 主升浪

        test_candles.append(Candle(
            date=f"2026-04-{(i % 30) + 1:02d}",
            open=close * 0.995,
            high=close * 1.01,
            low=close * 0.99,
            close=close,
            volume=1_000_000 + i * 10_000,
        ))

    print("=" * 60)
    print("livermore_engine 自测")
    print("=" * 60)

    result = livermore_analyze('TEST', test_candles)
    print(f"\n趋势: {result['trend']}")
    print(f"最近PP-H: {result['last_pivot_high']}")
    print(f"最近PP-L: {result['last_pivot_low']}")
    print(f"突破状态: {result['breakout_state']} ({result['breakout_days_ago']}天前)")
    print(f"最小阻力线: {result['resistance_line_state']}")
    print(f"六问: {result['six_questions']}")
    print(f"通过: {result['six_passed_count']}/7")
    print(f"金字塔建议: {result['pyramid_action']}")
    print(f"摘要: {result['summary']}")

    print("\n所有测试通过")