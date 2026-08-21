#!/usr/bin/env python3
"""动量修正误杀率回测：道氏引擎五层保险中的"近期动量修正"是否误杀真趋势启动

方法：sina 250根日K，滚动40日窗口，对比 带/不带 动量修正的判定，
统计触发修正的案例未来 5/10/20 日实际涨幅，评估误杀率。
"""
import sys
sys.path.insert(0, '/c/Users/Admin/WorkBuddy/2026-07-09-14-20-24/scripts')
from trading_engine.data_fetcher import _from_sina_etf
from trading_engine.dow_engine import _compute_trend
import pandas as pd

# ── 复制 _compute_trend 逻辑，加 suppress_momentum 开关（不改生产代码）──
def _compute_trend_bt(df, label, suppress_momentum=False):
    import pandas_ta as ta
    if len(df) < 10:
        return "range", 0, 0
    closes = df["close"]; highs = df["high"]; lows = df["low"]
    adx_period = min(14, len(df) // 2)
    adx_series = ta.adx(high=highs, low=lows, close=closes, length=adx_period)
    adx_val = 0.0; pdi_val = 25.0; ndi_val = 25.0; adx_rising = False
    if adx_series is not None and len(adx_series) > 0:
        adx_col = [c for c in adx_series.columns if "ADX" in c]
        if adx_col:
            raw = adx_series[adx_col[0]].iloc[-1]
            adx_val = round(raw, 1) if pd.notna(raw) else 0.0
            if len(adx_series) >= 4:
                last3 = adx_series[adx_col[0]].iloc[-4:].values
                valid = [v for v in last3 if pd.notna(v)]
                if len(valid) >= 3:
                    rises = sum(1 for i in range(1, len(valid)) if valid[i] > valid[i-1])
                    adx_rising = rises >= 2
        pdi_col = [c for c in adx_series.columns if "DMP" in c]
        ndi_col = [c for c in adx_series.columns if "DMN" in c]
        if pdi_col:
            pdi_val = round(float(adx_series[pdi_col[0]].iloc[-1]), 1) if pd.notna(adx_series[pdi_col[0]].iloc[-1]) else 25.0
        if ndi_col:
            ndi_val = round(float(adx_series[ndi_col[0]].iloc[-1]), 1) if pd.notna(adx_series[ndi_col[0]].iloc[-1]) else 25.0
    sma5 = ta.sma(closes, length=min(5, len(closes)))
    sma20 = ta.sma(closes, length=min(20, len(closes)))
    sma60 = ta.sma(closes, length=min(60, len(closes)))
    s5 = sma5.iloc[-1] if sma5 is not None and len(sma5) > 0 else None
    s20 = sma20.iloc[-1] if sma20 is not None and len(sma20) > 0 else None
    s60 = sma60.iloc[-1] if sma60 is not None and len(sma60) > 0 else None
    bull = 0; bear = 0
    if adx_val > 25 and adx_rising:
        if pdi_val > ndi_val: bull += 2
        elif ndi_val > pdi_val: bear += 2
    elif adx_val > 25 and not adx_rising:
        if pdi_val > ndi_val: bull += 1
        elif ndi_val > pdi_val: bear += 1
    elif adx_val > 20:
        if pdi_val > ndi_val * 1.15: bull += 1
        elif ndi_val > pdi_val * 1.15: bear += 1
    if s5 and s20 and s60 and all(pd.notna(x) for x in [s5, s20, s60]):
        last_c = closes.iloc[-1]
        if s5 > s20 * 1.005 and s20 > s60 * 1.005 and last_c > s5: bull += 2
        elif s5 < s20 * 0.995 and s20 < s60 * 0.995 and last_c < s5: bear += 2
        elif s5 > s20 * 1.005 and last_c > s5: bull += 1
        elif s5 < s20 * 0.995 and last_c < s5: bear += 1
    if s20 and pd.notna(s20) and s20 > 0:
        last_c = closes.iloc[-1]
        if last_c > s20 * 1.02: bull += 1
        elif last_c < s20 * 0.98: bear += 1
    total = bull + bear
    min_conf = 2 if adx_val >= 20 else 3
    if bull >= min_conf and bull > bear: direction = "bull"; strength = bull / max(total, 5)
    elif bear >= min_conf and bear > bull: direction = "bear"; strength = bear / max(total, 5)
    else: direction = "range"; strength = 0.0
    if s20 and pd.notna(s20) and s20 > 0:
        last_c = closes.iloc[-1]
        if direction == "bull" and last_c < s20 * 0.93: direction = "range"; strength = 0.0
        elif direction == "bear" and last_c > s20 * 1.07: direction = "range"; strength = 0.0
    if not suppress_momentum and len(closes) >= 4:
        recent_chg = (closes.iloc[-1] / closes.iloc[-3] - 1) * 100
        margin = abs(bull - bear)
        if recent_chg > 3 and direction == "bear" and margin <= 2:
            direction = "range"; strength = 0.0
        elif recent_chg < -3 and direction == "bull" and margin <= 2:
            direction = "range"; strength = 0.0
    return direction, adx_val, margin if 'margin' in dir() else abs(bull - bear)


def run_backtest(code, name, window=40, future_days=10):
    cands = _from_sina_etf(code, 'daily')
    if not cands or len(cands) < 80:
        return None
    # 过滤周末假K线
    cands = [c for c in cands if pd.to_datetime(c.date).weekday() < 5]
    df = pd.DataFrame({
        'open': [c.open for c in cands], 'high': [c.high for c in cands],
        'low': [c.low for c in cands], 'close': [c.close for c in cands],
        'volume': [c.volume for c in cands],
    })
    n = len(df)
    # 滚动回测
    triggers = []   # 动量修正触发的案例
    bear_cases = [] # 所有判bear的案例（对照）
    bull_cases = []
    for t in range(window + 5, n - future_days):
        win = df.iloc[t - window:t]
        if len(win) < window: continue
        d_with, adx, margin = _compute_trend_bt(win, 'bt', suppress_momentum=False)
        d_without, _, _ = _compute_trend_bt(win, 'bt', suppress_momentum=True)
        close_now = df['close'].iloc[t]
        f5 = (df['close'].iloc[t + 5] / close_now - 1) * 100 if t + 5 < n else None
        f10 = (df['close'].iloc[t + future_days] / close_now - 1) * 100
        f20 = (df['close'].iloc[min(t + 20, n - 1)] / close_now - 1) * 100
        # 动量修正触发: 带修正=range 但 不带修正≠range（且是bear→range或bull→range）
        if d_with == 'range' and d_without != 'range':
            triggers.append((d_without, f5, f10, f20))
        if d_without == 'bear':
            bear_cases.append((f5, f10, f20))
        if d_without == 'bull':
            bull_cases.append((f5, f10, f20))
    return triggers, bear_cases, bull_cases, n


etfs = [
    ('512980', '传媒'), ('588200', '科创芯片'), ('512800', '银行'),
    ('159611', '电力'), ('515790', '光伏'), ('512400', '有色'),
    ('512880', '证券'), ('512690', '酒'), ('512010', '医药'), ('159992', '创新药'),
]

total_trig = 0
total_f10 = []
total_f20 = []
trig_up10 = 0
trig_down10 = 0

print(f'{"标的":<8} {"窗口数":>6} {"触发次数":>6} {"触发率":>6} | {"触发后5日%":>8} {"10日%":>7} {"20日%":>7} | {"10日>3%":>6} {"10日<0%":>6}')
print('-' * 90)
for code, name in etfs:
    r = run_backtest(code, name)
    if not r:
        print(f'{name:<8} 数据不足')
        continue
    triggers, bear_cases, bull_cases, n = r
    if not triggers:
        print(f'{name:<8} {n:>6} {"0":>6} {"-":>6} | 无动量修正触发')
        continue
    f5s = [t[1] for t in triggers if t[1] is not None]
    f10s = [t[2] for t in triggers]
    f20s = [t[3] for t in triggers]
    up = sum(1 for x in f10s if x > 3)
    down = sum(1 for x in f10s if x < 0)
    avg5 = sum(f5s) / len(f5s) if f5s else 0
    avg10 = sum(f10s) / len(f10s)
    avg20 = sum(f20s) / len(f20s)
    total_trig += len(triggers)
    total_f10 += f10s
    total_f20 += f20s
    trig_up10 += up
    trig_down10 += down
    print(f'{name:<8} {n:>6} {len(triggers):>6} {len(triggers)/max(n,1)*100:>5.1f}% | {avg5:>+7.1f}% {avg10:>+6.1f}% {avg20:>+6.1f}% | {up:>4}/{len(f10s)} {down:>4}/{len(f10s)}')

print('-' * 90)
if total_trig:
    avg10_all = sum(total_f10) / len(total_f10)
    avg20_all = sum(total_f20) / len(total_f20)
    print(f'合计: {total_trig}次触发 | 触发后10日均涨跌 {avg10_all:+.2f}% | 20日均 {avg20_all:+.2f}%')
    print(f'误杀率(10日>3%的真启动): {trig_up10}/{total_trig} = {trig_up10/total_trig*100:.0f}%')
    print(f'修正正确率(10日<0%继续跌): {trig_down10}/{total_trig} = {trig_down10/total_trig*100:.0f}%')
