#!/usr/bin/env python3
"""
V5 三票制全量扫描 — 固定脚本（禁止LLM直接写分析逻辑，只允许调此脚本）
用法:
  python scripts/v5_scan.py              # 全量12只
  python scripts/v5_scan.py 588200       # 单只
  python scripts/v5_scan.py 588200 159992 # 多只
  python scripts/v5_scan.py --brief      # 精简输出(仅三票结果)
  python scripts/v5_scan.py --json       # JSON输出(供程序消费)

核心原则:
  1. 所有决策来自 trading_engine.quick_analysis()
  2. LLM只管调用 — 禁止在对话里手写MA/MACD/VPA/PA判断
  3. ETF列表 = config.py里定义的备选池
  4. 份额数据 = scripts/share_cache.json
"""
import argparse, json, os, subprocess, sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

# ── 路径 ──────────────────────────────────────────
_WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_WORKSPACE, "scripts"))
sys.path.insert(0, _WORKSPACE)

from trading_engine import quick_analysis, Candle, EngineConfig

NEODATA_DIR = "D:/WorkBuddy/resources/app.asar.unpacked/resources/builtin-skills/neodata-financial-search"
NEODATA_Q = f"{NEODATA_DIR}/scripts/query.py"
CACHE_FILE = os.path.join(_WORKSPACE, "scripts", "share_cache.json")
SHARES_SCRIPT = os.path.join(_WORKSPACE, "scripts", "etf_shares.py")
# etf_shares.py 需要 venv 里的 akshare → 用专用 Python
SHARES_PY = "C:/Users/Admin/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# ── ETF备选池（来自config + 新增标的） ──────────────
ETF_POOL: Dict[str, str] = {
    "588200": "科创芯片ETF嘉实",
    "159992": "创新药ETF银华",
    "512880": "证券ETF国泰",
    "562500": "机器人ETF华夏",
    "159530": "机器人ETF易方达",
    "159326": "电网设备ETF华夏",
    "159611": "电力ETF广发",
    "515880": "通信ETF国泰",
    "512710": "军工龙头ETF富国",
    "512400": "有色金属ETF南方",
    "518880": "华安黄金ETF",
    "159865": "养殖ETF国泰",
    "512980": "传媒ETF广发",
}


# ═══════════════════════════════════════════════════
#  数据加载（不可修改）
# ═══════════════════════════════════════════════════

def _raw_to_changes(daily_shares: list) -> List[float]:
    """day_shares[{date, shares}] → 变化率序列"""
    changes = []
    for i in range(1, len(daily_shares)):
        prev = daily_shares[i - 1]["shares"]
        curr = daily_shares[i]["shares"]
        pct = (curr - prev) / prev * 100
        changes.append(round(pct, 2))
    return changes


def load_share_live(timeout_sec: int = 25) -> Tuple[Dict[str, List[float]], bool]:
    """
    优先调用 etf_shares.py 获取实时份额。
    失败/超时 → 回退到 share_cache.json
    """
    # ── 尝试实时 ──
    try:
        r = subprocess.run(
            [SHARES_PY, SHARES_SCRIPT, "--json"],
            capture_output=True, text=True, timeout=timeout_sec
        )
        # etf_shares.py 输出在最后面的 JSON 数组里
        raw = r.stdout
        start = raw.rfind("[{")
        if start >= 0:
            end = raw.rfind("}]") + 2
            data = json.loads(raw[start:end])
            result = {}
            for item in data:
                if item.get("daily_shares"):
                    result[item["code"]] = _raw_to_changes(item["daily_shares"])
            if result:
                print(f"[INFO] 实时份额获取成功 ({len(result)}只)", file=sys.stderr)
                return result, True
    except Exception as e:
        print(f"[WARN] 实时份额失败: {e}", file=sys.stderr)

    # ── 回退缓存 ──
    print("[INFO] 回退到 share_cache.json", file=sys.stderr)
    return load_share_cache(), False


def load_share_cache() -> Dict[str, List[float]]:
    """从 share_cache.json 加载份额变化率序列。"""
    if not os.path.exists(CACHE_FILE):
        print("[WARN] share_cache.json 不存在，份额数据全部置零", file=sys.stderr)
        return {}

    with open(CACHE_FILE) as f:
        raw = json.load(f)

    result: Dict[str, List[float]] = {}
    for r in raw:
        shares = r.get("daily_shares", [])
        if len(shares) >= 2:
            result[r["code"]] = _raw_to_changes(shares)
    return result


def fetch_candles(code: str, name: str) -> List[Candle]:
    """从 neodata 拉取 K线 → Candle 列表。"""
    code_clean = code
    r = subprocess.run(
        [sys.executable, NEODATA_Q, "--query",
         f"{code_clean} {name} 历史K线 日行情"],
        capture_output=True, text=True, timeout=30, cwd=NEODATA_DIR
    )
    candles: List[Candle] = []
    try:
        data = json.loads(r.stdout)
        for item in data.get("data", {}).get("apiData", {}).get("apiRecall", []):
            content = item.get("content", "")
            if "数据详情" not in content:
                continue
            for line in content.split("\n"):
                cols = [c.strip() for c in line.split("|")]
                if len(cols) < 10:
                    continue
                if "2026-" not in cols[9]:
                    continue
                try:
                    c = Candle(
                        date=cols[9],
                        open=float(cols[1].replace(",", "")),
                        close=float(cols[2].replace(",", "")),
                        high=float(cols[3].replace(",", "")),
                        low=float(cols[4].replace(",", "")),
                        volume=float(cols[5].replace(",", "") or "0"),
                    )
                    if c.open > 0 and c.close > 0:
                        candles.append(c)
                except (ValueError, IndexError):
                    pass
    except Exception:
        pass
    return candles


# ═══════════════════════════════════════════════════
#  核心逻辑（不可修改——只调引擎）
# ═══════════════════════════════════════════════════

@dataclass
class ScanResult:
    code: str
    name: str
    pa_status: str      # PASS/WEAK/FAIL
    vpa_status: str
    share_status: str
    passed: int
    decision: str
    detail: dict        # 引擎原始输出，供外部消费


def scan_one(code: str, name: str, shares: List[float]) -> ScanResult:
    """对单只ETF执行完整三票制分析。"""
    candles = fetch_candles(code, name)
    result = quick_analysis(
        etf_code=code,
        candles=candles,
        share_changes=shares,
    )
    tickets = result.get("tickets", {})
    return ScanResult(
        code=code,
        name=name,
        pa_status=tickets.get("pa", {}).get("status", "?"),
        vpa_status=tickets.get("vpa", {}).get("status", "?"),
        share_status=tickets.get("share", {}).get("status", "?"),
        passed=tickets.get("passed", 0),
        decision=tickets.get("decision", "?"),
        detail=result,
    )


def scan_all(targets: Optional[List[str]] = None) -> List[ScanResult]:
    """全量或指定标的扫描。份额：实时优先 → 缓存兜底。"""
    shares, is_live = load_share_live()
    if not is_live and not targets:
        print("[INFO] 使用缓存份额数据（非实时）", file=sys.stderr)
    if targets is None:
        targets = list(ETF_POOL.keys())

    results: List[ScanResult] = []
    for code in targets:
        name = ETF_POOL.get(code, "")
        if not name:
            print(f"[SKIP] {code} 不在备选池中", file=sys.stderr)
            continue
        share_data = shares.get(code, [0.0, 0.0, 0.0, 0.0])
        try:
            sr = scan_one(code, name, share_data)
        except Exception as e:
            print(f"[ERR] {name}({code}): {e}", file=sys.stderr)
            sr = ScanResult(code, name, "?", "?", "?", 0, "引擎异常", {})
        results.append(sr)
    return results


# ═══════════════════════════════════════════════════
#  输出格式化（不可修改）
# ═══════════════════════════════════════════════════

def print_brief(results: List[ScanResult]) -> None:
    """精简表格输出。引擎返回的status已经是emoji(✅⚠️❌)。"""
    print(f"{'ETF':<14} {'①PA':>8} {'②VPA':>8} {'③份额':>8} {'通过':>4} {'决策':<16}")
    print("-" * 64)
    for r in results:
        print(f"{r.name:<14} {r.pa_status:>8} {r.vpa_status:>8} {r.share_status:>8} {r.passed:>4} {r.decision:<16}")

def print_json(results: List[ScanResult]) -> None:
    """结构化JSON输出。"""
    output = [{
        "code": r.code,
        "name": r.name,
        "pa": r.pa_status,
        "vpa": r.vpa_status,
        "share": r.share_status,
        "passed": r.passed,
        "decision": r.decision,
    } for r in results]
    print(json.dumps(output, ensure_ascii=False, indent=2))


# ═══════════════════════════════════════════════════
#  main
# ═══════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="V5三票制全量扫描")
    parser.add_argument("codes", nargs="*", help="指定ETF代码（不传=全量）")
    parser.add_argument("--brief", action="store_true", help="精简表格输出")
    parser.add_argument("--json", action="store_true", help="JSON输出（仅三票结果）")
    parser.add_argument("--detail", action="store_true", help="JSON输出（含引擎完整detail）")
    args = parser.parse_args()

    targets = [c for c in args.codes if c in ETF_POOL] if args.codes else None
    if args.codes and not targets:
        print(f"[ERR] 所有指定代码都不在备选池中: {args.codes}", file=sys.stderr)
        sys.exit(1)

    results = scan_all(targets)

    if args.detail:
        print(json.dumps([r.detail for r in results], ensure_ascii=False, indent=2))
    elif args.json:
        print_json(results)
    else:
        print_brief(results)
        if not args.brief:
            buy = [r for r in results if r.passed >= 3]
            obs = [r for r in results if r.passed == 2]
            print(f"\n📊 扫描{len(results)}只，{len(buy)}只可入场，{len(obs)}只观察，其余不入场")


if __name__ == "__main__":
    main()
