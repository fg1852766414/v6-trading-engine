#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V5 + 利弗莫尔双引擎 ETF 全板块扫描报告

数据流: neodata(K线) + AKShare(份额/上证历史) + sina(上证实时)
引擎: 缠论PA / stolgo / SMC / Volume(pandas_ta) / 道氏 / 威科夫 / 评分 / 利弗莫尔
输出: HTML 自包含报告 (deliverables/trading-agent/etf-full-scan-{date}.html)
"""

# ═══════════════════════════════════════════════════════════════════════════
# 1. 环境与全局路径
# ═══════════════════════════════════════════════════════════════════════════

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import sys, json, subprocess, concurrent.futures, time as _time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import pandas as pd

beijing_tz = timezone(timedelta(hours=8))
_VENV_PY = r'C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe'

sys.path.insert(0, '.')
from trading_engine import Candle, TechnicalEngine
from trading_engine.utils import ema
from trading_engine.stolgo_engine import analyze_pa
from trading_engine.smartmoneyconcepts_engine import analyze_smc
from trading_engine.volume_engine import analyze_volume
from trading_engine.scoring_engine import score_etf
from trading_engine.dow_engine import dow_quick
from trading_engine.livermore_engine import livermore_analyze
from trading_engine.data_fetcher import fetch_klines, fetch_weekly

# ═══════════════════════════════════════════════════════════════════════════
# 用户持仓 (从 data/positions.json 读取, 每周一更新)
# ═══════════════════════════════════════════════════════════════════════════
def _load_positions(path: str = 'data/positions.json') -> tuple:
    """从 JSON 加载持仓。
    返回 (持仓代码 → 实际仓位% 的字典, 总仓位%)
    如果 JSON 缺失/损坏, 返回空 dict + 0, 不报错。
    """
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        positions = {
            code: info.get('actual_pct', 0.0)
            for code, info in data.get('positions', {}).items()
        }
        total = data.get('total_pct', sum(positions.values()))
        return positions, total
    except FileNotFoundError:
        print(f'⚠️ 持仓文件不存在: {path}', file=sys.stderr)
        return {}, 0.0
    except Exception as e:
        print(f'⚠️ 持仓文件读取失败: {e}', file=sys.stderr)
        return {}, 0.0

USER_POSITIONS, TOTAL_POSITION_PCT = _load_positions()


def _range_context(cands, range_lookback=15, pre_lookback=20):
    """
    判断当前盘整区间形成前的大趋势方向。
    用于区分"上涨后中继整理" vs "下跌后底部吸筹"。
    返回: {'context': str, 'pre_chg_pct': float, 'detail': str}
    """
    if len(cands) < range_lookback + pre_lookback:
        return {'context': 'insufficient_data', 'pre_chg_pct': 0.0, 'detail': '数据不足',
                'is_consolidation': False, 'range_pct': 0.0}
    
    recent = cands[-range_lookback:]
    pre = cands[-(range_lookback + pre_lookback):-range_lookback]
    
    if not recent or not pre:
        return {'context': 'insufficient_data', 'pre_chg_pct': 0.0, 'detail': '数据不足',
                'is_consolidation': False, 'range_pct': 0.0}
    
    # 计算盘整区间：用排除极端值的稳健方法
    # 取中位数附近60%的K线的高低点范围
    recent_highs = sorted(c.high for c in recent)
    recent_lows = sorted(c.low for c in recent)
    mid_idx = len(recent_highs) // 2
    trim = max(1, len(recent_highs) // 5)  # 去掉最高最低各20%
    core_highs = recent_highs[trim:-trim] if trim > 0 else recent_highs
    core_lows = recent_lows[trim:-trim] if trim > 0 else recent_lows
    robust_high = max(core_highs) if core_highs else recent_highs[-1]
    robust_low = min(core_lows) if core_lows else recent_lows[0]
    
    if robust_low <= 0:
        return {'context': 'no_data', 'pre_chg_pct': 0.0, 'detail': '价格异常',
                'is_consolidation': False, 'range_pct': 0.0}
    recent_range_pct = (robust_high - robust_low) / robust_low * 100
    
    # 判断是否盘整：稳健波幅 < 18%，且中枢没有明显单向趋势
    is_consolidation = recent_range_pct < 18.0
    
    # 盘整前趋势：前pre_lookback根K线的涨跌幅 + 最低点确认
    pre_start_close = pre[0].close
    pre_end_close = pre[-1].close
    pre_chg_pct = (pre_end_close - pre_start_close) / pre_start_close * 100
    
    # 同时检查前期的极端低点（用于判断是否为触底反弹）
    pre_min_low = min(c.low for c in pre)
    pre_max_high = max(c.high for c in pre)
    pre_max_drawdown = (pre_min_low - pre_start_close) / pre_start_close * 100  # 前期最大回撤
    pre_total_range = (pre_max_high - pre_min_low) / pre_min_low * 100  # 前期总波幅
    
    # 分类
    if not is_consolidation:
        context = 'trending'
        detail = f'非盘整结构，近{range_lookback}日波幅{recent_range_pct:.1f}%'
    elif pre_chg_pct > 8 and pre_max_drawdown > -5:
        # 前期明确上涨且无明显深跌
        context = 'uptrend_before_box'
        detail = f'上涨+{pre_chg_pct:.1f}%后形成箱体 → 再吸筹/派发候选'
    elif pre_chg_pct > 3:
        context = 'mild_uptrend_before_box'
        if pre_max_drawdown < -8:
            detail = f'冲高回落+涨{pre_chg_pct:.0f}%最大回撤{pre_max_drawdown:.0f}%后盘整'
        else:
            detail = f'弱涨+{pre_chg_pct:.1f}%后盘整 → 方向待定'
    elif pre_chg_pct < -8:
        context = 'downtrend_before_box'
        detail = f'下跌{pre_chg_pct:.1f}%后盘整 → 底部吸筹候选'
    elif pre_chg_pct < -3:
        context = 'mild_downtrend_before_box'
        detail = f'弱跌{pre_chg_pct:.1f}%后盘整 → 方向待定'
    else:
        context = 'flat_before_box'
        detail = f'走平后盘整 → 方向待定'
    
    return {'context': context, 'pre_chg_pct': pre_chg_pct, 'detail': detail,
            'is_consolidation': is_consolidation, 'range_pct': recent_range_pct,
            'pre_max_drawdown': pre_max_drawdown}


# ═══ 信号置信度函数（降低箱体震荡中的假信号） ═══
def _signal_confidence(adx, pdi, ndi, is_consolidation, vol_ratio):
    """
    三层因子相乘，0-1连续输出，无分支。
    
    ① 趋势强度因子    = clamp(adx / 40, 0, 1)
    ② 方向明确性因子  = max(0.2, |pdi - ndi| / max(pdi + ndi, 1))
    ③ 成交量验证因子  = 0.5 + 0.5 × clamp(vol_ratio, 0.5, 1.5)
    
    箱体震荡时②自然压低，放量时③自然拉升。
    ADX=0或数据不足(<20根K线)时不衰减（全置信）。
    """
    if adx <= 0:
        return 1.0  # 无ADX数据时不衰减信号
    f1 = min(1.0, adx / 40.0)
    f2 = max(0.2, abs(pdi - ndi) / max(pdi + ndi, 1.0))
    f3 = 0.5 + 0.5 * max(0.5, min(1.5, vol_ratio))
    return round(min(1.0, f1 * f2 * f3), 3)

# ═══ 量价评级标签 ═══
def _volume_badge(vol_cmf, vol_obv, vol_ad_t, vol_obv_div, ambush_detected, ambush_stage, above_sma20=False):
    """量价评级标签（HTML徽章样式，显示在ETF名称前方便关注）。

    优先级(互斥): 🔥量价强 > ⭐埋伏(未启动) > 👀资金入 > 🔺底背离 > 🔻资金流出
    失效机制: 埋伏标的后若已站上SMA20(右侧启动) → 埋伏标签让位给量价信号(🔥/👀)
    样式借鉴 D:\\trading-system 副本 vp_tag；判定保留顶背离抑制 + AD双确认（比副本严谨）。
    """
    def _badge(text, color, title=''):
        t = f' title="{title}"' if title else ''
        return (f'<span{t} style="color:{color};font-weight:700;font-size:10px;'
                f'padding:1px 5px;border:1px solid {color};border-radius:3px;'
                f'margin-right:3px;white-space:nowrap">{text}</span>')

    cmf_val = 0.0
    if isinstance(vol_cmf, dict):
        cmf_val = float(vol_cmf.get('value') or 0.0)
    elif vol_cmf:
        cmf_val = float(vol_cmf)
    obv_bull = vol_obv == 'bullish'
    obv_bear = vol_obv == 'bearish'
    ad_bull = vol_ad_t == 'bullish'
    ad_bear = vol_ad_t == 'bearish'
    top_div = vol_obv_div == 'bearish_div'

    # ① 量价强：CMF≥0.10 + OBV/AD双多 + 无顶背离（右侧启动确认，最优先）
    #    即使同时满足埋伏（跌透后放量启动），显示"量价强"而非"埋伏"——代表埋伏已兑现
    if cmf_val >= 0.10 and obv_bull and ad_bull and not top_div:
        return _badge('🔥量价强', '#f59e0b')
    # ② 埋伏信号：低位缩量止跌（左侧建仓）— 未站上SMA20才显示；站上=已启动，让位
    if ambush_detected and not above_sma20:
        return _badge('⭐埋伏', '#22c55e', ambush_stage or '低位缩量埋伏区')
    # ③ 资金流入：CMF>0.05 + OBV/AD至少一多 + 无顶背离
    if cmf_val > 0.05 and (obv_bull or ad_bull) and not top_div:
        return _badge('👀资金入', '#38bdf8')
    # ④ 底背离：价格新低但量能不配合 → 主力吸筹迹象
    if vol_obv_div == 'bullish_div':
        return _badge('🔺底背离', '#a855f7')
    # ⑤ 资金流出警示（负向，方便规避）
    if cmf_val < -0.05 and (obv_bear or ad_bear):
        return _badge('🔻资金流出', '#ef4444')
    return ''

# ═══ 内联威科夫引擎 ═══
def _wyckoff_inline(pa_kind, vol_obv, vol_obv_div, vol_ad_trend, vol_cmf_val, stolgo_breakout, rsi=None, shrink_ok=False, chg=0, candle_high=None, candle_close=None, box_level=None, vol_high=False, range_ctx=None, confidence=1.0):
    """轻量版威科夫：直接翻译引擎输出
    range_ctx: _range_context() 返回的盘整前趋势信息，融入威科夫阶段判断。
    confidence: _signal_confidence() 计算的信号置信度，低值时抑制单信号触发。
    """
    obv_bullish_div = vol_obv_div == "bullish_div"
    obv_trend_bull = vol_obv == "bullish"
    ad_accumulating = vol_ad_trend == "bullish"
    ad_distributing = vol_ad_trend == "bearish"
    cmf_pos = vol_cmf_val and vol_cmf_val > 0.05
    is_down = chg < 0 if chg is not None else False

    # SC 恐慌抛售: RSI<30 + 放量下跌 + OBV底背离
    sc_detected = False
    if rsi is not None and rsi < 30 and is_down:
        sc_detected = True
    sc_label = "SC恐慌抛售" if sc_detected else ""

    # ST 二次测试: VPA缩量回踩 + 不创新低 + 底分型
    st_detected = shrink_ok and pa_kind == "bottom"
    st_label = "ST二次测试" if st_detected else ""

    # AR 自动反弹 (Automatic Rally): SC恐慌抛售后超卖区的首次强力反弹
    # 条件: RSI<35(仍处超卖区) + 今日大涨>3% + 放量(vol_high)
    # 威科夫事件链: SC→AR→ST→Spring，AR是下跌转吸筹的第一信号
    ar_detected = (rsi is not None and rsi < 35) and chg > 3.0 and vol_high
    ar_label = "AR自动反弹(超卖首弹)" if ar_detected else ""

    # UT 突破陷阱 (Upthrust): 价格短暂突破阻力位后回落 + AD派发 + 放量
    ut_detected = False
    if pa_kind == "top" and ad_distributing and box_level is not None and candle_high is not None and candle_close is not None:
        probed_above = candle_high > box_level * 1.003  # 上影线刺穿阻力
        failed_to_hold = candle_close <= box_level      # 收盘回落
        if probed_above and failed_to_hold and vol_high:
            ut_detected = True
    ut_label = "UT突破陷阱" if ut_detected else ""

    # Spring
    spring_score = 3 if pa_kind == "bottom" else 0
    if obv_bullish_div: spring_score += 3
    if ad_accumulating: spring_score += 2
    if cmf_pos: spring_score += 1
    spring_detected = spring_score >= 5

    # SOS
    sos_score = 0
    if stolgo_breakout and stolgo_breakout.get("is_breakout") and stolgo_breakout.get("direction") == "up":
        sos_score += 4
    if obv_trend_bull: sos_score += 2
    if ad_accumulating: sos_score += 2
    if cmf_pos: sos_score += 1
    sos_detected = sos_score >= 5

    # LPS
    lps_score = 4 if spring_detected and spring_score >= 3 else 0
    if obv_trend_bull: lps_score += 2
    if cmf_pos: lps_score += 1
    if ad_accumulating: lps_score += 1
    lps_detected = lps_score >= 5

    # Phase 判定（使用置信度抑制单信号假切换）
    # 规则：confidence < 0.35 时，单根K线的PA信号不触发阶段切换
    #      （SOS/LPS/Spring等多信号聚合不受影响）
    if sos_detected and sos_score >= 5:
        phase = "B-加仓期(SOS确认)"; phase_letter = "B"
    elif lps_detected and lps_score >= 6:
        phase = "A→B过渡(LPS确认)"; phase_letter = "A→B"
    elif ar_detected:
        # AR是SC后第一波反弹（比Spring更早），下跌转吸筹的第一信号
        phase = "AR-自动反弹(SC后首弹)"; phase_letter = "A"
    elif spring_detected:
        phase = "A-吸筹期(Spring)"; phase_letter = "A"
    elif pa_kind == "top":
        if confidence < 0.35:
            # 低置信度：单根顶分型不触发C期切换，维持当前context指示的阶段
            phase = "阶段待确认"; phase_letter = "?"
        else:
            transition_signals = 0
            if obv_bullish_div: transition_signals += 2
            if cmf_pos: transition_signals += 1
            if transition_signals >= 2:
                phase = "C→A过渡(吸筹初现)"; phase_letter = "C→A"
            else:
                phase = "C-派发期(顶分型)"; phase_letter = "C"
    else:
        phase = "阶段不明"; phase_letter = "?"

    # ── 威科夫区间上下文字段（威科夫价格位置的一部分） ──
    # 涨前/跌前趋势决定了"吸筹"与"再吸筹/派发"的区别
    phase_context = 'neutral'
    if range_ctx and range_ctx.get('is_consolidation'):
        ctx = range_ctx['context']
        if phase_letter in ('A', 'A→B'):
            if ctx in ('uptrend_before_box', 'mild_uptrend_before_box'):
                # 上涨后箱体 + A期 → 再吸筹候选(置信度较低)
                phase_context = 're_accumulation'
                # A→B过渡降为A，不要给B期的错觉
                if phase_letter == 'A→B':
                    phase = "A-吸筹期(涨后箱体)"; phase_letter = "A"
                else:
                    phase = "A-吸筹期(涨后箱体)"; phase_letter = "A"
            elif ctx in ('downtrend_before_box',):
                # 下跌后箱体 + A期 → 确认底部吸筹（置信度高）
                phase_context = 'confirmed_accumulation'
        elif phase_letter == 'C':
            if ctx in ('uptrend_before_box',):
                # 上涨后箱体 + C期 → 确认派发，不改阶段
                phase_context = 'confirmed_distribution'
            elif ctx in ('downtrend_before_box',):
                # 下跌后箱体 + C期 → 可能是底部派发衰竭，升为C→A过渡
                phase = "C→A过渡(跌后筑底)"; phase_letter = "C→A"
                phase_context = 'potential_bottoming'
        elif phase_letter in ('C→A',):
            if ctx in ('downtrend_before_box',):
                phase_context = 'potential_bottoming'

    return {
        'phase': phase, 'letter': phase_letter,
        'spring': spring_detected, 'spring_score': spring_score / 9,
        'sos': sos_detected, 'sos_score': sos_score / 9,
        'lps': lps_detected, 'lps_score': lps_score / 10,
        'sc': sc_detected, 'st': st_detected, 'ut': ut_detected,
        'ar': ar_detected,
        'sc_label': sc_label, 'st_label': st_label, 'ut_label': ut_label,
        'ar_label': ar_label,
        'phase_context': phase_context,  # 威科夫内部上下文字段
    }

# ═══════════════════════════════════════════════════════════════════════════
# 3. ETF 池与份额数据
# ═══════════════════════════════════════════════════════════════════════════

etfs = [
    ('000001.SH','上证指数'),
    # 宽基
    ('159915','创业板ETF'), ('588000','科创50ETF'),
    # 科技
    ('588200','科创芯片ETF嘉实'), ('512480','半导体ETF国联安'),
    ('159819','人工智能ETF'), ('159852','软件ETF嘉实'), ('515880','通信ETF国泰'),
    ('159869','游戏ETF华夏'), ('512980','传媒ETF广发'),
    # 医药
    ('159992','创新药ETF银华'), ('512010','医药ETF'),
    # 金融
    ('512880','证券ETF国泰'), ('512800','银行ETF华宝'),
    # 高端制造/新能源
    ('562500','机器人ETF华夏'), ('159530','机器人ETF易方达'),
    ('159326','电网设备ETF华夏'), ('159611','电力ETF广发'),
    ('515030','新能源车ETF'), ('515790','光伏ETF华泰柏瑞'),
    ('516160','新能源ETF南方'),
    # 周期资源
    ('512400','有色金属ETF南方'), ('515220','煤炭ETF国泰'),
    ('518880','华安黄金ETF'),
    # 消费
    ('512690','酒ETF'), ('159865','养殖ETF国泰'),
    ('515170','食品饮料ETF华夏'), ('159996','家电ETF国泰'),
    ('159766','旅游ETF富国'),
    # 军工/地产/港股
    ('512710','军工龙头ETF富国'), ('512200','房地产ETF南方'),
    ('513180','恒生科技ETF华夏'),
]

# ETF emoji 映射
ETF_EMOJI = {
    '000001.SH':'📊','159915':'📈','588000':'🧬',
    '588200':'🚀','512480':'🔬','159819':'🧠','159852':'💻','515880':'📡',
    '159869':'🎮','512980':'📺',
    '159992':'💊','512010':'💉',
    '512880':'🏦','512800':'🏛️',
    '562500':'🤖','159530':'🤖','159326':'⚡','159611':'🔌',
    '515030':'🔋','515790':'☀️','516160':'🌿',
    '512400':'🪙','515220':'⛏️','518880':'🥇',
    '512690':'🍷','159865':'🐷','515170':'🍔','159996':'❄️','159766':'✈️',
    '512710':'🪖','512200':'🏘️','513180':'🇭🇰',
}

# v3.3: 份额+溢价数据源已全部移除（用户"份额/溢价没什么卵用"），不再调用 etf_shares.py
share_date_str = ''

# ═══════════════════════════════════════════════════════════════════════════
# 4. 并发预取 ETF K 线 + 周K（统一 data_fetcher）
# ═══════════════════════════════════════════════════════════════════════════

rows = []
summary_rows = ''

# ── 并发预取所有 ETF 日K + 周K (统一 data_fetcher) ──
cand_cache = {}
_weekly_cache = {}
_prev_score_cache: dict = {}  # 每只ETF最近20次评分，用于评分引擎确定性平滑/翻转惩罚

def _prefetch_etf(code, name):
    """单只ETF预取：日K + 周K"""
    cands = fetch_klines(code, name)
    wk = fetch_weekly(code, name)
    return code, cands, wk

print(f'[并发] 预取 {len(etfs)-1} 只ETF日K+周K (max_workers=4)...', file=sys.stderr)
_t0 = _time.time()
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    _prefetch_futs = {executor.submit(_prefetch_etf, c, n): c for c, n in etfs if c != '000001.SH'}
    for _pf in concurrent.futures.as_completed(_prefetch_futs):
        _c, _cands, _wk = _pf.result()
        if _cands:
            cand_cache[_c] = _cands
        if _wk is not None:
            _weekly_cache[_c] = _wk
print(f'[并发] 预取完成 (日K {len(cand_cache)}只, 周K {len(_weekly_cache)}只, {_time.time()-_t0:.1f}s)', file=sys.stderr)

# 上证指数单独拉取（数据量大，独立处理）
_sh_cands = fetch_klines('000001.SH', '上证指数')
if _sh_cands:
    cand_cache['000001.SH'] = _sh_cands
print(f'[上证] 指数K线 {len(_sh_cands)}根', file=sys.stderr)

# ═══════════════════════════════════════════════════════════════════════════
# 5. 主分析循环: 每只ETF跑全引擎
# ═══════════════════════════════════════════════════════════════════════════

def _atr(cands, period=14):
    """真实波幅均值 ATR (Wilder 平滑): 衡量标的平均波动幅度。
    止损位 = 现价 - 2×ATR (标准ATR止损距离, 过滤日常噪音, 主表新增列)"""
    if len(cands) < period + 1:
        return None
    trs = []
    for i in range(1, len(cands)):
        h, l, pc = cands[i].high, cands[i].low, cands[i-1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs[:period]) / period
    for t in trs[period:]:
        atr = (atr * (period - 1) + t) / period
    return atr

for code, name in etfs:
    print(f'  {name}...', file=sys.stderr)
    cands = cand_cache.get(code, [])
    if len(cands) < 5:
        print(f'  ⚠️ {name}: 数据不足({len(cands)}根)，跳过', file=sys.stderr)
        continue

    # ── 利弗莫尔分析 (与 V5 完全独立, 输出 lm_* 字段) ──
    # 上证指数跳过 (不是个股, 且 K 线来自 AKShare, 不足以驱动关键点检测)
    if code != '000001.SH':
        try:
            current_pos_pct = USER_POSITIONS.get(code, 0.0)
            lm_result = livermore_analyze(code, cands, cands, current_position_pct=current_pos_pct)
        except Exception as e:
            print(f'  [Livermore ERROR] {code}: {e}', file=sys.stderr)
            lm_result = None
    else:
        lm_result = None

    # ── 盘整区间前趋势判断 ──
    rc = _range_context(cands)
    if rc['context'] == 'uptrend_before_box':
        print(f'  ⚠️ {name}: 上涨+{rc["pre_chg_pct"]:.1f}%后箱体整理，注意再吸筹/派发区别', file=sys.stderr)

    te = TechnicalEngine()
    last = cands[-1]; prev = cands[-2]
    atr_v = _atr(cands, 14)
    if atr_v:
        atr_sl_v = last.close - 2 * atr_v
        atr_pct = (atr_sl_v / last.close - 1) * 100
        atr_sl = f'{atr_sl_v:.3f} ({atr_pct:.1f}%)'
    else:
        atr_sl = '—'
    closes = [c.close for c in cands]
    ev = ema(closes, 20)[-1] if len(closes) >= 20 else sum(closes[-20:])/20
    chg = (last.close/prev.close - 1) * 100
    # 除权检测：单日涨跌幅超过25%极端值时标记，不参与涨跌幅/距EMA计算
    corporate_action = False
    if abs(chg) > 25:
        corporate_action = True
        chg = 0.0  # 除权日涨跌幅无意义，归零
        print(f'  ⚠️ {name}: 检测到极端涨跌幅({chg:.0f}%)，疑似除权，已标记', file=sys.stderr)
    vol5 = sum(c.volume for c in cands[-6:-1])/5
    vol_ratio = last.volume / vol5 * 100 if vol5 > 0 else 0

    fr = te.detect_fractal(cands)
    pa_icon = '✅' if fr.kind.value == 'bottom' else '❌'
    pa_kind = fr.kind.value
    pa_valid = bool(getattr(fr, 'is_valid', False))  # 底分型有效性(EMA压制下常无效), 评分引擎用
    pa_fractal_info = ''
    if fr.index >= 2:
        l, m, r2 = cands[fr.index-1], cands[fr.index], cands[fr.index+1]
        if pa_kind == 'bottom':
            v = te.validate_bottom_fractal(cands, fr)
            pa_fractal_info = f'{l.date}|{m.date}|{r2.date}|左低{l.low:.3f}|中低{m.low:.3f}|右低{r2.low:.3f}|右收{r2.close:.3f}|得分{v.quality_score}/3|有效={v.is_valid}'
        elif pa_kind == 'top':
            pa_fractal_info = f'{l.date}|{m.date}|{r2.date}|左高{l.high:.3f}|中高{m.high:.3f}|右高{r2.high:.3f}|形态=顶分型'

    rsi_raw = te.calc_rsi(closes)
    rsi = rsi_raw if isinstance(rsi_raw, float) else rsi_raw[-1]

    # ═══ stolgo PA 分析 ═══
    stolgo_result = analyze_pa(cands, code)
    # K线形态
    stolgo_patterns = [p for p in stolgo_result['candlestick_patterns'] if p['detected']]
    stolgo_pattern_str = '; '.join(f'{p["name"]}({p["signal_type"]})' for p in stolgo_patterns) if stolgo_patterns else '无'
    # 突破
    bo = stolgo_result['breakout']
    stolgo_breakout_str = bo['description'] if bo['is_breakout'] else '箱体内'
    # S/R
    sr = stolgo_result['sr_levels']
    # 趋势
    tf = stolgo_result['trend']
    # 摘要
    stolgo_summary = stolgo_result['summary']

    # ═══ SMC 市场结构分析（精简: 前H/L + 回撤%）═══
    smc_result = analyze_smc(cands)
    smc_phl = smc_result['previous_hl']
    smc_ret = smc_result['retracement']
    smc_summary = smc_result['summary']

    # ═══ Volume 量价分析 (pandas_ta) ═══
    vol_result = analyze_volume(cands)
    vol_cmf = vol_result['cmf']
    vol_mfi = vol_result['mfi']
    vol_obv = vol_result['obv_trend']
    vol_obv_div = vol_result['obv_divergence']
    vol_ad = vol_result['ad']
    vol_vwap = vol_result['vwap']
    vol_vwap_dist = vol_result['vwap_dist_pct']
    vol_summary = vol_result['summary']
    ambush_sig = vol_result.get('ambush') or {}
    ambush_detected = bool(ambush_sig.get('detected'))
    ambush_stage = ambush_sig.get('stage', '')
    ambush_reason = ambush_sig.get('reason', '')
    # 量价评级标签（ETF名称前置，方便关注量价优秀的标的）
    # 失效机制: 埋伏后站上SMA20(右侧启动) → 埋伏标签让位给量价信号(🔥量价强/👀资金入)
    _sma20_v = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    above_sma20 = _sma20_v is not None and last.close > _sma20_v
    # 有效埋伏信号: 三要素满足 且 未站上SMA20(未右侧启动) —— 主表徽章/操作建议/详情卡统一用这个
    ambush_active = ambush_detected and not above_sma20
    volume_tag = _volume_badge(
        vol_cmf, vol_obv,
        vol_ad['trend'] if isinstance(vol_ad, dict) else vol_ad,
        vol_obv_div, ambush_detected, ambush_stage, above_sma20,
    )

    # ═══ 道氏趋势分析 ═══
    _weekly_df = _weekly_cache.get(code)
    dow_result = dow_quick(cands, weekly_df=_weekly_df)

    shrink_ok = last.volume < vol5 * 0.8
    price_ok = last.low >= prev.low
    surge_ok = last.volume > vol5 * 1.5 and chg > 2
    vpa = '✅' if (shrink_ok and price_ok) or surge_ok else '❌'
    vpa_info = f'5日均量={vol5/1e8:.1f}亿 · 今日量={last.volume/1e8:.1f}亿({vol_ratio:.0f}%) · 缩量条件={shrink_ok} · 价不创新低={price_ok} · 放量突破={surge_ok}'

    # ═══ 信号置信度计算（降低箱体震荡假切换） ═══
    import pandas_ta as pta
    import pandas as pd
    _adx_df = pta.adx(high=pd.Series([c.high for c in cands]),
                       low=pd.Series([c.low for c in cands]),
                       close=pd.Series([c.close for c in cands]), length=min(14, len(cands)//2))
    _adx_v = 0.0; _pdi = 25.0; _ndi = 25.0
    if _adx_df is not None and len(_adx_df) > 0:
        _adx_col = [c for c in _adx_df.columns if 'ADX' in c]
        _pdi_col = [c for c in _adx_df.columns if 'DMP' in c]
        _ndi_col = [c for c in _adx_df.columns if 'DMN' in c]
        if _adx_col:
            _av = _adx_df[_adx_col[0]].iloc[-1]
            _adx_v = round(_av, 1) if pd.notna(_av) else 0.0
        if _pdi_col:
            _pv = _adx_df[_pdi_col[0]].iloc[-1]
            _pdi = round(_pv, 1) if pd.notna(_pv) else 25.0
        if _ndi_col:
            _nv = _adx_df[_ndi_col[0]].iloc[-1]
            _ndi = round(_nv, 1) if pd.notna(_nv) else 25.0
    _vr_ratio = last.volume / vol5 if vol5 > 0 else 1.0
    _is_consol = rc.get('is_consolidation', False) if rc else False
    sig_conf = _signal_confidence(_adx_v, _pdi, _ndi, _is_consol, _vr_ratio)

    # ═══ Wyckoff 威科夫分析（内联） ═══
    _w_ad_trend = vol_ad['trend'] if isinstance(vol_ad, dict) else str(vol_ad)
    _w_cmf_val = vol_cmf['value'] if isinstance(vol_cmf, dict) and vol_cmf['value'] else 0.0
    # v3.3: 份额+溢价已全部删除
    # 计算 UT 需要的参数
    box_level = bo.get('level') if bo.get('is_breakout') else sr.get('resistance_21') if sr.get('resistance_21') else None
    vol_high = last.volume > vol5 * 1.2 if vol5 > 0 else False
    wyckoff_result = _wyckoff_inline(
        pa_kind=pa_kind,
        vol_obv=vol_obv,
        vol_obv_div=vol_obv_div,
        vol_ad_trend=_w_ad_trend,
        vol_cmf_val=_w_cmf_val,
        stolgo_breakout=bo,
        rsi=rsi,
        shrink_ok=shrink_ok,
        chg=chg,
        candle_high=last.high,
        candle_close=last.close,
        box_level=box_level,
        vol_high=vol_high,
        range_ctx=rc,
        confidence=sig_conf,
    )
    w_phase_desc = wyckoff_result['phase']
    w_phase_letter = wyckoff_result['letter']
    w_phase_context = wyckoff_result.get('phase_context', 'neutral')
    w_spring_detected = wyckoff_result['spring']
    w_sos_detected = wyckoff_result['sos']
    w_lps_detected = wyckoff_result['lps']
    w_ar_detected = wyckoff_result.get('ar', False)

    dist = (last.close/ev - 1) * 100
    dc = 'grn' if dist > 1 else ('red' if dist < -3 else 'yel')

    # Generate advice per ETF — 威科夫替代VPA
    w_sig = ''
    if w_sos_detected: w_sig += 'SOS✓ '
    if w_lps_detected: w_sig += 'LPS✓ '
    if w_spring_detected: w_sig += 'Spring✓ '
    if wyckoff_result.get('st'): w_sig += 'ST✓ '
    if w_ar_detected: w_sig += 'AR✓ '
    
    advice = ''
    if w_phase_letter == 'B':
        advice = f'🟢 B-加仓期 {w_sig}'
    elif w_phase_letter in ('A', 'A→B'):
        advice = f'🟡 A-吸筹期 {w_sig}'
    elif w_phase_letter == 'C→A':
        advice = '🟡 C→A过渡(吸筹初现)'
    elif w_phase_letter == 'C':
        if rsi < 30:
            advice = '🔴 C-派发期+RSI超卖，等底分型'
        elif dist < -5:
            advice = '🔴 C-派发期+深度偏离EMA，等结构修复'
        else:
            advice = '🔴 C-派发期(顶分型)，观望'
    elif w_phase_letter == 'D':
        advice = '🔴 D-下跌期，规避'
    else:
        advice = '🟡 阶段不明，观望'
    
    # 如果威科夫信号+PA底分型 → 更强信号
    if w_sos_detected and pa_kind == 'bottom':
        advice += ' | 底分型+SOS确认'

    if dist > 3:
        advice += ' | 距EMA较远注意回调'
    if rsi < 30:
        advice += ' | RSI超卖'
    if rsi > 65:
        advice += ' | RSI偏高'

    # Volume signal enrich
    if vol_cmf['signal'] == 'bullish' and vol_cmf['value']:
        advice += f' | 量价流+({vol_cmf["value"]:.2f})'
    elif vol_cmf['signal'] == 'bearish' and vol_cmf['value']:
        advice += f' | 量价流-({vol_cmf["value"]:.2f})'
    if vol_mfi['signal'] == 'oversold':
        advice += ' | 资金流量超卖'
    elif vol_mfi['signal'] == 'overbought':
        advice += ' | 资金流量过热'
    if vol_obv_div == 'bullish_div':
        advice += ' | 能量潮底背离←'
    elif vol_obv_div == 'bearish_div':
        advice += ' | 能量潮顶背离→'

    # stolgo pattern & breakout enrich
    if '多头吞没' in stolgo_pattern_str:
        advice += ' | stolgo:多头吞没🔥'
    elif '空头吞没' in stolgo_pattern_str:
        advice += ' | stolgo:空头吞没💀'
    elif '锤子' in stolgo_pattern_str:
        advice += ' | stolgo:锤子线'
    if bo['is_breakout']:
        advice += f' | {"突破↑" if bo["direction"]=="up" else "破位↓"}强度{bo["strength"]:.0%}'

    # SMC context
    # 修复：阈值从 78 提到 90，并只在"已破前低"时才有信号意义，否则只是位置偏深
    if smc_ret['current_pct'] and smc_ret['current_pct'] > 90 and smc_phl.get('broken_low'):
        advice += ' | SMC深度回撤+破前低⚠️'

    # 威科夫区间上下文字段（已融入阶段判断）
    if w_phase_context == 're_accumulation':
        if rc.get('pre_max_drawdown', 0) < -8:
            advice += f' | ⚠️冲高回落箱体(最大回撤{rc["pre_max_drawdown"]:.0f}%)'
        else:
            advice += f' | ⚠️涨后箱体(先涨+{rc["pre_chg_pct"]:.0f}%)'
    elif w_phase_context in ('confirmed_accumulation', 'potential_bottoming'):
        advice += f' | 📉跌后箱体(先跌{rc["pre_chg_pct"]:.0f}%)'

    # Build summary row
    etf_emoji = ''
    for ec in ETF_EMOJI:
        if code.startswith(ec):
            etf_emoji = ETF_EMOJI[ec]
            break

    # Determine action tag (威科夫替代VPA)
    # 初版不含评分门槛 (etf_score 还没算),后面会在评分后重写
    if w_phase_letter == 'B' and (w_sos_detected or w_lps_detected):
        action_tag = '→ 关注买入'
    elif w_phase_letter in ('A', 'A→B') and w_spring_detected:
        action_tag = '→ 关注买入'
    elif w_phase_letter in ('A', 'A→B'):
        action_tag = '→ 等威科夫信号'
    elif w_phase_letter == 'C→A':
        action_tag = '→ 观察'
    elif w_phase_letter == 'C':
        action_tag = '→ 观望'
    elif w_phase_letter == 'D':
        action_tag = '→ 规避'
    else:
        action_tag = '→ 持有观察'

    # 🪤 埋伏信号介入：阶段不明/观望 但量价出现"地量止跌" 且未站上SMA20(左侧窗口) → 提示埋伏
    if ambush_active and ambush_stage == '地量止跌' and w_phase_letter in ('?', 'C', 'D'):
        action_tag = '→ 🪤埋伏(10%观察)'

    # Wyckoff phase tag + position sizing
    w_phase_tag = ''
    position_advice = ''
    # ── 仓位建议（威科夫阶段 + PA结构双重判定） ──
    # 即使威科夫给了A/B期，如果PA是顶分型+CMF为负，说明结构不支持
    pa_is_top = pa_kind == 'top'
    cmf_is_neg = vol_cmf['signal'] == 'bearish' or (isinstance(vol_cmf.get('value'), (int,float)) and vol_cmf['value'] < -0.02)
    
    # 先按威科夫阶段定基础仓位
    base_position = '0-5%'
    if w_phase_letter == 'B':
        w_phase_tag = '📈B'
        base_position = '50-70%'
    elif w_phase_letter in ('A', 'A→B'):
        w_phase_tag = '📉A'
        base_position = '25-35%'
    elif w_phase_letter == 'C→A':
        w_phase_tag = '🔄C→A'
        base_position = '10%观察'
    elif w_phase_letter == 'C':
        w_phase_tag = '📊C'
        base_position = '0-5%'
    elif w_phase_letter == 'D':
        w_phase_tag = '📉D'
        base_position = '0%'
    
    # PA和量价修正：顶分型+CMF负 → 降级仓位
    if pa_is_top and cmf_is_neg:
        position_advice = '0-5%'
    elif pa_is_top:
        # 顶分型但CMF为正 → 降到下一档
        downgrade = {'50-70%': '25-35%', '25-35%': '10%观察', '10%观察': '0-5%', '0-5%': '0-5%', '0%': '0%'}
        position_advice = downgrade.get(base_position, '0-5%')
    elif cmf_is_neg and base_position not in ('0-5%', '0%'):
        position_advice = '10%观察'
    else:
        position_advice = base_position

    # 道氏趋势标签
    dow_icon = {'bull':'📈','bear':'📉','range':'➡️'}.get(dow_result['primary']['direction'], '')
    dow_str = dow_result['primary']['strength']

    # 先构建rows dict用于评分
    row_data = dict(n=name, c=code, p=f'{last.close:.3f}', ch=f'{chg:+.2f}',
        chc='red' if chg < 0 else 'grn', ds=f'{dist:+.1f}', dc=dc,
        pa=pa_icon, vp=vpa,
        atr_sl=atr_sl,  # ATR(14)止损位 = 现价 - 2×ATR
        rs=f'{rsi:.1f}',
        ca=corporate_action,  # 除权标记
        # 量(亿): 指数用成交额(亿元), ETF 用成交手数(亿手)
        vl=(f'{last.volume/1e8:.1f}' if code != '000001.SH'
            else f'{last.volume/1e8:.0f}'),
        pai=pa_fractal_info, vpi=vpa_info, adv=advice,
        pk=pa_kind, pa_valid=pa_valid, rsi_v=rsi, chg_v=chg, dist_v=dist,
        vol5=vol5, last_vol=last.volume, shrink_ok=shrink_ok, price_ok=price_ok,
        sto_pat=stolgo_pattern_str, sto_bo=stolgo_breakout_str,
        sto_consol=tf['consolidating'], sto_sum=stolgo_summary,
        smc_phl_h=smc_phl['high'], smc_phl_l=smc_phl['low'],
        smc_phl_bh=smc_phl['broken_high'], smc_phl_bl=smc_phl['broken_low'],
        smc_sum=smc_summary,
        vol_cmf=f'{vol_cmf["value"]:.2f}|{vol_cmf["signal"]}' if vol_cmf['value'] else '',
        vol_mfi=f'{vol_mfi["value"]:.0f}|{vol_mfi["signal"]}' if vol_mfi['value'] else '',
        vol_obv=vol_obv, vol_obv_div=vol_obv_div,
        vol_ad_t=vol_ad['trend'], vol_vwap=vol_vwap, vol_vwap_dist=vol_vwap_dist,
        vol_sum=vol_summary,
        ambush_stage=ambush_stage, ambush_reason=ambush_reason, ambush_active=ambush_active,
        vol_tag=volume_tag,
        w_phase=wyckoff_result['phase'], w_letter=wyckoff_result['letter'],
        w_spring_det=wyckoff_result['spring'], w_sos_det=wyckoff_result['sos'],
        w_lps_det=wyckoff_result['lps'], w_spring_s=wyckoff_result['spring_score'],
        w_sos_s=wyckoff_result['sos_score'], w_lps_s=wyckoff_result['lps_score'],
        w_sc=wyckoff_result['sc'], w_st=wyckoff_result['st'], w_ut=wyckoff_result['ut'],
        w_ar=wyckoff_result.get('ar', False),
        w_sc_lbl=wyckoff_result['sc_label'], w_st_lbl=wyckoff_result['st_label'],
        w_ut_lbl=wyckoff_result['ut_label'], w_ar_lbl=wyckoff_result.get('ar_label', ''),
        dow_p_dir=dow_result['primary']['direction'], dow_p_str=dow_result['primary']['strength'],
        dow_p_lbl=dow_result['primary']['label'], dow_s_dir=dow_result['secondary']['direction'],
        dow_s_str=dow_result['secondary']['strength'], dow_s_adx=dow_result['secondary']['adx'],
        dow_s_lbl=dow_result['secondary']['label'], dow_vol_ok=dow_result['volume_confirms'],
        dow_sum=dow_result['summary'],
    )
    # 评分 (v3.1: prev_raw 传入评分引擎做确定性平滑/翻转惩罚)
    _prev_raw = row_data.get('_prev_scores', [])
    _se = score_etf(row_data, prev_raw=_prev_raw)
    etf_score, etf_grade, etf_grade_lbl, _, e0_warning = _se[:5]
    etf_stable, etf_grade_stable, etf_daily_chg, etf_flip_cnt = _se[5:9]
    row_data['sc'] = etf_score
    row_data['sg'] = etf_grade
    # 记录评分历史(最近20次)供下次平滑
    _prev_score_cache.setdefault(code, []).append(etf_score)
    if len(_prev_score_cache[code]) > 20:
        _prev_score_cache[code] = _prev_score_cache[code][-20:]
    row_data['_prev_scores'] = _prev_score_cache[code][:-1]  # 不含本次
    # 评分颜色(用稳定分, 防单日抖动)
    sc_color = {'A':'#22c55e','B':'#34d399','C':'#eab308','D':'#f97316','E':'#ef4444'}.get(etf_grade_stable, '#94a3b8')

    # 评分门槛联动: B 期<40 或 A 期<35 → 操作降级
    if etf_score < 40 and w_phase_letter == 'B' and (w_sos_detected or w_lps_detected):
        action_tag = '→ 等待<br><span style="font-size:10px;color:#94a3b8">(评分过低)</span>'
    elif etf_score < 35 and w_phase_letter in ('A', 'A→B') and w_spring_detected:
        action_tag = '→ 等威科夫信号<br><span style="font-size:10px;color:#94a3b8">(评分过低)</span>'

    # E0 警告：分数被截断到 0，多维度同时极差，需要在汇总里显眼标记
    e0_badge = ' ⚠️E0' if e0_warning else ''

    # ── 利弗莫尔展示字段 (lm_result 已在上方计算) ──
    if lm_result:
        lm_trend = lm_result['trend']
        lm_trend_icon = {'uptrend': '📈上升', 'downtrend': '📉下降', 'sideways': '➡️震荡'}.get(lm_trend, '—')
        if lm_result.get('last_pivot_high') and lm_result.get('last_pivot_low'):
            lm_pp_label = f"PP-H:{lm_result['last_pivot_high']['price']:.3f}<br>PP-L:{lm_result['last_pivot_low']['price']:.3f}"
        else:
            lm_pp_label = "无PP"
        lm_six_count = lm_result['six_passed_count']
        lm_six_color = '#22c55e' if lm_six_count >= 5 else '#fbbf24' if lm_six_count >= 3 else '#ef4444'
        lm_action = lm_result['pyramid_action']
        lm_action_label = {
            'add_tier1': '+1档(25%)',
            'add_tier2': '+2档(50%)',
            'add_tier3': '+3档(75%)',
            'hold': '持有',
            'trim': '减仓',
            'exit': '清仓',
        }.get(lm_action, lm_action)
        lm_pyramid_text = f"{lm_action_label}<br><span style='font-size:9px;color:#94a3b8'>止:{lm_result['pyramid_stop_price']:.3f}</span>"
        lm_pyramid_color = {
            'add_tier1': '#34d399', 'add_tier2': '#22c55e', 'add_tier3': '#16a34a',
            'hold': '#94a3b8', 'trim': '#fbbf24', 'exit': '#ef4444',
        }.get(lm_action, '#94a3b8')
        # 同时存到 row_data 供详情卡使用
        row_data['lm_result'] = lm_result
    else:
        lm_trend_icon = '—'
        lm_pp_label = '—'
        lm_six_count = '—'
        lm_six_color = '#94a3b8'
        lm_pyramid_text = '—'
        lm_pyramid_color = '#94a3b8'

    summary_rows += f'''<tr style="line-height:1.6"><td style="font-weight:600;text-align:left;padding:5px 10px;font-size:13px">{etf_emoji} {name}</td>
<td style="text-align:center;padding:5px 10px;font-size:13px;letter-spacing:1px">{pa_icon}</td>
<td style="text-align:center;padding:5px 10px;font-weight:600;font-size:13px">{w_phase_tag}</td>
<td style="text-align:center;padding:5px 10px;font-size:12px">{dow_icon}{dow_str:.0%}</td>
<td style="text-align:center;padding:5px 10px;font-weight:700;font-size:14px;color:{sc_color}">{etf_grade}{etf_score}{e0_badge}</td>
<td style="text-align:left;padding:5px 10px;font-size:12px;color:#94a3b8">{advice}</td>
<td style="text-align:center;padding:5px 12px;font-weight:600;font-size:13px;min-width:140px;white-space:normal">{action_tag}</td>
<td style="text-align:center;padding:5px 12px;font-weight:600;font-size:13px;min-width:90px;white-space:normal;color:{'#22c55e' if position_advice in ('50-70%','25-35%') else '#eab308' if '观察' in position_advice else '#6b7280'}">{position_advice}</td>
<td style="text-align:center;padding:5px 8px;font-size:11px">{lm_trend_icon}<br><span style="font-size:10px;color:#94a3b8">{lm_pp_label}</span></td>
<td style="text-align:center;padding:5px 8px;font-weight:600;font-size:13px;color:{lm_six_color}">{lm_six_count}/7</td>
<td style="text-align:center;padding:5px 8px;font-weight:600;font-size:11px;color:{lm_pyramid_color};white-space:normal">{lm_pyramid_text}</td></tr>'''

    rows.append(row_data)

# ═══════════════════════════════════════════════════════════════════════════
# 6. 大盘系统性风控
# ═══════════════════════════════════════════════════════════════════════════

# 统计所有ETF（不含上证指数）的威科夫阶段+道氏趋势+RSI状态
risk_etf_count = 0
risk_c_phase = 0
risk_bear_trend = 0
risk_rsi_under30 = 0
risk_rsi_under20 = 0
risk_etf_total = 0
for r in rows:
    if r['c'] == '000001.SH': continue
    risk_etf_total += 1
    if r['w_letter'] == 'C': risk_c_phase += 1
    if r.get('dow_p_dir') == 'bear': risk_bear_trend += 1
    if r['rsi_v'] < 30: risk_rsi_under30 += 1
    if r['rsi_v'] < 20: risk_rsi_under20 += 1

c_pct = risk_c_phase / risk_etf_total if risk_etf_total > 0 else 0
bear_pct = risk_bear_trend / risk_etf_total if risk_etf_total > 0 else 0

# 风险等级判定
if c_pct >= 0.7 and bear_pct >= 0.5 and risk_rsi_under30 >= 3:
    risk_level = "🔴 高风险"
    risk_summary = f"系统性风险！{risk_c_phase}/{risk_etf_total}只ETF在C-派发期({c_pct:.0%})，{risk_bear_trend}只道氏熊市，{risk_rsi_under30}只RSI超卖。建议总仓位≤20%，暂停新建仓。"
    risk_color = "#ef4444"
elif c_pct >= 0.5 or bear_pct >= 0.5:
    risk_level = "🟡 中等风险"
    risk_summary = f"市场偏弱。{risk_c_phase}/{risk_etf_total}只C-派发期({c_pct:.0%})，{risk_bear_trend}只道氏熊市，{risk_rsi_under30}只超卖。建议总仓位≤30%，精选个股。"
    risk_color = "#eab308"
elif c_pct <= 0.2 and bear_pct <= 0.2:
    risk_level = "🟢 低风险"
    risk_summary = f"市场健康。仅{risk_c_phase}/{risk_etf_total}只C-派发期，{risk_bear_trend}只道氏熊市。可按仓位规则执行。"
    risk_color = "#22c55e"
else:
    risk_level = "🟡 中等风险"
    risk_summary = f"市场震荡。{risk_c_phase}/{risk_etf_total}只C-派发期({c_pct:.0%})，{risk_bear_trend}只道氏熊市。建议总仓位≤40%。"
    risk_color = "#eab308"

market_risk_html = f'''<div style="display:flex;align-items:center;gap:12px;background:{risk_color}15;border:1px solid {risk_color}40;border-radius:8px;padding:10px 14px;margin-bottom:14px">
<div style="font-size:16px;font-weight:700;color:{risk_color};white-space:nowrap">{risk_level}</div>
<div style="flex:1;font-size:12px;color:#cbd5e1">{risk_summary}</div>
<div style="font-size:11px;color:#94a3b8;text-align:right">
C期:{risk_c_phase}/{risk_etf_total} · 熊:{risk_bear_trend} · 超卖:{risk_rsi_under30}
</div>
</div>'''

# ═══ 大盘风控数据存入变量，供报告引用 ═══
# 提取上证指数关键数据
_idx_dow_p_str = 0
_idx_dow_s_adx = 0
_idx_cmf_v = 0
for r in rows:
    if r['c'] == '000001.SH':
        _idx_dow_p_str = r.get('dow_p_str', 0)
        _idx_dow_s_adx = r.get('dow_s_adx', 0)
        _idx_cmf_v = float(r.get('vol_cmf','0|').split('|')[0]) if r.get('vol_cmf') else 0
        break

market_risk_data = {
    'level': risk_level,
    'summary': risk_summary,
    'c_count': risk_c_phase,
    'c_total': risk_etf_total,
    'c_pct': c_pct,
    'bear_count': risk_bear_trend,
    'rsi30_count': risk_rsi_under30,
    'color': risk_color,
    'dow_p_str': _idx_dow_p_str,
    'dow_s_adx': _idx_dow_s_adx,
    'cmf_v': _idx_cmf_v,
}

# ═══════════════════════════════════════════════════════════════════════════
# 7. 大盘预测卡 & 持仓概览卡 & HTML 生成
# ═══════════════════════════════════════════════════════════════════════════

# 大盘预测卡片的动态变量 (从 rows 读上证指数 + 从 USER_POSITIONS 读持仓)
idx_row = next((r for r in rows if r['c'] == '000001.SH'), None)

# 引擎诊断
if idx_row:
    idx_dow_p_dir = idx_row.get('dow_p_dir', '?')
    idx_dow_p_str = idx_row.get('dow_p_str', 0) or 0
    idx_dow_s_dir = idx_row.get('dow_s_dir', '?')
    idx_dow_s_adx = idx_row.get('dow_s_adx', 0) or 0
    idx_dow_vol_ok = idx_row.get('dow_vol_ok', False)
    idx_w_phase = idx_row.get('w_phase', '?')
    idx_w_letter = idx_row.get('w_letter', '?')
    idx_w_sc = idx_row.get('w_sc', False)
    idx_w_st = idx_row.get('w_st', False)
    idx_w_ut = idx_row.get('w_ut', False)
    idx_w_ar = idx_row.get('w_ar', False)
    idx_cmf_v = market_risk_data['cmf_v']
    idx_vol_obv = idx_row.get('vol_obv', 'neutral')
    idx_vol_ad_t = idx_row.get('vol_ad_t', 'neutral')
    idx_rsi_v = idx_row.get('rsi_v', 50)
    idx_dist_v = idx_row.get('dist_v', 0)  # 距 EMA%
    idx_last_close = float(idx_row.get('p', '0').replace(',', ''))
else:
    idx_dow_p_dir = idx_dow_s_dir = idx_w_phase = idx_w_letter = '?'
    idx_dow_p_str = idx_dow_s_adx = idx_cmf_v = idx_rsi_v = idx_dist_v = idx_last_close = 0
    idx_dow_vol_ok = False
    idx_w_sc = idx_w_st = idx_w_ut = idx_w_ar = False
    idx_vol_obv = idx_vol_ad_t = 'neutral'

# 道氏状态描述
_dow_p_icon = {'bull':'📈','bear':'📉','range':'➡️'}.get(idx_dow_p_dir, '❓')
_dow_s_icon = {'bull':'📈','bear':'📉','range':'➡️'}.get(idx_dow_s_dir, '❓')
_dow_color = '#22c55e' if idx_dow_p_dir == 'bull' else '#ef4444' if idx_dow_p_dir == 'bear' else '#94a3b8'
_dow_label = f'{_dow_p_icon}{_dow_s_icon} {"双熊" if (idx_dow_p_dir=="bear" and idx_dow_s_dir=="bear") else "双牛" if (idx_dow_p_dir=="bull" and idx_dow_s_dir=="bull") else "分化"}'
_dow_text = f'{_dow_label} 主{idx_dow_p_dir}({idx_dow_p_str:.0%}) 次{idx_dow_s_dir}(ADX={idx_dow_s_adx:.0f}) {"量价确认✓" if idx_dow_vol_ok else "量价背离⚠️"}'

# 威科夫状态
_w_color = '#22c55e' if idx_w_letter in ('A','B','A→B') else '#fbbf24' if idx_w_letter == 'C→A' else '#ef4444'
_w_signals = []
if idx_w_sc: _w_signals.append('💥SC恐慌抛售')
if idx_w_st: _w_signals.append('✅ST二次测试')
if idx_w_ut: _w_signals.append('⚠️UT突破陷阱')
if idx_w_ar: _w_signals.append('↗️AR自动反弹')
_w_signal_text = ' '.join(_w_signals) if _w_signals else '无子信号'
_wyckoff_text = f'{idx_w_phase} {_w_signal_text}'

# 量价状态
_cmf_color = '#22c55e' if idx_cmf_v > 0.05 else '#ef4444' if idx_cmf_v < -0.05 else '#eab308'
_cmf_label = '流入' if idx_cmf_v > 0.05 else '流出' if idx_cmf_v < -0.05 else '中性'
_obv_label = {'bullish':'看多','bearish':'看空','neutral':'中性'}.get(idx_vol_obv, idx_vol_obv)
_ad_label = {'bullish':'积累','bearish':'派发','neutral':'中性'}.get(idx_vol_ad_t, idx_vol_ad_t)
_volume_text = f'CMF={idx_cmf_v:.2f} ({_cmf_label}) | 能量潮={_obv_label} | {_ad_label}'

# RSI 状态
_rsi_color = '#ef4444' if idx_rsi_v < 30 else '#f97316' if idx_rsi_v < 40 else '#94a3b8' if idx_rsi_v < 60 else '#22c55e' if idx_rsi_v < 70 else '#ef4444'
_rsi_label = '超卖' if idx_rsi_v < 30 else '偏低' if idx_rsi_v < 40 else '中性' if idx_rsi_v < 60 else '偏高' if idx_rsi_v < 70 else '超买'
_rsi_text = f'{idx_rsi_v:.1f} {_rsi_label} | 距EMA {idx_dist_v:+.1f}%'

# 位置状态 (箱体判断基于 SMA20)
if idx_last_close > 0:
    box_top = idx_last_close * 1.02  # 箱顶:当前价上方 2%
    box_bot = idx_last_close * 0.98  # 箱底:当前价下方 2%
    if idx_dist_v > 1:
        pos_label = f'在箱体上方, 距箱顶 {box_top:.0f}'
        pos_color = '#fbbf24'
    elif idx_dist_v < -3:
        pos_label = f'跌破箱底 {box_bot:.0f}, 距 {abs(idx_dist_v):.1f}%'
        pos_color = '#ef4444'
    else:
        pos_label = f'箱体中位 ({box_bot:.0f}-{box_top:.0f})'
        pos_color = '#94a3b8'
else:
    pos_label = '数据缺失'
    pos_color = '#94a3b8'

# 短期预测 (基于风险等级 + 当前价)
# 高风险: 跳空大跌 60% / 弱反弹 30% / 横盘 10%
# 中等风险: 惯性下探 55% / 跳空破位 30% / 横盘 15%
# 低风险: 向上突破 50% / 高位震荡 30% / 回踩 20%
cur = idx_last_close
if cur > 0:
    if risk_level.startswith('🔴'):
        scen1_pct, scen1_dir = 60, '再下探' if idx_dist_v < -3 else '弱反弹'
        scen1_range = f'{cur*0.95:.0f}-{cur*0.97:.0f}'
        scen2_pct, scen2_dir, scen2_range = 30, '弱反弹', f'{cur:.0f}-{cur*1.02:.0f}'
        scen3_pct, scen3_dir, scen3_range = 10, '横盘震荡', f'{cur*0.98:.0f}-{cur*1.01:.0f}'
    elif risk_level.startswith('🟢'):
        scen1_pct, scen1_dir, scen1_range = 50, '向上突破', f'{cur*1.03:.0f}-{cur*1.05:.0f}'
        scen2_pct, scen2_dir, scen2_range = 30, '高位震荡', f'{cur*0.98:.0f}-{cur*1.02:.0f}'
        scen3_pct, scen3_dir, scen3_range = 20, '技术回踩', f'{cur*0.96:.0f}-{cur*0.99:.0f}'
    else:  # 🟡 中等
        scen1_pct, scen1_dir, scen1_range = 55, '惯性下探', f'{cur*0.97:.0f}-{cur*0.99:.0f}'
        scen2_pct, scen2_dir, scen2_range = 30, '技术反弹', f'{cur*1.01:.0f}-{cur*1.03:.0f}'
        scen3_pct, scen3_dir, scen3_range = 15, '横盘震荡', f'{cur*0.98:.0f}-{cur*1.01:.0f}'
else:
    scen1_pct = scen2_pct = scen3_pct = 0
    scen1_dir = scen2_dir = scen3_dir = scen1_range = scen2_range = scen3_range = '—'

# 风险建议文字
if risk_level.startswith('🔴'):
    action_hint = '恐慌中避免接刀, 等成交量萎缩+底分型确认再加仓'
elif risk_level.startswith('🟢'):
    action_hint = '趋势向好, 回调不破均线可加仓, 突破前高可追'
else:
    action_hint = '中等风险震荡, 高抛低吸为主, 等放量突破箱体再加仓'

# 动态总仓位建议 (根据风险等级 + 用户实际持仓)
# 跟 risk_summary 里的总仓位建议保持一致 (高: ≤20% / 中: ≤30% / 低: 按规则)
if risk_level.startswith('🔴'):
    cap_str = '≤20%'
elif risk_level.startswith('🟢'):
    cap_str = '按仓位规则'
else:
    cap_str = '≤30%'

# 从 USER_POSITIONS 动态生成持仓建议 (只列当前持仓的 5 只)
_position_hints = []
for code, pos_pct in USER_POSITIONS.items():
    # rows 里找这只 ETF
    r_match = next((r for r in rows if r['c'] == code), None)
    if r_match is None:
        r_match = next((r for r in rows if r['c'].startswith(code[:4])), None)
    if r_match:
        w_letter = r_match.get('w_letter', '?')
        # emoji（全局映射，覆盖全部ETF池）
        emoji = ETF_EMOJI.get(code, '📈')
        short_name = r_match['n'].replace('ETF', '').replace('ETF嘉实', '').replace('ETF银华', '').replace('ETF国泰', '').replace('ETF华夏', '').replace('ETF易方达', '').replace('ETF广发', '').replace('ETF富国', '').replace('ETF南方', '')[:4]
        # 根据 V5 阶段给建议
        if w_letter == 'B':
            action = f'已仓 {pos_pct:.0f}% 加仓'
        elif w_letter in ('A', 'A→B'):
            action = f'已仓 {pos_pct:.0f}% 持有'
        elif w_letter == 'C→A':
            action = f'已仓 {pos_pct:.0f}% 观察'
        elif w_letter == 'C':
            action = f'已仓 {pos_pct:.0f}% 减仓'
        elif w_letter == 'D':
            action = f'已仓 {pos_pct:.0f}% 规避'
        else:
            action = f'已仓 {pos_pct:.0f}% 观望'
        _position_hints.append(f'{emoji} {short_name} {action}')

# 风控门槛文字 (基于风险等级 + 大盘阶段)
if risk_level.startswith('🔴'):
    risk_gate = '等 C期<60% + 道氏转双牛 + 风控转🟢 三步确认'
elif risk_level.startswith('🟢'):
    risk_gate = '可正常仓位, 突破前高加仓, 跌破均线减仓'
else:
    risk_gate = '等 道氏转双牛 + 风控转🟢 + 突破箱顶 三步确认'

# HTML
# ═══ 构造用户持仓概览卡 (避免嵌套 f-string) ═══
_pos_cards = []
for code, pos_pct in USER_POSITIONS.items():
    match = next((r for r in rows if r['c'] == code), None)
    if match is None:
        # 尝试前缀匹配 (机器人ETF两个不同代码)
        match = next((r for r in rows if r['c'].startswith(code[:4])), None)
    if match:
        w_letter = match.get('w_letter', '?')
        if w_letter in ('B', 'A', 'A→B'):
            border_color = '#22c55e'
            phase_text = f'V5阶段:{w_letter} (积极)'
        elif w_letter == 'C→A':
            border_color = '#fbbf24'
            phase_text = f'V5阶段:{w_letter} (过渡)'
        elif w_letter in ('C', 'D'):
            border_color = '#ef4444'
            phase_text = f'V5阶段:{w_letter} (消极)'
        else:
            border_color = '#94a3b8'
            phase_text = f'V5阶段:{w_letter}'

        score_str = f"{match.get('sg', '?')}{match.get('sc', '?')}"
        lm = match.get('lm_result')
        lm_text = ''
        if lm:
            lm_act = lm.get('pyramid_action', '—')
            lm_disp = {
                'add_tier1': 'LM:+1档', 'add_tier2': 'LM:+2档', 'add_tier3': 'LM:+3档',
                'hold': 'LM:持有', 'trim': 'LM:减仓', 'exit': 'LM:清仓',
            }.get(lm_act, f'LM:{lm_act}')
            lm_six = lm.get('six_passed_count', 0)
            lm_text = f' | {lm_disp} ({lm_six}/7)'

        action_color = '#22c55e' if w_letter in ('B', 'A', 'A→B') else '#ef4444' if w_letter in ('C', 'D') else '#fbbf24'
        pos_pct_str = f'{pos_pct:.0f}'
        _pos_cards.append(
            f'<div style="background:#0f172a;border-radius:8px;padding:10px;border-left:3px solid {border_color}">'
            f'<div style="color:#fbbf24;font-weight:700;font-size:16px">{pos_pct_str}%</div>'
            f'<div style="font-size:13px;font-weight:600;margin-top:2px">{match["n"]}</div>'
            f'<div style="color:#94a3b8;font-size:10px;margin-top:2px">{match["c"]}</div>'
            f'<div style="margin-top:6px;font-size:10px;line-height:1.5;color:{action_color}">{phase_text} | 评分{score_str}{lm_text}</div>'
            f'</div>'
        )
    else:
        pos_pct_str = f'{pos_pct:.0f}'
        _pos_cards.append(
            f'<div style="background:#0f172a;border-radius:8px;padding:10px;border-left:3px solid #94a3b8">'
            f'<div style="color:#fbbf24;font-weight:700;font-size:16px">{pos_pct_str}%</div>'
            f'<div style="font-size:13px;font-weight:600;margin-top:2px">{code}</div>'
            f'<div style="color:#ef4444;font-size:10px;margin-top:2px">⚠️ 今日未扫描</div>'
            f'</div>'
        )

total_pos_str = f'{TOTAL_POSITION_PCT:.0f}'
USER_POSITIONS_CARD = (
    f'<div style="background:linear-gradient(135deg,#1e293b,#0f172a);border:2px solid #f59e0b;border-radius:12px;padding:14px 18px;margin-bottom:14px">'
    f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">'
    f'<div style="font-size:15px;font-weight:700;color:#f59e0b">💼 你的持仓概览</div>'
    f'<div style="font-size:10px;color:#94a3b8;background:#0f172a60;padding:2px 8px;border-radius:4px">总仓位 {total_pos_str}%</div>'
    f'</div>'
    f'<div style="display:grid;grid-template-columns:repeat({len(_pos_cards)},1fr);gap:8px;font-size:11px">'
    + ''.join(_pos_cards)
    + '</div></div>'
)

# 使用固定 UTC+8 时区 (中国标准时间, 不受 Windows 夏令时误判影响)
now = datetime.now(beijing_tz).strftime('%Y-%m-%d %H:%M')
h = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>V5 ETF扫描 {now[:10]}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC",sans-serif;max-width:1400px;margin:0 auto;padding:20px;background:#0f172a;color:#e2e8f0;font-size:12px}}
h1{{font-size:20px;margin-bottom:2px}}.sub{{color:#94a3b8;font-size:12px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:10px;overflow:hidden}}
th{{background:#334155;padding:8px 5px;text-align:center;font-weight:600;color:#94a3b8;font-size:11px}}
td{{padding:6px 5px;text-align:center;border-bottom:1px solid #1e293b;font-size:11px}}
tr:hover{{background:#2d3a50}}
.grn{{color:#34d399}}.red{{color:#f87171}}.yel{{color:#fbbf24}}.gry{{color:#94a3b8}}.wht{{color:#e2e8f0}}
.bg{{background:#064e3b;color:#34d399;padding:2px 6px;border-radius:8px;font-size:10px}}
.br{{background:#7f1d1d;color:#f87171;padding:2px 6px;border-radius:8px;font-size:10px}}
.card{{background:#1e293b;border-radius:10px;padding:14px;margin-top:14px}}
.card h3{{font-size:13px;margin:0 0 6px 0}}
.footer{{text-align:center;color:#64748b;font-size:10px;margin-top:16px;padding-top:12px;border-top:1px solid #334155}}
.detail-row{{display:none;background:#0f172a}}
.detail-row td{{text-align:left;padding:10px 16px;line-height:1.6;font-size:11px;border-bottom:2px solid #1e293b}}
.detail-grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}
.detail-box{{background:#1e293b;border-radius:6px;padding:8px 10px}}
.detail-box h4{{color:#94a3b8;font-size:10px;margin:0 0 3px 0;text-transform:uppercase;letter-spacing:0.5px}}
.detail-box .val{{font-size:11px;line-height:1.5}}
.clickable{{cursor:pointer}}
</style>
<script>
function toggle(id){{var e=document.getElementById('d_'+id);e.style.display=e.style.display=='table-row'?'none':'table-row'}}
</script></head><body>
<h1>🎫 V5 三票制 · ETF 全板块扫描 — {now}</h1>
<div class="sub">{len(etfs)-1}只ETF+上证指数 | 点击板块名展开详情</div>
{market_risk_html}

<!-- 大盘预测与总仓位建议 -->
<div style="background:linear-gradient(135deg,#1e293b,#334155);border-radius:10px;padding:14px 18px;margin-bottom:14px;border:1px solid #475569">
<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
<div style="font-size:15px;font-weight:700;color:#f1f5f9">📉 大盘预测 &amp; 总仓位建议</div>
<div style="font-size:10px;color:#94a3b8;background:#0f172a60;padding:2px 8px;border-radius:4px">基于五引擎综合分析</div>
</div>

<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">

<!-- 左列：引擎诊断 -->
<div style="background:#0f172a60;border-radius:6px;padding:10px">
<div style="font-size:10px;color:#94a3b8;margin-bottom:5px;text-transform:uppercase">🔍 大盘引擎诊断</div>
<table style="width:100%;font-size:11px;border-collapse:collapse">
<tr><td style="padding:2px 4px;color:#94a3b8;width:55px">道氏</td>
<td style="padding:2px 4px"><span style="color:{_dow_color}">{_dow_text}</span></td></tr>
<tr><td style="padding:2px 4px;color:#94a3b8">威科夫</td>
<td style="padding:2px 4px"><span style="color:{_w_color}">{_wyckoff_text}</span></td></tr>
<tr><td style="padding:2px 4px;color:#94a3b8">量价</td>
<td style="padding:2px 4px"><span style="color:{_cmf_color}">{_volume_text}</span></td></tr>
<tr><td style="padding:2px 4px;color:#94a3b8">RSI</td>
<td style="padding:2px 4px"><span style="color:{_rsi_color}">{_rsi_text}</span></td></tr>
<tr><td style="padding:2px 4px;color:#94a3b8">位置</td>
<td style="padding:2px 4px"><span style="color:{pos_color}">{pos_label}</span></td></tr>
</table>
</div>

<!-- 右列：短期预测 -->
<div style="background:#0f172a60;border-radius:6px;padding:10px">
<div style="font-size:10px;color:#94a3b8;margin-bottom:5px;text-transform:uppercase">🔮 短期预测 (3-5天)</div>
<div style="font-size:11px;line-height:1.6">
<span style="color:#f97316">┌─ {scen1_pct}%</span> {scen1_dir} → {scen1_range}<br>
<span style="color:#ef4444">├─ {scen2_pct}%</span> {scen2_dir} → {scen2_range}<br>
<span style="color:#94a3b8">└─ {scen3_pct}%</span> {scen3_dir} → {scen3_range}
</div>
<div style="font-size:10px;color:#94a3b8;margin-top:6px;padding-top:6px;border-top:1px solid #334155">
⚠️ {action_hint}
</div>
</div>
</div>

<!-- 仓位建议条 -->
<div style="display:flex;align-items:center;gap:12px;background:#0f172a60;border-radius:6px;padding:8px 12px">
<div style="font-size:12px;font-weight:700;color:{risk_color};white-space:nowrap">💼 总仓位 {cap_str}</div>
<div style="flex:1;font-size:11px;color:#cbd5e1">
{(' · '.join(_position_hints)) if _position_hints else '未读取到持仓 JSON'}
</div>
<div style="font-size:10px;color:#94a3b8;text-align:right;white-space:nowrap">
⏳ {risk_gate}
</div>
</div>
</div>

<table>
<thead><tr>
<th>ETF</th><th>现价</th><th>涨跌%</th><th>距EMA%</th>
<th>①PA</th><th>②威科夫</th>
<th>ATR止损</th><th>RSI</th><th>量(亿)</th>
</tr></thead><tbody>'''

# ═══ ETF 详情卡片渲染循环 ═══

for i, r in enumerate(rows):
    # Main row
    chg_color = 'red' if r['chg_v'] < 0 else 'grn'
    # PA detail parsing
    pa_parts = r['pai'].split('|') if r['pai'] else []
    pa_html = ''
    if len(pa_parts) >= 8 and r['pk'] == 'bottom':
        pa_html = f'<div class="detail-box"><h4>① PA底分型</h4><div class="val">左({pa_parts[0]}) L={pa_parts[3]} → 中({pa_parts[1]}) L={pa_parts[4]} → 右({pa_parts[2]}) L={pa_parts[5]}<br>右收={pa_parts[6]} | {pa_parts[7]} | {pa_parts[8]}</div></div>'
    elif len(pa_parts) >= 7 and r['pk'] == 'top':
        pa_html = f'<div class="detail-box"><h4>① PA顶分型</h4><div class="val">左({pa_parts[0]}) H={pa_parts[3]} → 中({pa_parts[1]}) H={pa_parts[4]} → 右({pa_parts[2]}) H={pa_parts[5]}<br>{pa_parts[6]}</div></div>'

    # VPA detail
    vpa_html = f'<div class="detail-box"><h4>② VPA量价</h4><div class="val">{r["vpi"]}</div></div>'

    # v3.3: 溢价块已删除（用户"溢价也可以删掉了"）
    advice_html = f'<div class="detail-box"><h4>📋 操作建议</h4><div class="val">{r["adv"]}</div></div>'

    # stolgo 详情卡片
    sto_pat = r['sto_pat']
    sto_bo = r['sto_bo']
    
    # Ensure stolgo HTML renders correctly
    has_stolgo_pat = sto_pat != '无' and sto_pat.strip() != ''
    sto_icon = '🔥' if 'bullish' in sto_pat else ('⚠️' if 'bearish' in sto_pat else '')
    sto_bo_icon = '🚀' if '向上突破' in sto_bo else ('💀' if '向下破位' in sto_bo else '')
    
    stolgo_html = f'''<div class="detail-box" style="grid-column:1/-1">
<h4>🔬 stolgo PA 分析</h4>
<div class="val" style="display:flex;gap:16px;flex-wrap:wrap">
<div title="多头吞没=阳线包裹前阴线(看涨)，空头吞没=阴线包裹前阳线(看跌)，锤子线=长下影小实体(底部反转)"><b>K线形态:</b> {sto_icon} <span class="{'grn' if 'bullish' in sto_pat else 'red' if 'bearish' in sto_pat else ''}">{sto_pat}</span></div>
<div title="近7日箱体突破检测。向上突破=脱离盘整区上行，向下破位=跌破盘整区下行"><b>突破:</b> {sto_bo_icon} {sto_bo}</div>
<div>{'📦 盘整中' if r['sto_consol'] else ''}</div>
<div style="width:100%;color:#94a3b8;font-size:10px">{r['sto_sum']}</div>
</div></div>'''

    # ═══ SMC 市场结构卡片（精简版）═══
    smc_break = ('↑破前高' if r['smc_phl_bh'] else '') + (' ↓破前低' if r['smc_phl_bl'] else '')
    smc_html = f'''<div class="detail-box" style="grid-column:1/-1">
<h4>🧠 SMC 市场结构</h4>
<div class="val" style="display:flex;gap:16px;flex-wrap:wrap">
<div><b>前H/L(周):</b> H={r['smc_phl_h']} L={r['smc_phl_l']}{smc_break}</div>
<div>{r['smc_sum']}</div>
</div></div>'''

    # ═══ Wyckoff 威科夫分析(精简) ═══
    phase_icons = {'A':'📉','A→B':'📈','B':'📈','C':'📊','C→A':'🔄','D':'📉'}
    pi = phase_icons.get(r['w_letter'], '❓')
    w_signals = []
    if r['w_spring_det']: w_signals.append(f'🔄 Spring({r["w_spring_s"]:.0%})')
    if r['w_sos_det']:    w_signals.append(f'🚀 SOS({r["w_sos_s"]:.0%})')
    if r['w_lps_det']:    w_signals.append(f'🎯 LPS({r["w_lps_s"]:.0%})')
    w_sc_desc = '💥SC恐慌抛售 ' if r['w_sc'] else ''
    w_st_desc = '✅ST二次测试 ' if r['w_st'] else ''
    w_ut_desc = '⚠️UT突破陷阱' if r['w_ut'] else ''
    w_ar_desc = '↗️AR自动反弹' if r.get('w_ar', False) else ''
    w_sub = (w_sc_desc + w_st_desc + w_ut_desc + w_ar_desc).strip()
    w_sig = ' '.join(w_signals) if w_signals else '无明确信号'

    wyckoff_html = f'''<div class="detail-box" style="grid-column:1/-1">
<h4>📖 威科夫操盘法</h4>
<div class="val" style="display:flex;gap:12px;flex-wrap:wrap;font-size:11px">
<div><b>阶段:</b> {pi} <span class="{'grn' if r['w_letter'] in ('A','B','A→B') else 'yel' if r['w_letter'] == 'C→A' else 'red'}">{r['w_phase']}</span></div>
<div>{w_sig}</div>
<div class="gry" style="font-size:10px">{w_sub}</div>
</div></div>'''

    # ═══ Volume 量价卡片 (精简版) ═══
    _sig_cn = {'bullish':'看多','bearish':'看空','neutral':'中性','overbought':'过热','oversold':'超卖'}
    _div_cn = {'bullish_div':'底背离','bearish_div':'顶背离'}
    _ad_cn  = {'bullish':'积累','bearish':'派发','neutral':'中性'}

    def _vfmt(raw, label):
        if not raw: return f'{label}=—'
        p = raw.split('|', 1)
        sig = _sig_cn.get(p[1], '') if len(p) > 1 else ''
        return f'{label}={p[0]}' + (f'({sig})' if sig else '')

    vol_cmf_str = _vfmt(r['vol_cmf'], '量价流')
    vol_mfi_str = _vfmt(r['vol_mfi'], '资金流量')
    vol_obv_str = f'能量潮={_sig_cn.get(r["vol_obv"], r["vol_obv"])}'
    if r['vol_obv_div'] != 'none':
        vol_obv_str += f'({_div_cn.get(r["vol_obv_div"],"")})'
    vol_ad_str = f'积累={_ad_cn.get(r["vol_ad_t"], r["vol_ad_t"])}' if r['vol_ad_t'] else '积累=—'
    vol_vwap_str = f'成本线={r["vol_vwap"]}'
    if r['vol_vwap_dist'] is not None:
        cls = 'grn' if r['vol_vwap_dist'] > 0 else 'red'
        vol_vwap_str += f'<span class="{cls}">({r["vol_vwap_dist"]:+.1f}%)</span>'

    # 位置+量价综合解读
    w_letter_v = r['w_letter']
    ad_trend = r['vol_ad_t']
    cmf_raw_v = r.get('vol_cmf', '')
    cmf_v = 0.0
    if cmf_raw_v:
        try: cmf_v = float(cmf_raw_v.split('|')[0])
        except: pass
    
    pos_parts = []
    if w_letter_v == 'B' and ad_trend == 'bullish' and cmf_v > 0.1:
        pos_parts.append('✅ B期+量价流入+AD积累 → 主力吸筹确认')
    elif w_letter_v == 'B' and ad_trend == 'bearish':
        pos_parts.append('⚠️ B期但AD派发 → 量价背离，警惕诱多')
    elif w_letter_v == 'B' and ad_trend == 'bullish':
        pos_parts.append('🟡 B期+AD积累但量能中性 → 等放量确认')
    elif w_letter_v in ('A', 'A→B') and ad_trend == 'bullish':
        pos_parts.append('✅ A期+AD积累 → 底部吸筹特征')
    elif w_letter_v in ('A', 'A→B') and ad_trend == 'bearish':
        pos_parts.append('⚠️ A期但AD派发 → 吸筹未完成，等积累信号')
    elif w_letter_v in ('A', 'A→B') and cmf_v < -0.05:
        pos_parts.append('⚠️ A期但量价流出 → 底部未确认')
    elif w_letter_v == 'C' and ad_trend == 'bearish':
        pos_parts.append('🔴 C期+AD派发 → 主力出货中')
    elif w_letter_v == 'C' and cmf_v > 0.1:
        pos_parts.append('🔴 C期+量价流入 → 可能是拉高出货')
    elif w_letter_v == 'D':
        pos_parts.append('🔴 D-下跌期，量价无支撑')
    
    if r.get('w_sc') and w_letter_v == 'C':
        pos_parts.append('💥 SC恐慌抛售触发，等ST缩量止跌')
    if r.get('w_st'):
        pos_parts.append('✅ ST二次测试通过，底部确认')

    # 🪤 埋伏信号（缩量下跌→地量止跌，V型反转前的低吸窗口）
    # 与主表徽章一致: 站上SMA20(右侧启动)后, 埋伏窗口关闭, 显示"已启动"而非"埋伏"
    if r.get('ambush_active'):
        pos_parts.append(f'🪤 埋伏:{r["ambush_stage"]}(左侧窗口)')
    elif r.get('ambush_stage'):
        pos_parts.append(f'➡️ 埋伏窗口关闭(已站上SMA20)')
    
    pos_comment = ' · '.join(pos_parts) if pos_parts else ''
    
    vol_html = f'''<div class="detail-box" style="grid-column:1/-1">
<h4>📊 量价分析 <span class="gry" style="font-size:9px">VPA:今日量={r.get('vpi','')[:30]}</span></h4>
<div class="val" style="display:flex;gap:12px;flex-wrap:wrap;font-size:11px">
<div><b>{vol_cmf_str}</b></div>
<div><b>{vol_mfi_str}</b></div>
<div><b>{vol_obv_str}</b></div>
<div><b>{vol_ad_str}</b></div>
<div><b>{vol_vwap_str}</b></div>
</div>'''
    if pos_comment:
        vol_html += f'<div style="margin-top:6px;font-size:11px;color:#94a3b8;border-top:1px solid #334155;padding-top:5px">{pos_comment}</div>'
    vol_html += '</div>'''

    # ═══ 利弗莫尔持仓手册卡 ═══
    lm_card_html = ''
    user_pos = USER_POSITIONS.get(r['c'], 0)
    if 'lm_result' in r and r['lm_result']:
        lm = r['lm_result']
        lm_trend_disp = {'uptrend': ('📈上升', '#22c55e'), 'downtrend': ('📉下降', '#ef4444'), 'sideways': ('➡️震荡', '#fbbf24')}.get(lm['trend'], ('—', '#94a3b8'))
        lm_pph = lm.get('last_pivot_high')
        lm_ppl = lm.get('last_pivot_low')
        lm_pph_str = f"{lm_pph['date']} @{lm_pph['price']:.3f}(强度{lm_pph['strength']})" if lm_pph else '—'
        lm_ppl_str = f"{lm_ppl['date']} @{lm_ppl['price']:.3f}(强度{lm_ppl['strength']})" if lm_ppl else '—'
        lm_break_disp = {'breaking_up':'🔄突破中', 'confirmed_up':'✅已确认', 'breaking_down':'🔄跌破中', 'confirmed_down':'✅已确认', 'failed':'❌假突破', 'none':'—'}.get(lm['breakout_state'], '—')
        lm_action_disp = {
            'add_tier1': ('+1档 (25%)', '#34d399'),
            'add_tier2': ('+2档 (50%)', '#22c55e'),
            'add_tier3': ('+3档 (75%)', '#16a34a'),
            'hold': ('持有', '#94a3b8'),
            'trim': ('减仓', '#fbbf24'),
            'exit': ('清仓', '#ef4444'),
        }.get(lm['pyramid_action'], ('—', '#94a3b8'))
        # 持仓上下文
        if user_pos > 0:
            lm_holding_html = f'<div style="margin-top:6px;padding:6px 10px;background:#1e3a5f;border-radius:6px;font-size:11px"><b>💼 你的持仓:</b> {user_pos:.0f}% · 金字塔建议: <span style="color:{lm_action_disp[1]};font-weight:700">{lm_action_disp[0]}</span> · 止损: {lm["pyramid_stop_price"]:.3f}</div>'
        else:
            lm_holding_html = f'<div style="margin-top:6px;padding:6px 10px;background:#1e293b;border-radius:6px;font-size:11px;color:#94a3b8"><b>💼 未持仓:</b> 金字塔建议: <span style="color:{lm_action_disp[1]};font-weight:700">{lm_action_disp[0]}</span></div>'

        six_q = lm['six_questions']
        q_icons = ''.join([
            f'<span title="Q{i+1}" style="color:{"#22c55e" if v else "#ef4444"}">{"✅" if v else "❌"}</span>'
            for i, (k, v) in enumerate([
                ('最小阻力向上', six_q.get('Q1_resistance_up', False)),
                ('股价>SMA20', six_q.get('Q2_price_above_sma20', False)),
                ('SMA20上扬', six_q.get('Q2_sma20_rising', False)),
                ('近期突破', six_q.get('Q3_recent_breakout', False)),
                ('回撤守住', six_q.get('Q4_natural_retrace_ok', False)),
                ('量价配合', six_q.get('Q5_volume_confirm', False)),
                ('不强于大盘', six_q.get('Q6_not_against_market', False)),
            ])
        ])

        lm_card_html = f'''<div class="detail-box" style="grid-column:1/-1;border-left:3px solid {lm_trend_disp[1]}">
<h4>📕 利弗莫尔持仓手册 <span class="gry" style="font-size:9px">六问:{lm["six_passed_count"]}/7</span></h4>
<div class="val" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:11px">
<div><b>趋势:</b> <span style="color:{lm_trend_disp[1]};font-weight:700">{lm_trend_disp[0]}</span></div>
<div><b>最小阻力线:</b> {lm['resistance_line_state']} (斜率{lm['resistance_line_slope']:+.4f})</div>
<div><b>突破状态:</b> {lm_break_disp} ({lm['breakout_days_ago']}天前)</div>
<div><b>PP-H:</b> {lm_pph_str}</div>
<div><b>PP-L:</b> {lm_ppl_str}</div>
<div><b>当前相对PP-H:</b> {lm['position_pct_above_last_pivot']:+.2f}%</div>
</div>
<div style="margin-top:6px;padding:6px 10px;background:#0f172a;border-radius:6px;font-size:11px">
<b>六问检:</b> {q_icons} <span class="gry" style="font-size:10px">(最小阻力·趋势·突破·回撤·量价·大盘)</span>
</div>
{lm_holding_html}
</div>'''

    # ═══ 道氏趋势分析卡片(精简) ═══
    dow_dir_icons = {'bull':'📈','bear':'📉','range':'➡️'}
    dow_pi = dow_dir_icons.get(r['dow_p_dir'], '❓')
    dow_si = dow_dir_icons.get(r['dow_s_dir'], '❓')
    dow_vol_icon = '✅' if r['dow_vol_ok'] else '⚠️背离'
    dow_html = f'''<div class="detail-box" style="grid-column:1/-1">
<h4>📐 道氏趋势</h4>
<div class="val" style="display:flex;gap:12px;flex-wrap:wrap;font-size:11px">
<div><b>主:</b> {dow_pi} {r['dow_p_dir']}({r['dow_p_str']:.0%})</div>
<div><b>次级:</b> {dow_si} {r['dow_s_dir']}(ADX={r['dow_s_adx']})</div>
<div><b>量价:</b> {dow_vol_icon}</div>
</div></div>'''

    detail_html = f'''<div class="detail-grid">{pa_html}{vpa_html}</div>{stolgo_html}{smc_html}{dow_html}{wyckoff_html}{vol_html}{lm_card_html}'''

    chg_display = f'''<span style="color:#94a3b8;font-size:10px" title="疑似除权/份额拆分">除权</span>''' if r.get('ca') else f'{r["ch"]}%'
    h += f'''<tr onclick="toggle({i})" class="clickable">
<td style="text-align:left;font-weight:600">{r.get('vol_tag','')}{r['n']}<br><span class="gry" style="font-size:10px">{r['c']}</span></td>
<td>{r['p']}</td>
<td class="{chg_color}">{chg_display}</td>
<td class="{r['dc']}">{r['ds']}%</td>
<td><span class="{'bg' if r['pa']=='✅' else 'br'}">{r['pa']}</span></td>
<td><span class="{'bg' if r['w_letter'] in ('B','A','A→B') else 'br'}">{'🟢' if r['w_letter'] in ('B','A','A→B') else '🔴' if r['w_letter'] in ('C','D') else '⚪'}</span></td>
<td class="gry" title="ATR(14)止损: 现价-2×ATR">{r.get('atr_sl','—')}</td>
<td class="{'red' if r['rsi_v']<30 else 'grn' if r['rsi_v']>60 else ''}">{r['rs']}</td>
<td class="gry">{r['vl']}</td>
</tr>
<tr class="detail-row" id="d_{i}"><td colspan="10">{detail_html}</td></tr>'''

# ═══ 汇总建议表 ═══

h += '''</tbody></table>

<div class="card" style="margin-top:16px">
<h3>📋 汇总建议</h3>
''' + USER_POSITIONS_CARD + '''
<table style="background:transparent;font-size:11px;width:100%">
<tr style="color:#94a3b8;font-size:11px"><th style="text-align:left;padding:6px 10px">板块</th><th style="text-align:center;padding:6px 10px;width:50px">PA</th><th style="text-align:center;padding:6px 10px;width:65px">阶段</th><th style="text-align:center;padding:6px 10px;width:50px">道氏</th><th style="text-align:center;padding:6px 10px;width:48px">评分</th><th style="text-align:left;padding:6px 10px">信号</th><th style="text-align:center;padding:6px 10px;width:58px">V5操作</th><th style="text-align:center;padding:6px 10px;width:55px">V5仓位</th><th style="text-align:center;padding:6px 10px;width:65px">LM趋势</th><th style="text-align:center;padding:6px 10px;width:60px">LM六问</th><th style="text-align:center;padding:6px 10px;width:80px">LM金字塔</th></tr>
''' + summary_rows + '''
</table></div>

<div class="footer">⚠️ V5 + 利弗莫尔双引擎基于公开数据自动生成，不构成投资建议。LM列与V5并列展示，独立评估，不合并决策。<br>v5_scan.py + livermore_engine.py | 关键点 / 最小阻力线 / 金字塔加仓 / 利弗莫尔六问</div>
</body></html>'''

out = f'deliverables/trading-agent/etf-full-scan-{now[:10]}.html'
with open(out, 'w', encoding='utf-8') as f:
    f.write(h)
print(f'✅ {out}', file=sys.stderr)
print(out)
