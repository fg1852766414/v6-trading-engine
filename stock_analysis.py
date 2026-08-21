#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立个股分析脚本 —— 跑全部引擎分析单只A股"""

import sys
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import sys, json, os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from trading_engine import Candle, TechnicalEngine
from trading_engine.utils import ema
from trading_engine.stolgo_engine import analyze_pa as _analyze_pa
from trading_engine.smartmoneyconcepts_engine import analyze_smc as _analyze_smc
from trading_engine.volume_engine import analyze_volume
from trading_engine.scoring_engine import score_etf
from trading_engine.dow_engine import dow_quick

# ── 配置 ──
CODE = '002230'
NAME = '科大讯飞'
PERIOD = 60  # 取多少根K线

# ── 拉数据（通过AKShare stock_zh_a_daily） ──
import akshare as ak
import pandas as pd

raw = ak.stock_zh_a_daily(symbol='sz002230', adjust='qfq')
recent = raw.tail(PERIOD).copy()
cands = []
for _, row in recent.iterrows():
    try:
        cands.append(Candle(
            date=str(row['date'])[:10],
            open=float(row['open']),
            close=float(row['close']),
            high=float(row['high']),
            low=float(row['low']),
            volume=float(row['volume']),
        ))
    except:
        pass

if len(cands) < 10:
    print(f'❌ 数据不足: {len(cands)}条')
    sys.exit(1)

print(f'📊 {NAME}({CODE}) 近{len(cands)}个交易日分析')
print(f'   数据区间: {cands[0].date} → {cands[-1].date}')
print()

# ── 基础计算 ──
last = cands[-1]; prev = cands[-2]
closes = [c.close for c in cands]
ev = ema(closes, 20)[-1] if len(closes) >= 20 else sum(closes[-20:])/20
chg = (last.close/prev.close - 1) * 100
vol5 = sum(c.volume for c in cands[-6:-1])/5 if len(cands) >= 6 else sum(c.volume for c in cands)/len(cands)
vol_ratio = last.volume / vol5 * 100 if vol5 > 0 else 0
dist = (last.close - ev) / ev * 100 if ev > 0 else 0
rsi_v = 50
if len(closes) >= 15:
    gains = [max(closes[i]-closes[i-1], 0) for i in range(-14, 0)]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(-14, 0)]
    avg_g = sum(gains)/14; avg_l = sum(losses)/14
    rsi_v = 50 if avg_l == 0 else 100 - 100/(1+avg_g/avg_l)

print(f'┌─── 行情概览 ───')
print(f'│ 最新价: {last.close:.3f}')
print(f'│ 涨跌幅: {chg:+.2f}%')
print(f'│ 距EMA20: {dist:+.1f}%')
print(f'│ 成交量: {last.volume/1e8:.2f}亿股 (5日均量{vol5/1e8:.2f}亿的{vol_ratio:.0f}%)')
print(f'│ RSI(14): {rsi_v:.1f}')
print(f'└──────────────')
print()

# ═══ 1. 缠论 PA ═══
te = TechnicalEngine()
fr = te.detect_fractal(cands)
pa_kind = fr.kind.value
pa_valid = False
pa_info = ''
if fr.index >= 2:
    l, m, r2 = cands[fr.index-1], cands[fr.index], cands[fr.index+1]
    if pa_kind == 'bottom':
        pa_valid = te.validate_bottom_fractal(cands, fr)
        pa_info = f'底分型: L={m.low:.3f} 有效={pa_valid}'
    else:
        pa_valid = te.validate_top_fractal(cands, fr)
        pa_info = f'顶分型: H={m.high:.3f} 有效={pa_valid}'
else:
    pa_info = '无法定位分型'
pa_icon = '✅' if pa_kind == 'bottom' and pa_valid else '❌'

print(f'┌─── ① 缠论PA ───')
print(f'│ {pa_info}')
print(f'└──────────────')
print()

# ═══ 2. stolgo ═══
stolgo_result = _analyze_pa(cands, CODE)
patterns = [p for p in stolgo_result['candlestick_patterns'] if p['detected']]
pattern_str = '; '.join(f'{p["name"]}({p["signal_type"]})' for p in patterns) if patterns else '无'
bo = stolgo_result['breakout']
sr = stolgo_result['sr_levels']

print(f'┌─── ② stolgo 形态分析 ───')
print(f'│ K线形态: {pattern_str}')
if bo['is_breakout']:
    print(f'│ 箱体突破: {"突破↑" if bo["direction"]=="up" else "破位↓"} 强度{bo["strength"]:.0%}')
else:
    print(f'│ 箱体: 盘整中')
print(f'│ 阻力(R21): {sr.get("resistance_21",0):.3f}  支撑(S21): {sr.get("support_21",0):.3f}')
print(f'└──────────────')
print()

# ═══ 3. SMC ═══
smc = _analyze_smc(cands)
smc_phl = smc.get('weekly_phl', {})
smc_ret = smc.get('retracement', {})
smc_high = smc_phl.get('high', 0)
smc_low = smc_phl.get('low', 0)
broken_high = smc_phl.get('broken_high', False)
broken_low = smc_phl.get('broken_low', False)
ret_pct = smc_ret.get('current_pct', 0)

print(f'┌─── ③ SMC 市场结构 ───')
print(f'│ 前H/L(周): H={smc_high:.3f} L={smc_low:.3f}')
if broken_high:
    print(f'│ ↑破前高')
if broken_low:
    print(f'│ ↓破前低')
print(f'│ 回撤: {ret_pct:.0f}%')
print(f'└──────────────')
print()

# ═══ 4. Volume ═══
vol = analyze_volume(cands)
cmf = vol['cmf']
mfi = vol['mfi']
obv = vol['obv_trend']
obv_div = vol['obv_divergence']
ad = vol['ad']
vwap = vol['vwap']
vwap_dist = vol['vwap_dist_pct']

print(f'┌─── ④ 量价分析 ───')
print(f'│ CMF: {cmf.get("value",0):+.3f} ({cmf.get("signal","中性")})')
print(f'│ MFI: {mfi.get("value",50):.0f} ({mfi.get("signal","中性")})')
print(f'│ OBV趋势: {obv}  背离: {obv_div}')
print(f'│ A/D积累: {ad.get("trend","中性")}')
print(f'│ VWAP: {vwap:.3f} (距vwap: {vwap_dist:+.1f}%)')
print(f'└──────────────')
print()

# ═══ 5. 道氏趋势 ═══
dow = dow_quick(cands)
p_dir = dow['primary']['direction']
p_str = dow['primary']['strength']
s_dir = dow['secondary']['direction']
s_adx = dow['secondary'].get('adx', 0)
vol_ok = dow.get('volume_confirms', False)
dow_icon_p = {'bull':'📈','bear':'📉','range':'➡️'}.get(p_dir, '')
dow_icon_s = {'bull':'📈','bear':'📉','range':'➡️'}.get(s_dir, '')

print(f'┌─── ⑤ 道氏趋势 ───')
print(f'│ 主趋势: {dow_icon_p} {p_dir}({p_str})')
print(f'│ 次级趋势: {dow_icon_s} {s_dir}(ADX={s_adx:.1f})')
print(f'│ 量价验证: {"✅" if vol_ok else "❌"}')
print(f'└──────────────')
print()

# ═══ 6. 威科夫（内联直译） ═══
ad_accumulating = ad['trend'] == 'bullish'
ad_distributing = ad['trend'] == 'bearish'
cmf_val = cmf.get('value', 0)
cmf_pos = cmf_val > 0.05
obv_bullish = obv == 'bullish'
obv_bullish_div = obv_div == 'bullish_div'
is_down = chg < 0

# Spring检测
spring_score = 3 if pa_kind == 'bottom' else 0
if obv_bullish_div: spring_score += 3
if ad_accumulating: spring_score += 2
if cmf_pos: spring_score += 1
spring_detected = spring_score >= 5

# SOS检测
sos_score = 0
if bo.get('is_breakout') and bo.get('direction') == 'up':
    sos_score += 4
if obv_bullish: sos_score += 2
if ad_accumulating: sos_score += 2
if cmf_pos: sos_score += 1
sos_detected = sos_score >= 5

# LPS检测
lps_score = 3 if spring_detected and spring_score >= 3 else 0
lps_detected = lps_score >= 6

# SC检测
sc_detected = rsi_v < 30 and is_down
sc_label = '💥SC恐慌抛售触发' if sc_detected else ''

# 阶段判定
if sos_detected and sos_score >= 5:
    w_phase = 'B-加仓期(SOS确认)'
    w_letter = 'B'
elif lps_detected and lps_score >= 6:
    w_phase = 'A→B过渡(LPS确认)'
    w_letter = 'A→B'
elif spring_detected:
    w_phase = 'A-吸筹期(Spring)'
    w_letter = 'A'
elif pa_kind == 'top':
    ts = (1 if obv_bullish_div else 0) + (1 if cmf_pos else 0) + (1 if False else 0)
    if ts >= 2:
        w_phase = 'C→A过渡(吸筹初现)'
        w_letter = 'C→A'
    else:
        w_phase = 'C-派发期(顶分型)'
        w_letter = 'C'
else:
    w_phase = '阶段不明'
    w_letter = '?'

w_sig = ''
if sos_detected: w_sig += 'SOS✓ '
if lps_detected: w_sig += 'LPS✓ '
if spring_detected: w_sig += 'Spring✓ '
if sc_detected: w_sig += 'SC💥 '

print(f'┌─── ⑥ 威科夫操盘法 ───')
print(f'│ 阶段: {w_phase}')
if w_sig:
    print(f'│ 子信号: {w_sig}')
if sc_label:
    print(f'│ {sc_label}')
print(f'└──────────────')
print()

# ═══ 7. 综合评分 ═══
row_data = dict(
    n=NAME, c=CODE, p=f'{last.close:.3f}', ch=f'{chg:+.2f}',
    dist=f'{dist:+.1f}', pa=pa_icon, s3=False, s5=False,
    rsi_v=rsi_v, vol=f'{last.volume/1e8:.1f}',
    w_letter=w_letter, w_phase=w_phase,
    dow_p_dir=p_dir, dow_p_str=p_str, dow_s_dir=s_dir,
    dow_s_adx=s_adx, dow_vol_ok=vol_ok, dow_sum=dow['summary'],
    cmf_v=cmf_val, cmf_s=cmf.get('signal',''),
    mfi_v=mfi.get('value',50), mfi_s=mfi.get('signal',''),
    obv_v=obv, obv_div=obv_div, ad_trend=ad['trend'],
    vwap_dist=vwap_dist, vol_ratio=vol_ratio,
    smc_high=smc_high, smc_low=smc_low,
    smc_ret_pct=ret_pct, smc_broken_low=broken_low, smc_broken_high=broken_high,
    bo_kind='breakout' if bo.get('is_breakout') else 'none',
    bo_dir=bo.get('direction',''), bo_strength=bo.get('strength',0),
)
score, grade, grade_lbl, _ = score_etf(row_data)

print(f'┌─── ⑦ 综合评分 ───')
print(f'│ 总分: {score} ({grade}级) —— {grade_lbl}')
print(f'└──────────────')
print()

# ═══ 8. 交易建议 ═══
action = '→ 观望'
position = '0-5%'
if w_letter == 'B' and (sos_detected or lps_detected):
    action = '→ 关注买入'; position = '50-70%'
elif w_letter in ('A', 'A→B') and spring_detected:
    action = '→ 关注买入'; position = '25-35%'
elif w_letter in ('A', 'A→B'):
    action = '→ 等威科夫信号'; position = '10%观察'
elif w_letter == 'C→A':
    action = '→ 观察'; position = '10%观察'
elif w_letter == 'D':
    action = '→ 规避'; position = '0%'

# 评分门槛修正
if score < 40 and action == '→ 关注买入':
    action = '→ 评分低,等待'; position = '10%观察'
elif score < 20 and action in ('→ 关注买入', '→ 等威科夫信号'):
    action = '→ 评分极低,规避'; position = '0-5%'

print(f'┌─── ⑧ 操作建议 ───')
print(f'│ 操作: {action}')
print(f'│ 建议仓位: {position}')
print(f'├─── 关键价位 ───')
print(f'│ 当前价: {last.close:.3f}')
print(f'│ 阻力: {sr.get("resistance_21",0):.3f}  支撑: {sr.get("support_21",0):.3f}')
if bo.get('level'):
    print(f'│ 箱体: {bo["level"]:.3f}')
print(f'│ 成本线(VWAP): {vwap:.3f}')
print(f'└──────────────')
print()

# ═══ 9. 综合诊断 ───
print(f'┌─── ⑨ 综合诊断 ───')
issues = []
positives = []
if w_letter in ('B',):
    positives.append('威科夫B-加仓期，趋势上行阶段')
if w_letter in ('A', 'A→B'):
    positives.append(f'威科夫{w_letter}期，底部吸筹阶段')
if pa_kind == 'bottom' and pa_valid:
    positives.append('有效底分型确认，结构支撑')
if cmf_val > 0.05:
    positives.append('CMF为正面，资金流入')
if ad_accumulating:
    positives.append('AD积累为正面，机构吸筹')
if spring_detected:
    positives.append('Spring信号触发，反转确认')
if sos_detected:
    positives.append('SOS强势信号触发')

if w_letter in ('C',):
    issues.append('威科夫C-派发期，主力出货风险')
if w_letter == 'D':
    issues.append('威科夫D-下跌期，规避')
if pa_kind == 'top':
    issues.append('顶分型压制，结构压力')
if cmf_val < -0.05:
    issues.append('CMF为负，资金流出')
if ad_distributing:
    issues.append('AD派发，机构出货')
if rsi_v > 70:
    issues.append(f'RSI {rsi_v:.0f}进入超买区')
if rsi_v < 30:
    issues.append(f'RSI {rsi_v:.0f}超卖')
if broken_low:
    issues.append('SMC破前低，结构走坏')
if dist > 8:
    issues.append(f'距EMA过远({dist:+.0f}%)，注意回调')
if dist < -15:
    issues.append(f'深度偏离EMA({dist:.0f}%)，深度超跌')

for p in positives:
    print(f'  ✅ {p}')
for i in issues:
    print(f'  ⚠️ {i}')
if not positives:
    print(f'  ❌ 无明显正面信号')
if not issues:
    print(f'  ✅ 无明显风险信号')
print(f'└──────────────')
print()

# 一句话总结
concise = ''
if w_letter == 'B':
    concise = f'B-加仓期，如已持仓持有，空仓可等缩量回踩VWAP({vwap:.3f})附近再进。'
elif w_letter in ('A', 'A→B'):
    concise = f'A-吸筹期，底部结构确认中，当前操作等级: {action}，仓位{position}。止损设在支撑{sr.get("support_21",0):.3f}下方。'
elif w_letter == 'C':
    concise = 'C-派发期，主力出货阶段，观望为主。不要抄底。'
elif w_letter == 'D':
    concise = 'D-下跌期，规避为主。'
else:
    concise = '阶段不明，暂时观察。'

print(f'📝 一句话总结:')
print(f'   {concise}')
print()
print(f'⚠️ 以上由AI基于公开数据自动分析生成，仅供参考，不构成投资建议。')
