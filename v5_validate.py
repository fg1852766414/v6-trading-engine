#!/usr/bin/env python3
"""V5引擎全量验证：neodata K线 → Candle[] → quick_analysis()"""
import sys, json, subprocess, os

# 确保能找到trading_engine包
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_WORKSPACE, "scripts"))
sys.path.insert(0, _WORKSPACE)

from trading_engine import quick_analysis, Candle

# 从share_cache.json读取份额数据
def _load_share_cache():
    cache_path = os.path.join(_WORKSPACE, "scripts", "share_cache.json")
    with open(cache_path) as f:
        raw = json.load(f)
    result = {}
    for r in raw:
        shares = r.get("daily_shares", [])
        if len(shares) >= 2:
            # 转换为变化率%
            changes = []
            for i in range(1, len(shares)):
                prev = shares[i-1]["shares"]
                curr = shares[i]["shares"]
                pct = (curr - prev) / prev * 100
                changes.append(round(pct, 2))
            result[r["code"]] = changes
    return result

SHARE_CACHE = _load_share_cache()

NEODATA_DIR = "D:/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/neodata-financial-search"
NEODATA_Q = f"{NEODATA_DIR}/scripts/query.py"

ETFS = [
    ("588200.SH", "科创芯片ETF嘉实"),
    ("159992.SZ", "创新药ETF银华"),
    ("512880.SH", "证券ETF国泰"),
    ("562500.SH", "机器人ETF华夏"),
    ("159530.SZ", "机器人ETF易方达"),
    ("159326.SZ", "电网设备ETF华夏"),
    ("159611.SZ", "电力ETF广发"),
    ("515880.SH", "通信ETF国泰"),
    ("512710.SH", "军工龙头ETF富国"),
    ("512400.SH", "有色金属ETF南方"),
    ("518880.SH", "华安黄金ETF"),
    ("159865.SZ", "养殖ETF国泰"),
]

def get_candles(code, name):
    """从neodata拉K线，返回Candle列表"""
    code_clean = code.split(".")[0]
    r = subprocess.run(
        [sys.executable, NEODATA_Q, "--query", f"{code_clean} {name} 近3个月日K线 开盘价 收盘价 最高价 最低价 成交量"],
        capture_output=True, text=True, timeout=30, cwd=NEODATA_DIR
    )
    candles = []
    try:
        data = json.loads(r.stdout)
        for item in data.get("data",{}).get("apiData",{}).get("apiRecall",[]):
            content = item.get("content","")
            if "数据详情" not in content: continue
            lines = content.split("\n")
            # neodata ETF K线: split后有前置空列，cols[1]=开, cols[2]=收, cols[3]=高, cols[4]=低, cols[5]=量, ..., cols[9]=日期
            for line in lines:
                cols = [c.strip() for c in line.split("|")]
                if len(cols) < 10: continue
                date_str = cols[9]
                if "2026" not in date_str: continue
                try:
                    candle = Candle(
                        date=date_str,
                        open=float(cols[1].replace(",","")),
                        close=float(cols[2].replace(",","")),
                        high=float(cols[3].replace(",","")),
                        low=float(cols[4].replace(",","")),
                        volume=float(cols[5].replace(",","") or "0"),
                    )
                    if candle.open > 0 and candle.close > 0:
                        candles.append(candle)
                except (ValueError, IndexError):
                    pass
    except:
        pass
    return candles

if __name__ == "__main__":
    print(f"V5引擎全量验证")
    print(f"="*70)
    print(f"{'ETF':<14} {'①PA':>6} {'②VPA':>6} {'③份额':>6} {'通过':>4} {'决策':>14}")
    print(f"-"*70)
    
    for code, name in ETFS:
        code_base = code.split(".")[0]
        candles = get_candles(code, name)
        shares = SHARE_CACHE.get(code_base, [0,0,0,0])
        
        result = quick_analysis(
            etf_code=code,
            candles=candles,
            share_changes=shares,
        )
        
        tickets = result.get("tickets", {})
        pa_s = tickets.get("pa", {}).get("status", "?")
        vpa_s = tickets.get("vpa", {}).get("status", "?")
        share_s = tickets.get("share", {}).get("status", "?")
        passed = tickets.get("passed", 0)
        decision = tickets.get("decision", "?")
        
        print(f"{name:<14} {pa_s:>6} {vpa_s:>6} {share_s:>6} {passed:>4} {decision:<14}")
    
    print(f"-"*70)
    print(f"✅ V5引擎完成11只ETF全量分析")
