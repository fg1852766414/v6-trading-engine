#!/usr/bin/env python3
"""
ETF 日间份额追踪 v2 — AKShare + neodata 互补
- AKShare: 拉前4天上交所逐日份额 (7/6→7/9)
- neodata: 查当日份额快照 (7/10)
- 合并5日序列，输出3日扳机 + 5日保险判断

用法：
  python3 scripts/etf_shares.py                # 全部11只，表格
  python3 scripts/etf_shares.py 588200 159992  # 指定代码
  python3 scripts/etf_shares.py --json         # JSON输出
"""
import sys, json
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

TARGETS = {
    "588200": "科创芯片ETF嘉实",
    "512480": "半导体ETF国联安",
    "159819": "人工智能ETF",
    "159852": "软件ETF嘉实",
    "159869": "游戏ETF华夏",
    "159992": "创新药ETF银华",
    "512880": "证券ETF国泰",
    "512800": "银行ETF华宝",
    "562500": "机器人ETF华夏",
    "159530": "机器人ETF易方达",
    "159326": "电网设备ETF华夏",
    "159611": "电力ETF广发",
    "515880": "通信ETF国泰",
    "512710": "军工龙头ETF富国",
    "512400": "有色金属ETF南方",
    "515220": "煤炭ETF国泰",
    "518880": "华安黄金ETF",
    "159865": "养殖ETF国泰",
    "512980": "传媒ETF广发",
    "512690": "酒ETF",
    "512010": "医药ETF",
    "515030": "新能源车ETF",
    "515790": "光伏ETF华泰柏瑞",
    "516160": "新能源ETF南方",
    "512200": "房地产ETF南方",
    "159766": "旅游ETF富国",
    "159996": "家电ETF国泰",
    "515170": "食品饮料ETF华夏",
    "513180": "恒生科技ETF华夏",
}

args = sys.argv[1:]
output_json = "--json" in args
if output_json: args.remove("--json")
targets = {c: TARGETS.get(c, f"ETF{c}") for c in args} if args else dict(TARGETS)

# ── 最近5个交易日 ──
dates = []
d = datetime.now()
while len(dates) < 5:
    if d.weekday() < 5:
        dates.append(d.strftime("%Y%m%d"))
    d -= timedelta(days=1)
dates = sorted(dates, reverse=True)  # [7/10, 7/9, 7/8, 7/7, 7/6]

today_str = dates[0]

# ========== 1. AKShare: 前4天逐日份额 ==========
sse_shares = {}
for date_str in dates:
    try:
        df = ak.fund_etf_scale_sse(date=date_str)
        for _, row in df.iterrows():
            code = str(row['基金代码']).strip()
            if code in targets:
                sse_shares.setdefault(code, {})[date_str] = float(row['基金份额'])
    except:
        pass  # 周末/收盘前不可用，正常跳过

# ========== 2. 深交所份额: fund_scale_daily_szse（日期范围查询）==========
szse_shares = {}
try:
    df_sz = ak.fund_scale_daily_szse(
        start_date=dates[-1],  # 最早日期 e.g. "20260706"
        end_date=dates[0],     # 最晚日期 e.g. "20260710"
        symbol='ETF'
    )
    for _, row in df_sz.iterrows():
        code = str(row['基金代码']).strip()
        if code in targets:
            d_val = row['日期']
            if hasattr(d_val, 'strftime'):  # datetime类型
                d = d_val.strftime('%Y%m%d')
            else:
                d = str(d_val).replace("-", "").replace(" ", "")
            szse_shares.setdefault(code, {})[d] = float(row['基金份额'])
except Exception as e:
    import sys as _sys
    print(f"  [SZSE查询异常: {e}]", file=_sys.stderr)

# ========== 3. 东方财富快照: 折价率 ==========
# 注意: spot_em在盘中就有当日估算数据,比neodata和SSE都新
spot_data = {}
try:
    df_spot = ak.fund_etf_spot_em()
    for code in targets:
        row = df_spot[df_spot['代码'].astype(str) == code]
        if not row.empty:
            r = row.iloc[0]
            prem = float(r['基金折价率']) if pd.notna(r['基金折价率']) else None
            spot_shares = float(r['最新份额']) if pd.notna(r['最新份额']) else None
            spot_data[code] = {
                'price': float(r['最新价']),
                'premium': prem,
                'shares': spot_shares,
                'date': str(r['数据日期']),
            }
except:
    pass

# ========== 4. 合并输出 ==========
def check_trigger(daily_list):
    """3日扳机: 最近3个可用日是否连续净申购"""
    if len(daily_list) < 3: return False, "数据不足"
    last3 = [s for _, s in daily_list[-3:]]
    if all(last3[i] > last3[i-1] for i in range(1, len(last3))):
        return True, "连续↑"
    return False, "未连续"

def check_insurance(daily_list):
    """5日保险: 全部5日趋势向上"""
    if len(daily_list) < 4: return False, "数据不足"
    # 线性趋势: 最后一天 > 第一天
    if daily_list[-1][1] > daily_list[0][1]:
        return True, "趋势↑"
    return False, "趋势↓"

result = []
for code, name in targets.items():
    item = {"code": code, "name": name}

    # 份额序列: SSE(上交所) + SZSE(深交所) 合并
    merged = {}
    if code in sse_shares:
        merged.update(sse_shares[code])
    if code in szse_shares:
        merged.update(szse_shares[code])
    
    daily_list = sorted(merged.items())
    item["daily_shares"] = [{"date": d, "shares": s} for d, s in daily_list]
    item["data_sources"] = {
        "akshare_sse_days": list(sse_shares.get(code, {}).keys()) if code in sse_shares else [],
        "akshare_szse_days": list(szse_shares.get(code, {}).keys()) if code in szse_shares else [],
    }
    
    # 3日扳机
    trig_ok, trig_label = check_trigger(daily_list)
    item["trigger_3day"] = {"passed": trig_ok, "label": trig_label}
    
    # 5日保险
    ins_ok, ins_label = check_insurance(daily_list)
    item["insurance_5day"] = {"passed": ins_ok, "label": ins_label}
    
    # 综合结论
    if trig_ok and ins_ok:
        item["share_conclusion"] = "✅入场信号 (3日扳机+5日保险全部通过)"
    elif trig_ok and not ins_ok:
        item["share_conclusion"] = "⚠️可建底仓 (3日扳机通过,5日保险未确认)"
    else:
        item["share_conclusion"] = "❌不满足条件"
    
    # 折价率
    if code in spot_data:
        s = spot_data[code]
        item["price"] = s["price"]
        item["premium_pct"] = s["premium"]
        if s["premium"] is not None:
            item["premium_flag"] = "⚠️溢价>2%" if s["premium"] > 2 else "🔴折价>2%" if s["premium"] < -2 else "平价"
    
    result.append(item)

if output_json:
    print(json.dumps(result, ensure_ascii=False, indent=2))
else:
    print(f"拉取交易日: {dates}")
    print(f"数据源: AKShare(前4天) + neodata(当天)\n")
    print("=" * 110)
    print(f"{'ETF名称':<14} {'代码':<7} {'份额序列':>30} {'3日扳机':>10} {'5日保险':>10} {'结论':>20} {'溢价':>8}")
    print("=" * 110)
    for r in result:
        seq = "→".join(f"{s['shares']/1e8:.1f}" for s in r["daily_shares"][-5:])
        t = "🟢通过" if r["trigger_3day"]["passed"] else "🔴未过"
        ins = "🟢通过" if r["insurance_5day"]["passed"] else "🔴未过"
        pm = r.get("premium_flag", "—")
        print(f"{r['name']:<14} {r['code']:<7} {seq:>30} {t:>10} {ins:>10} {r['share_conclusion']:>20} {pm:>8}")
