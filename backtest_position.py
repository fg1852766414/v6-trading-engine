"""回测：新仓位框架下，各时间点应该建多少仓"""
import sys, json, subprocess
sys.path.insert(0,'scripts')
from trading_engine.models import Candle
from trading_engine.technical_engine import TechnicalEngine
from trading_engine.volume_engine import analyze_volume
from trading_engine.stolgo_engine import analyze_pa

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
if all_cands[0].date > all_cands[-1].date:
    all_cands = list(reversed(all_cands))

def get_position(w_letter):
    mapping = {
        'B': (50, 70, 'B-加仓期'),
        'A→B': (25, 35, 'A→B过渡'),
        'A': (25, 35, 'A-吸筹期'),
        'C→A': (10, 10, 'C→A过渡(观察)'),
        'C': (0, 5, 'C-派发期'),
        'D': (0, 0, 'D-下跌期'),
    }
    return mapping.get(w_letter, (0, 0, '阶段不明'))

def calc_wyckoff(pa_kind, vol, bo, s3, s5):
    pa_btm = pa_kind == 'bottom'
    obv_t = vol.get('obv_trend','?')
    obv_d = vol.get('obv_divergence','?')
    ad_t = vol.get('ad',{}).get('trend','?')
    cmf_v = vol.get('cmf',{}).get('value', 0)
    cmf_pos = cmf_v and cmf_v > 0.05

    spring = (3 if pa_btm else 0) + (3 if obv_d=='bullish_div' else 0) + (2 if ad_t=='bullish' else 0) + (1 if cmf_pos else 0)
    sos = (4 if bo['is_breakout'] and bo['direction']=='up' else 0) + (2 if obv_t=='bullish' else 0) + (2 if ad_t=='bullish' else 0) + (1 if cmf_pos else 0)
    lps = (3 if spring >= 3 else 0) + (3 if s5 else 0) + (2 if obv_t=='bullish' else 0) + (1 if cmf_pos else 0) + (1 if ad_t=='bullish' else 0)
    spring_det = spring >= 5; sos_det = sos >= 5; lps_det = lps >= 6

    if sos_det: phase, letter = 'B-加仓期(SOS确认)', 'B'
    elif lps_det: phase, letter = 'A→B过渡(LPS确认)', 'A→B'
    elif spring_det: phase, letter = 'A-吸筹期(Spring)', 'A'
    else:
        trans = (2 if obv_d=='bullish_div' else 0) + (1 if cmf_pos else 0) + (1 if s5 else 0)
        if pa_kind == 'top' and trans >= 2:
            phase, letter = 'C→A过渡(吸筹初现)', 'C→A'
        elif pa_kind == 'top':
            phase, letter = 'C-派发期(顶分型)', 'C'
        else:
            phase, letter = '阶段不明', '?'
    return phase, letter, spring, sos, lps, spring_det, sos_det, lps_det

# 回测日期序列
test_dates = ['2026-06-12', '2026-06-18', '2026-06-25', '2026-06-30', '2026-07-03', '2026-07-10', '2026-07-15']

print(f'{"日期":12s} {"价格":6s} {"PA":6s} {"CMF":6s} {"OBV":12s} {"AD":8s} {"Spring":6s} {"SOS":6s} {"LPS":6s} {"阶段":20s} {"建议仓位":10s}')
print('─' * 100)

trades = []  # (date, price, action, pos_pct)
last_pos = 0

for bt_date in test_dates:
    cands_bt = [c for c in all_cands if c.date <= bt_date]
    last = cands_bt[-1]
    te = TechnicalEngine()
    fr = te.detect_fractal(cands_bt)
    vol = analyze_volume(cands_bt)
    sto = analyze_pa(cands_bt, '159992')
    bo = sto['breakout']
    # Share data - use the actual share cache
    s3 = s5 = False

    phase, letter, sp, so, lp, sp_d, so_d, lp_d = calc_wyckoff(fr.kind.value, vol, bo, s3, s5)
    pos_min, pos_max, pos_label = get_position(letter)
    avg_pos = (pos_min + pos_max) // 2

    obv_t = vol.get('obv_trend','?')
    obv_d = vol.get('obv_divergence','?')
    ad_t = vol.get('ad',{}).get('trend','?')
    cmf_v = vol.get('cmf',{}).get('value', 0)

    print(f'{bt_date:12s} {last.close:.3f} {fr.kind.value:6s} {cmf_v:.2f} {obv_t:6s}/{obv_d:6s} {ad_t:8s} {sp:2d}/9 {so:2d}/9 {lp:2d}/10 {phase:20s} {pos_label:10s}({pos_min}-{pos_max}%)')

    # Track trade actions relative to previous
    if avg_pos > last_pos:
        trades.append((bt_date, last.close, f'+{avg_pos - last_pos}%', avg_pos))
    elif avg_pos < last_pos:
        trades.append((bt_date, last.close, f'-{last_pos - avg_pos}%', avg_pos))
    last_pos = avg_pos

print()
print('=== 交易信号时间线 ===')
prev_action = ''
for d, p, a, pos in trades:
    print(f'{d} 价={p:.3f} {a} → 总仓位{pos}%')
