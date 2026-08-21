# -*- coding: utf-8 -*-
"""埋伏信号 + 道氏主趋势过滤 对比回测
验证: 加"主趋势≠bear"过滤后，胜率/收益是否提升、触发量剩多少
"""
import sys
import numpy as np

sys.path.insert(0, '.')
from trading_engine.data_fetcher import _from_sina_etf
from trading_engine.dow_engine import _compute_trend
import pandas as pd

ETFS = [
    ('512800', '银行'), ('515220', '煤炭'), ('159611', '电力'), ('518880', '黄金'),
    ('515170', '食品饮料'), ('512010', '医药'), ('512690', '酒'), ('515790', '光伏'),
    ('512400', '有色'), ('159766', '旅游'), ('512980', '传媒'), ('512880', '证券'),
    ('512200', '房地产'), ('159865', '养殖'), ('159992', '创新药'), ('588200', '科创芯片'),
    ('512480', '半导体'), ('159819', 'AI'), ('515030', '新能源车'), ('159915', '创业板'),
    ('159530', '机器人易方达'), ('562500', '机器人华夏'), ('159326', '电网设备'),
    ('515880', '通信'), ('512710', '军工'), ('159869', '游戏'), ('516160', '新能源'),
    ('513180', '恒生科技'), ('159996', '家电'),
]

# 参数（4年回测最优组合）
DD_TH = -15.0
SH_TH = 0.85
FL_TH = 1.3

def main():
    points = []  # (drawdown, chg12, shrink, floor_ratio, no_new, above5, primary_dir, fwd10, fwd20)
    n_etfs = 0

    for code, name in ETFS:
        try:
            cands = _from_sina_etf(code, 'daily')
            if cands is None or len(cands) < 100:
                continue
            n_etfs += 1
            closes = np.array([c.close for c in cands], dtype=float)
            volumes = np.array([c.volume for c in cands], dtype=float)
            lows = np.array([c.low for c in cands], dtype=float)
            highs = np.array([c.high for c in cands], dtype=float)
            n = len(closes)

            for i in range(80, n - 20, 5):
                last_c = closes[i]
                drawdown = (last_c / closes[i-59:i+1].max() - 1) * 100
                chg12 = (last_c / closes[i-12] - 1) * 100
                vol6 = volumes[i-5:i+1].mean()
                vol12 = volumes[i-11:i+1].mean()
                shrink = vol6 / vol12 if vol12 > 0 else 1.0
                vol_low20 = volumes[i-19:i+1].min()
                floor_ratio = volumes[i] / vol_low20 if vol_low20 > 0 else 9.9
                no_new = lows[i] >= lows[i-3]
                sma5 = closes[i-4:i+1].mean()
                above5 = last_c >= sma5
                # 道氏主趋势（截至检测点的日K，SMA20/60）
                df_i = pd.DataFrame({'high': highs[:i+1], 'low': lows[:i+1], 'close': closes[:i+1]})
                try:
                    tr = _compute_trend(df_i, 'bt')
                    pdir = tr.direction
                except Exception:
                    pdir = 'range'
                fwd10 = closes[i+10] / closes[i] - 1
                fwd20 = closes[i+20] / closes[i] - 1
                points.append((drawdown, chg12, shrink, floor_ratio, no_new, above5, pdir, fwd10, fwd20))
        except Exception as e:
            print(f'  ❌ {name}: {type(e).__name__}')

    print(f'数据: {n_etfs}只ETF, {len(points)}检测点\n')

    # 埋伏触发（无过滤）
    trig_all, trig_filtered, trig_excluded = [], [], []
    for (drawdown, chg12, shrink, floor_ratio, no_new, above5, pdir, fwd10, fwd20) in points:
        low_enough = drawdown < DD_TH
        sold_out = shrink < SH_TH
        stabilising = no_new or above5
        at_floor = floor_ratio <= FL_TH
        still_falling = chg12 < -5.0
        if not (low_enough and sold_out and (at_floor and stabilising or still_falling)):
            continue
        trig_all.append((fwd10, fwd20, pdir))
        if pdir != 'bear':
            trig_filtered.append((fwd10, fwd20, pdir))
        else:
            trig_excluded.append((fwd10, fwd20, pdir))

    def stats(trigs, label):
        if not trigs:
            print(f'{label}: 触发0次')
            return
        n = len(trigs)
        p10 = sum(1 for t in trigs if t[0] > 0) / n
        p20 = sum(1 for t in trigs if t[1] > 0) / n
        avg20 = np.mean([t[1] for t in trigs])
        med20 = np.median([t[1] for t in trigs])
        print(f'{label}: {n}次触发 | 10日胜率{p10:.1%} | 20日胜率{p20:.1%} | 20日均{avg20:+.2%} | 20日中位{med20:+.2%}')
        return n, p20, avg20

    print('=== 对比结果 ===')
    stats(trig_all, '无过滤(原埋伏)   ')
    stats(trig_filtered, '加主趋势≠bear过滤')
    stats(trig_excluded, '被过滤掉的(主趋势bear)')

    # 被过滤掉的案例分析：是否真是"接飞刀"？
    if trig_excluded:
        n = len(trig_excluded)
        neg = sum(1 for t in trig_excluded if t[1] < 0)
        avg = np.mean([t[1] for t in trig_excluded])
        print(f'\n被过滤掉的 {n} 次中, {neg}次({neg/n:.0%})后续20日下跌, 平均{avg:+.2%}')
        print('→ 若这些真是"熊市接飞刀", 过滤有效; 若其中有大涨, 过滤误杀')

if __name__ == '__main__':
    main()
