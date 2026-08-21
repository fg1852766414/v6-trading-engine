#!/usr/bin/env python3
"""直接从neodata拉30日K线，使用subprocess直接调用python3"""
import sys, json, subprocess, os

ND = "D:/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/neodata-financial-search"
Q = f"{ND}/scripts/query.py"

ETFS = [
    ("588200","科创芯片ETF嘉实"),("512480","半导体ETF国联安"),("159819","人工智能ETF"),
    ("159852","软件ETF嘉实"),("159869","游戏ETF华夏"),("159992","创新药ETF银华"),("512880","证券ETF国泰"),
    ("512800","银行ETF华宝"),("562500","机器人ETF华夏"),("159530","机器人ETF易方达"),
    ("159326","电网设备ETF华夏"),("159611","电力ETF广发"),("515880","通信ETF国泰"),
    ("512710","军工龙头ETF富国"),("512400","有色金属ETF南方"),("515220","煤炭ETF国泰"),
    ("518880","华安黄金ETF"),("159865","养殖ETF国泰"),("512980","传媒ETF广发"),
    ("512690","酒ETF"),("512010","医药ETF"),("515030","新能源车ETF"),
    ("515790","光伏ETF华泰柏瑞"),("516160","新能源ETF南方"),("512200","房地产ETF南方"),
    ("159766","旅游ETF富国"),("159996","家电ETF国泰"),("515170","食品饮料ETF华夏"),
    ("513180","恒生科技ETF华夏"),
]

all_data = {}
for code, name in ETFS:
    r = subprocess.run(
        [sys.executable, Q, "--query", f"{code} {name} 近3个月日K线"],
        capture_output=True, timeout=30, cwd=ND
    )
    closes = []
    try:
        data = json.loads(r.stdout)
        for item in data.get("data",{}).get("apiData",{}).get("apiRecall",[]):
            content = item.get("content","")
            if "数据详情" not in content: continue
            for line in content.split("\n"):
                cols = [c.strip() for c in line.split("|")]
                if len(cols) < 10 or "2026" not in cols[9]: continue
                try:
                    closes.append((cols[9], round(float(cols[2].replace(",","")), 3)))
                except: pass
    except: pass
    closes = closes[-30:] if len(closes) >= 30 else closes
    all_data[code] = {"name": name, "closes": closes}
    print(f"{name}: {len(closes)}根 (最新={closes[-1][1] if closes else "—"})")

html = '''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>30日K线走势 - 11只ETF</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
body{font-family:-apple-system,"PingFang SC",sans-serif;max-width:1100px;margin:0 auto;padding:20px;background:#f7f8fa;color:#1f2937}
h1{font-size:22px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.card{background:#fff;border-radius:10px;padding:12px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}
.card h3{margin:0 0 2px 0;font-size:14px}.card .meta{font-size:11px;color:#6b7280;margin-bottom:4px}
.card canvas{width:100%!important;height:200px!important}
.disc{color:#6b7280;font-size:12px;text-align:center;margin-top:16px;padding:12px;border-top:1px solid #e5e7eb}
</style></head><body>
<h1>📈 11只ETF 30日收盘价走势</h1>
<div class="grid">'''

for code, d in all_data.items():
    html += f'<div class="card"><h3>{d["name"]} ({code})</h3><div class="meta">最近{len(d["closes"])}个交易日</div><canvas id="c{code}"></canvas></div>'

html += '</div><div class="disc">数据来源: NeoData | 2026-07-10 收盘</div><script>'

for code, d in all_data.items():
    vals = [c[1] for c in d["closes"]]
    labels = [c[0][5:] for c in d["closes"]]
    if not vals: continue
    color = "#059669" if vals[-1] >= (vals[-2] if len(vals)>=2 else vals[-1]) else "#dc2626"
    hl = max(vals); ll = min(vals)
    html += f'''new Chart(document.getElementById('c{code}'),{{type:'line',data:{{
labels:{json.dumps(labels)},datasets:[{{label:'收盘价',data:{vals},
borderColor:'{color}',backgroundColor:'rgba(5,150,105,0.06)',fill:true,
borderWidth:2,pointRadius:1.5,tension:0.3}}]}},options:{{
responsive:true,maintainAspectRatio:false,
plugins:{{legend:{{display:false}},tooltip:{{callbacks:{{label:function(c){{return c.raw.toFixed(3)+'元'}}}}}}}},
scales:{{x:{{display:false,grid:{{display:false}}}},
y:{{min:{ll},max:{hl},ticks:{{maxTicksLimit:4,callback:function(v){{return v.toFixed(3)}}}},grid:{{color:'rgba(0,0,0,0.04)'}}}}}}}}}});'''

html += '</script></body></html>'

out = "C:/Users/Admin/WorkBuddy/2026-07-09-14-20-24/deliverables/trading-agent/etf-30day-close-2026-07-10.html"
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, 'w') as f:
    f.write(html)
print(f"\n✅ {out}")
