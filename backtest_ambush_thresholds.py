# -*- coding: utf-8 -*-
"""埋伏信号阈值敏感性回测
对三要素阈值做敏感性扫描: 回撤 / 缩量 / 地量倍数
统计每种组合下: 触发次数、触发后10日/20日正收益概率、平均收益
"""
import sys
import numpy as np
import pandas as pd
from collections import defaultdict

sys.path.insert(0, '.')
from trading_engine.data_fetcher import _from_sina_etf

# 29只ETF池
ETFS = [
    ('000001.SH', '上证指数'), ('512800', '银行'), ('515220', '煤炭'), ('159611', '电力'),
    ('518880', '黄金'), ('515170', '食品饮料'), ('512010', '医药'), ('512690', '酒'),
    ('515790', '光伏'), ('512400', '有色'), ('159766', '旅游'), ('512980', '传媒'),
    ('512880', '证券'), ('512200', '房地产'), ('159865', '养殖'), ('159992', '创新药'),
    ('588200', '科创芯片'), ('512480', '半导体'), ('159819', 'AI'), ('515030', '新能源车'),
    ('159915', '创业板'), ('159530', '机器人易方达'), ('562500', '机器人华夏'), ('159326', '电网设备'),
    ('515880', '通信'), ('512710', '军工'), ('159869', '游戏'), ('516160', '新能源'),
    ('513180', '恒生科技'), ('159996', '家电'),
]

def load_data(code, name):
    """用sina拿长历史(250根)，上证指数走sina实时接口拿不到历史，用腾讯"""
    if code == '000001.SH':
        return None  # 指数跳过（个股ETF才适用埋伏逻辑）
    cands = _from_sina_etf(code, 'daily')
    return cands

def main():
    all_points = []  # 每个检测点: (drawdown, chg12, shrink, floor_ratio, no_new, above5, fwd10, fwd20)
    total_etfs = 0
    total_points = 0

    for code, name in ETFS:
        if code == '000001.SH':
            continue
        try:
            cands = load_data(code, name)
            if cands is None or len(cands) < 80:
                print(f'  ⏭ {name}: 数据不足({len(cands) if cands else 0}根)')
                continue
            total_etfs += 1
            closes = np.array([c.close for c in cands], dtype=float)
            volumes = np.array([c.volume for c in cands], dtype=float)
            lows = np.array([c.low for c in cands], dtype=float)
            n = len(closes)

            # 每5个交易日一个检测点（从第60根开始，留足窗口）
            for i in range(60, n - 20, 5):
                last_c = closes[i]
                highs60 = closes[i-59:i+1].max()
                drawdown = (last_c / highs60 - 1) * 100 if highs60 > 0 else 0
                chg12 = (last_c / closes[i-12] - 1) * 100 if i >= 12 else 0
                vol6 = volumes[i-5:i+1].mean()
                vol12 = volumes[i-11:i+1].mean()
                shrink = vol6 / vol12 if vol12 > 0 else 1.0
                vol_today = volumes[i]
                vol_low20 = volumes[i-19:i+1].min()
                floor_ratio = vol_today / vol_low20 if vol_low20 > 0 else 9.9
                no_new = lows[i] >= lows[i-3]
                sma5 = closes[i-4:i+1].mean()
                above5 = last_c >= sma5
                fwd10 = closes[i+10] / closes[i] - 1
                fwd20 = closes[i+20] / closes[i] - 1
                all_points.append((drawdown, chg12, shrink, floor_ratio, no_new, above5, fwd10, fwd20))
                total_points += 1
        except Exception as e:
            print(f'  ❌ {name}: {type(e).__name__}: {e}')

    print(f'\n数据就绪: {total_etfs}只ETF, {total_points}个检测点\n')

    # ── 阈值敏感性扫描 ──
    dd_range = [-15, -18, -22, -25]
    shrink_range = [0.75, 0.80, 0.85, 0.90]
    floor_range = [1.3, 1.6, 2.0, 2.5]

    results = []
    for dd_th in dd_range:
        for sh_th in shrink_range:
            for fl_th in floor_range:
                trig = []
                for (drawdown, chg12, shrink, floor_ratio, no_new, above5, fwd10, fwd20) in all_points:
                    low_enough = drawdown < dd_th
                    sold_out = shrink < sh_th
                    stabilising = no_new or above5
                    at_floor = floor_ratio <= fl_th
                    still_falling = chg12 < -5.0
                    if low_enough and sold_out and (at_floor and stabilising or still_falling):
                        trig.append((fwd10, fwd20))
                n_trig = len(trig)
                if n_trig == 0:
                    continue
                fwd10s = [t[0] for t in trig]
                fwd20s = [t[1] for t in trig]
                p10 = sum(1 for v in fwd10s if v > 0) / n_trig
                p20 = sum(1 for v in fwd20s if v > 0) / n_trig
                avg20 = np.mean(fwd20s)
                med20 = np.median(fwd20s)
                results.append({
                    '回撤': dd_th, '缩量': sh_th, '地量': fl_th,
                    '触发': n_trig, '10日胜率': p10, '20日胜率': p20,
                    '20日均收益': avg20, '20日中位': med20,
                })

    # 排序: 20日胜率 × 触发次数综合（胜率优先，触发太少剔除）
    results.sort(key=lambda r: (r['20日胜率'], r['触发']), reverse=True)

    print(f'{"回撤":>5} {"缩量":>5} {"地量":>5} {"触发":>5} {"10日胜率":>8} {"20日胜率":>8} {"20日均":>8} {"20日中位":>8}  标记')
    print('-' * 70)
    for r in results:
        mark = ''
        if abs(r['回撤'] - (-18)) < 0.01 and abs(r['缩量'] - 0.85) < 0.001 and abs(r['地量'] - 1.6) < 0.001:
            mark = ' ◀ 当前参数'
        print(f'{r["回撤"]:>5} {r["缩量"]:>5.2f} {r["地量"]:>5.1f} {r["触发"]:>5} '
              f'{r["10日胜率"]:>7.1%} {r["20日胜率"]:>7.1%} {r["20日均收益"]:>+7.2%} {r["20日中位"]:>+7.2%} {mark}')

    print(f'\n组合总数: {len(results)} / {len(dd_range)*len(shrink_range)*len(floor_range)}')

    # 顶部5组合明细
    print('\n=== TOP5 组合（20日胜率排序） ===')
    for r in results[:5]:
        print(f'  回撤<-{abs(r["回撤"])}% + 缩量<{r["缩量"]} + 地量<{r["地量"]}倍 → 触发{r["触发"]}次, '
              f'20日胜率{r["20日胜率"]:.0%}, 20日均{r["20日均收益"]:+.1%}, 中位{r["20日中位"]:+.1%}')

if __name__ == '__main__':
    main()
