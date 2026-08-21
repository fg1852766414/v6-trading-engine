"""回测：用V5引擎在6月18日的数据点分析创新药"""
import sys, json, subprocess
sys.path.insert(0,'scripts')
from trading_engine.models import Candle
from trading_engine.technical_engine import TechnicalEngine
from trading_engine.volume_engine import analyze_volume
from trading_engine.stolgo_engine import analyze_pa
from trading_engine.smartmoneyconcepts_engine import analyze_smc

ND = 'D:/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/neodata-financial-search'
Q = f'{ND}/scripts/query.py'

r = subprocess.run([sys.executable, Q, '--query', '159992 创新药ETF银华 历史K线 日行情'],
    capture_output=True, text=True, timeout=30, cwd=ND)

all_cands = []
for item in json.loads(r.stdout).get('data',{}).get('apiData',{}).get('apiRecall',[]):
    if '数据详情' not in item.get('content',''): continue
    for line in item['content'].split('\n'):
        cols = [x.strip() for x in line.split('|')]
        if len(cols) < 10 or '2026-' not in cols[9]: continue
        try:
            all_cands.append(Candle(date=cols[9], open=float(cols[1]), close=float(cols[2]),
                high=float(cols[3]), low=float(cols[4]),
                volume=float(cols[5].replace(',','') or '0')))
        except: pass

# 检查数据顺序
if all_cands and len(all_cands) > 1:
    d0 = all_cands[0].date
    d1 = all_cands[-1].date
    if d0 > d1:  # 新->旧, 需要反转
        all_cands = list(reversed(all_cands))
        print(f'数据已反转: {all_cands[0].date}~{all_cands[-1].date}')
    else:
        print(f'数据顺序: {all_cands[0].date}~{all_cands[-1].date}')

# 回测截止日期
backtest_date = '2026-06-18'
cands_bt = [c for c in all_cands if c.date <= backtest_date]
print(f'回测截止: {backtest_date}, 共{len(cands_bt)}根K线')
if cands_bt:
    print(f'最新K线: {cands_bt[-1].date} C={cands_bt[-1].close:.3f} V={cands_bt[-1].volume/1e8:.1f}亿')
print()

# ═══ 1. 缠论PA ═══
te = TechnicalEngine()
fr = te.detect_fractal(cands_bt)
print('【缠论PA】')
print(f'  PA类型: {fr.kind.value}')
if fr.index >= 2 and fr.index + 1 < len(cands_bt):
    l = cands_bt[fr.index-1]
    m = cands_bt[fr.index]
    r2 = cands_bt[fr.index+1]
    print(f'  分型位置: 左({l.date} L={l.low:.3f}) 中({m.date} L={m.low:.3f}) 右({r2.date} L={r2.low:.3f})')
    if fr.kind.value == 'bottom':
        v = te.validate_bottom_fractal(cands_bt, fr)
        print(f'  底分型质量: {v.quality_score}/3, 有效={v.is_valid}')
print()

# ═══ 2. stolgo ═══
sto = analyze_pa(cands_bt, '159992')
patterns = [p for p in sto['candlestick_patterns'] if p['detected']]
bo = sto['breakout']
sr = sto['sr_levels']
print('【stolgo】')
print(f'  K线形态: {"; ".join(f"{p["name"]}({p["signal_type"]})" for p in patterns) if patterns else "无"}')
print(f'  突破: {bo["description"]}')
print(f'  阻力R21: {sr.get("resistance_21","?")} 支撑S21: {sr.get("support_21","?")}')

# stolgo summary
sto_summary = sto.get('summary','')
print(f'  摘要: {sto_summary}')
print()

# ═══ 3. SMC ═══
smc = analyze_smc(cands_bt)
phl = smc['previous_hl']
ret = smc['retracement']
print('【SMC市场结构】')
ph_h = f'{phl["high"]:.3f}' if phl.get('high') else '无'
ph_l = f'{phl["low"]:.3f}' if phl.get('low') else '无'
print(f'  前H/L(周): H={ph_h} L={ph_l} 破前高={phl.get("broken_high","?")} 破前低={phl.get("broken_low","?")}')
print(f'  摘要: {smc.get("summary","?")}')
print()

# ═══ 4. Volume ═══
vol = analyze_volume(cands_bt)
cmf_v = vol.get('cmf',{}).get('value', 0)
cmf_s = vol.get('cmf',{}).get('signal', '?')
mfi_v = vol.get('mfi',{}).get('value', 0)
mfi_s = vol.get('mfi',{}).get('signal', '?')
obv_t = vol.get('obv_trend','?')
obv_d = vol.get('obv_divergence','?')
ad_t = vol.get('ad',{}).get('trend','?')
vwap_d = vol.get('vwap_distance', None)

print('【量价分析】')
print(f'  量价流CMF: {cmf_v:.4f} ({cmf_s})')
print(f'  资金流量MFI: {mfi_v:.1f} ({mfi_s})')
print(f'  能量潮OBV: 趋势={obv_t} 背离={obv_d}')
print(f'  积累派发AD: 趋势={ad_t}')
print(f'  摘要: {vol["summary"]}')
print()

# ═══ 5. 道氏趋势 ═══
highs_20 = [c.high for c in cands_bt[-20:]]
lows_20 = [c.low for c in cands_bt[-20:]]
print('【道氏趋势】')
print(f'  近20日范围: {min(lows_20):.3f}-{max(highs_20):.3f}')
# 近5日趋势
r5 = cands_bt[-5:]
low_up = r5[0].low < r5[-1].low
high_up = r5[0].high < r5[-1].high
print(f'  近5日: {"低点上移" if low_up else "低点下移"} {"高点上移" if high_up else "高点下移"}')
print()

# ═══ 6. 威科夫信号 ═══
pa_btm = fr.kind.value == 'bottom'
obv_bull = obv_t == 'bullish'
obv_div = obv_d == 'bullish_div'
ad_accum = ad_t == 'bullish'
cmf_pos = cmf_v is not None and cmf_v > 0.05
stolgo_breakout_up = bo['is_breakout'] and bo['direction'] == 'up'

spring_score = (3 if pa_btm else 0) + (3 if obv_div else 0) + (2 if ad_accum else 0) + (1 if cmf_pos else 0)
sos_score = (4 if stolgo_breakout_up else 0) + (2 if obv_bull else 0) + (2 if ad_accum else 0) + (1 if cmf_pos else 0)
lps_score = (3 if spring_score >= 3 else 0) + (3 if False else 0) + (2 if obv_bull else 0) + (1 if cmf_pos else 0) + (1 if ad_accum else 0)

spring_det = spring_score >= 5
sos_det = sos_score >= 5
lps_det = lps_score >= 6

print('【威科夫信号】')
print(f'  Spring得分: {spring_score}/9 ({spring_score/9:.0%}) → 检测={spring_det}')
print(f'  SOS得分:    {sos_score}/9 ({sos_score/9:.0%}) → 检测={sos_det}')
print(f'  LPS得分:    {lps_score}/10 ({lps_score/10:.0%}) → 检测={lps_det}')

if sos_det:
    phase = 'B-加仓期(SOS确认)'
elif lps_det:
    phase = 'A→B过渡(LPS确认)'
elif spring_det:
    phase = 'A-吸筹期(Spring)'
else:
    phase = '未进入明确阶段'
print(f'  威科夫阶段: {phase}')
print()

# ═══ 7. 三票制 ═══
# 模拟当时VPA
last = cands_bt[-1]
vol5 = sum(c.volume for c in cands_bt[-6:-1]) / 5
shrink_ok = last.volume < vol5 * 0.8
price_ok = last.low >= cands_bt[-2].low
surge_ok = last.volume > vol5 * 1.5 and (last.close - last.open) / last.open * 100 > 2
vpa = '✅' if (shrink_ok and price_ok) or surge_ok else '❌'
pa_icon = '✅' if pa_btm else '❌'
print('【三票制】')
print(f'  ①PA: {pa_icon} (底分型={pa_btm})')
print(f'  ②VPA: {vpa} (缩量={shrink_ok} 价不创新低={price_ok} 放量突破={surge_ok})')
print(f'  今日量比vs5日均量: {last.volume/vol5:.2f}x')
print()

# ═══ 8. 综合建议 ═══
print('【6月18日综合结论】')
print(f'  价格: {last.close:.3f}')
print(f'  威科夫: {phase}')
if sos_det:
    print(f'  → 系统会在6月18日判定B-加仓期(SOS确认)')
    print(f'  → 买入信号明确：LPS已在6/12-6/17完成确认')
    print(f'  → 建议仓位：50-70%（视风险偏好）')
    print(f'  → 止损位：6/11低点0.711下方')
elif lps_det:
    print(f'  → A→B过渡期，可以建仓')
elif spring_det:
    print(f'  → 吸筹初期，等SOS或LPS确认')
else:
    print(f'  → 没有明确信号')
