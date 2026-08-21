#!/usr/bin/env python3
"""Fetch ETF K-lines and output VPA data for LLM analysis."""
import subprocess, json, sys

_VENV_PY = r'C:\Users\Admin\.workbuddy\binaries\python\envs\default\Scripts\python.exe'
ND = 'D:/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/neodata-financial-search'

etfs = [
    ('000001.SH','上证指数'), ('159992','创新药'), ('512880','证券'), ('562500','机器人华夏'),
    ('159611','电力'), ('515880','通信'), ('159865','养殖'), ('512980','传媒'),
    ('512690','酒'), ('512010','医药'), ('159326','电网设备'), ('512710','军工龙头'),
    ('512400','有色'), ('518880','黄金'), ('515030','新能源车'),
]

def parse_candle(cols):
    c = [x.strip().replace(',','') for x in cols]
    if c[1].startswith('2026-'):  # Index format
        return {'d':c[1][5:10], 'o':float(c[3]), 'c':float(c[2]), 'h':float(c[7]), 'l':float(c[8]), 'v':float(c[5] or '0')}
    else:  # ETF format
        return {'d':c[9][5:10], 'o':float(c[1]), 'c':float(c[2]), 'h':float(c[3]), 'l':float(c[4]), 'v':float(c[5] or '0')}

for code, name in etfs:
    r = subprocess.run([_VENV_PY, f'{ND}/scripts/query.py', '--query', f'{code} {name} 历史K线 日行情'],
        capture_output=True, text=True, timeout=30, cwd=ND)
    try:
        data = json.loads(r.stdout)
    except:
        continue
    for item in data['data']['apiData']['apiRecall']:
        raw_lines = [l for l in item.get('content','').split('\n') if '|' in l]
        candles = []
        for line in raw_lines:
            cols = [x.strip() for x in line.split('|')]
            if len(cols) < 9: continue
            try:
                c = parse_candle(cols)
                if '2026-' in c['d'] or len(c['d'])==5:
                    candles.append(c)
            except:
                pass
        if len(candles) < 8: continue
        
        t = candles[-1]
        tc = t['c']; to = t['o']; th = t['h']; tl = t['l']; tv = t['v']
        hl = th - tl
        body = abs(tc - to)
        body_pct = body / hl * 100 if hl > 0 else 0
        upper_pct = (th - max(tc, to)) / hl * 100 if hl > 0 else 0
        lower_pct = (min(tc, to) - tl) / hl * 100 if hl > 0 else 0
        is_green = tc >= to
        
        d5 = candles[-5]
        chg5 = (tc / d5['c'] - 1) * 100
        
        vols = [c['v'] for c in candles[-10:]]
        avg_v = sum(vols) / len(vols)
        vr = tv / avg_v if avg_v > 0 else 1
        
        vol_pattern = ''
        for i in range(-5, 0):
            v = candles[i]['v']
            vol_pattern += '+' if v > avg_v * 1.1 else ('-' if v < avg_v * 0.9 else '.')
        
        p20 = [c['c'] for c in candles[-20:]]
        r20_h, r20_l = max(p20), min(p20)
        pos = (tc - r20_l) / (r20_h - r20_l) * 100 if r20_h > r20_l else 50
        
        desc = 'GREEN' if is_green else 'RED'
        if body_pct > 70: desc += '+长实体'
        elif body_pct < 12: desc += '+十字星'
        else: desc += f'+{body_pct:.0f}%实体'
        if upper_pct > 55: desc += '+长上影'
        if lower_pct > 55: desc += '+长下影'
        
        print(name + '|' + str(round(tc,3)) + '|5d' + f'{chg5:+.1f}%' + '|' + desc + '|量' + f'{vr:.2f}' + '|量型' + vol_pattern + '|位置' + f'{pos:.0f}%')
        break
