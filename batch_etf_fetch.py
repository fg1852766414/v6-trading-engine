#!/usr/bin/env python3
"""
ETF 批量数据获取脚本 V4
一次性并行获取所有ETF的行情 + 自算技术指标 + 份额
速度：11只约5-10秒
用法：
  python3 scripts/batch_etf_fetch.py              # 全部11只
  python3 scripts/batch_etf_fetch.py 588200 159992  # 指定代码
"""

import json, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

NEODATA_DIR = "D:/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/neodata-financial-search"
Q = f"{NEODATA_DIR}/scripts/query.py"

ETF_LIST = [
    ("588200.SH", "科创芯片ETF嘉实", "B"),
    ("512480.SH", "半导体ETF国联安", "B"),
    ("159819.SZ", "人工智能ETF", "B"),
    ("159852.SZ", "软件ETF嘉实", "B"),
    ("159869.SZ", "游戏ETF华夏", "B"),
    ("159992.SZ", "创新药ETF银华", "B"),
    ("512880.SH", "证券ETF国泰", "A"),
    ("512800.SH", "银行ETF华宝", "A"),
    ("562500.SH", "机器人ETF华夏", "B"),
    ("159530.SZ", "机器人ETF易方达", "B"),
    ("159326.SZ", "电网设备ETF华夏", "A"),
    ("159611.SZ", "电力ETF广发", "A"),
    ("515880.SH", "通信ETF国泰", "B"),
    ("512710.SH", "军工龙头ETF富国", "B"),
    ("512400.SH", "有色金属ETF南方", "A"),
    ("515220.SH", "煤炭ETF国泰", "A"),
    ("518880.SH", "华安黄金ETF", "A"),
    ("159865.SZ", "养殖ETF国泰", "B"),
    ("512980.SH", "传媒ETF广发", "B"),
    ("512690.SH", "酒ETF", "B"),
    ("512010.SH", "医药ETF", "B"),
    ("515030.SZ", "新能源车ETF", "B"),
    ("515790.SH", "光伏ETF华泰柏瑞", "A"),
    ("516160.SH", "新能源ETF南方", "A"),
    ("512200.SH", "房地产ETF南方", "B"),
    ("159766.SZ", "旅游ETF富国", "B"),
    ("159996.SZ", "家电ETF国泰", "B"),
    ("515170.SH", "食品饮料ETF华夏", "B"),
    ("513180.SH", "恒生科技ETF华夏", "B"),
]

def query(t):
    try:
        r = subprocess.run([sys.executable, Q, "--query", t],
            capture_output=True, text=True, timeout=25, cwd=NEODATA_DIR)
        if r.returncode == 0 and r.stdout.strip():
            d = json.loads(r.stdout)
            if d.get("code")=="200" and d.get("suc"): return d
    except: pass
    return {}

def calc_ma(data, n):
    if len(data) < n: return "?"
    return f"{sum(data[-n:])/n:.3f}"

def calc_macd(data):
    """计算MACD: DIF=EMA12-EMA26 → DEA=EMA(DIF,9) → 柱=(DIF-DEA)*2"""
    if len(data) < 35: return "?","?","?"
    # DIF = EMA12 - EMA26
    difs = []
    ema12, ema26 = data[0], data[0]
    for p in data[1:]:
        ema12 = ema12 * 11/13 + p * 2/13
        ema26 = ema26 * 25/27 + p * 2/27
        difs.append(ema12 - ema26)
    # DEA = EMA(DIF, 9) — Wilder平滑
    dea_seq = difs[0]
    for d in difs[1:]:
        dea_seq = dea_seq * 8/10 + d * 2/10
    dif_val, dea_val = difs[-1], dea_seq
    hist = (dif_val - dea_val) * 2
    return f"{dif_val:.3f}", f"{dea_val:.3f}", f"{hist:.3f}"

def calc_rsi(data, n=14):
    """Wilder平滑RSI"""
    if len(data) <= n + 1: return "?"
    # 首次：前n根算平均
    gains, losses = 0, 0
    for i in range(1, n+1):
        d = data[i] - data[i-1]
        if d >= 0: gains += d
        else: losses -= d
    avg_gain, avg_loss = gains / n, losses / n
    # Wilder平滑：后续逐根
    for i in range(n+1, len(data)):
        d = data[i] - data[i-1]
        gain = d if d >= 0 else 0
        loss = -d if d < 0 else 0
        avg_gain = (avg_gain * (n-1) + gain) / n
        avg_loss = (avg_loss * (n-1) + loss) / n
    if avg_loss == 0: return "100.0"
    rs = avg_gain / avg_loss
    return f"{100 - 100/(1+rs):.1f}"

def find_col_index(header_row, target_name):
    """在表头行里找目标列的索引（列名匹配，不依赖硬编码）"""
    cols = [x.strip() for x in header_row.split("|")]
    for i, c in enumerate(cols):
        if target_name in c:
            return i
    return -1

def fetch_one(code, name):
    bare_code = code.split(".")[0]  # "159530.SZ" → "159530" (neodata对带后缀查询不稳定)
    r = {"code":code, "name":name, "price":"?", "change":"?", "high":"?",
         "low":"?", "volume":"?", "ma5":"?", "ma10":"?",
         "ma20":"?", "ma60":"?", "diff":"?", "dea":"?", "hist":"?",
         "rsi":"?", "shares":"?", "scale":"?", "share_date":"?"}
    
    # ── 1. 实时行情(用裸代码查，重试1次防并行竞态) ──
    for retry in range(2):
        d = query(f"{bare_code} {name} 实时行情")
        try:
            for item in d.get("data",{}).get("apiData",{}).get("apiRecall",[]):
                if item.get("type")=="基金实时行情查询":
                    for row in item.get("content","").split("\n"):
                        cols = [x.strip() for x in row.split("|")]
                        if len(cols)>=20 and any(kw in row for kw in ["沪市","深市"]):
                            r["price"] = cols[4]
                            r["prev_close"] = cols[5]
                            r["volume"] = cols[7]
                            try:
                                p = float(cols[4].replace(",",""))
                                pc = float(cols[5].replace(",",""))
                                if pc > 0: r["change"] = f"{(p/pc-1)*100:.2f}%"
                            except: pass
        except: pass
        if r.get("price","?") != "?": break
        import time as _t; _t.sleep(0.5)

    # ── 2. 历史K线 → 自算技术指标 ──
    d = query(f"{bare_code} {name} 近3个月历史行情 日K线 开盘价 收盘价 最高价 最低价")
    try:
        closes = []
        for item in d.get("data",{}).get("apiData",{}).get("apiRecall",[]):
            c = item.get("content","")
            if "数据详情" in c or "数据详情" in c:
                lines = c.split("\n")
                if not lines: break
                # 从表头解析"最新价或收盘"的列索引
                close_idx = -1
                header = next((l for l in lines if "开盘价" in l and "收盘" in l), "")
                if header:
                    cols = [x.strip() for x in header.split("|")]
                    for i, col in enumerate(cols):
                        if "收盘" in col:
                            close_idx = i
                            break
                if close_idx < 1:
                    close_idx = 2  # 回退到已知的第2列
                # 解析数据行
                for line in lines:
                    cols = [x.strip() for x in line.split("|")]
                    if len(cols) >= 8 and "2026-" in line:
                        try:
                            close = float(cols[close_idx].replace(",",""))
                            if close > 0: closes.append(close)
                        except: pass
        if len(closes) >= 10:
            closes = closes[-120:]
            r["ma5"] = calc_ma(closes, 5)
            r["ma10"] = calc_ma(closes, 10)
            r["ma20"] = calc_ma(closes, 20)
            r["ma60"] = calc_ma(closes, 60)
            dif, dea, hist = calc_macd(closes)
            r["diff"], r["dea"], r["hist"] = dif, dea, hist
            r["rsi"] = calc_rsi(closes, 14)
            r["kline_count"] = len(closes)
    except: pass

    # ── 3. 份额 ──
    d = query(f"{bare_code} {name} 最新份额")
    try:
        for item in d.get("data",{}).get("apiData",{}).get("apiRecall",[]):
            if item.get("type")=="基金最新份额查询":
                for row in item.get("content","").split("\n"):
                    sc = code.split(".")[0].lstrip("0")
                    if sc in row.replace(" ",""):
                        cols = [x.strip() for x in row.split("|")]
                        if len(cols)>=7:
                            r["share_date"] = cols[3]
                            r["shares"] = cols[4]
                            r["scale"] = cols[5]
    except: pass
    
    return r

def run(codes=None):
    targets = [(c,n,m) for c,n,m in ETF_LIST if not codes or c.split(".")[0] in codes]
    if not targets: targets = ETF_LIST
    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        fs = {ex.submit(fetch_one,c,n):c for c,n,m in targets}
        for f in as_completed(fs):
            try:
                rr = f.result(); results[rr["code"]] = rr
            except: pass
    return results, targets

if __name__ == "__main__":
    codes = sys.argv[1:] if len(sys.argv)>1 else None
    t0 = time.time()
    results, targets = run(codes)
    t = time.time()-t0
    
    print(f"✅ {len(targets)}只ETF, {t:.1f}秒, K线根数/溢价率")
    print(f"{'代码':<12}{'名称':<14}{'价格':>8}{'涨跌':>8}{'MA5':>8}{'MA10':>8}{'MA20':>8}{'MACD':>8}{'RSI':>6}{'溢价':>8}{'份额(亿)':>10}")
    print("-"*100)
    for c,n,m in targets:
        r = results.get(c,{})
        p=r.get("price","?"); ch=r.get("change","?")
        m5=r.get("ma5","?"); m10=r.get("ma10","?"); m20=r.get("ma20","?")
        h=r.get("hist","?")
        mf="?"
        try:
            hv=float(h.replace(",",""))
            mf="🟢金叉" if hv>0 else "🔴死叉" if hv<0 else "平"
        except: pass
        rs=r.get("rsi","?")
        sh=r.get("shares","?")
        try: shb=f"{float(sh)/1e8:.1f}"
        except: shb=sh[:6]
        print(f"{c:<12}{n:<14}{p:>8}{ch:>8}{m5:>8}{m10:>8}{m20:>8}{mf:>8}{rs:>6}{'—':>8}{shb:>10}")
    
    print("\n--- JSON ---")
    out = [{**results.get(c,{}), "mode":m} for c,n,m in targets]
    for o in out:
        for k in list(o.keys()):
            if isinstance(o[k], str) and len(o[k]) > 80:
                o[k] = o[k][:80]
    print(json.dumps(out, ensure_ascii=False, indent=2))
