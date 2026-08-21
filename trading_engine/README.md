# trading_engine — V5 确定性交易分析引擎

> 所有基于规则/公式/查表的计算均在此实现。零 AI 幻觉风险，输入数据 → 输出结构化决策。

## 包结构

```
scripts/trading_engine/
├── __init__.py           # 入口 + quick_analysis() 便捷函数
├── config.py             # ★ 全局可配参数（所有 Engine 通过 config 注入）
├── models.py             # 数据结构定义（枚举 + dataclass）
├── utils.py              # 数学工具（sma / ema / rma）
│
├── mode_engine.py        # Phase 0  行情模式识别（A-F，含黑天鹅分级 + 周线聚合）
├── technical_engine.py   #          技术指标（MA / MACD / RSI / PA 分型）
├── ticket_engine.py      #          三票制入场判定（含 2/3 边界底仓 + 3日/5日保险）
├── position_engine.py    #          仓位计算（PE 阶梯查表 + 加分制 + 分批左侧底仓）
├── risk_engine.py        #          组合风控（回撤 / 集中度 / 再平衡）
├── exit_engine.py        #          退出信号（硬/软清仓 + 熊市上限 + 黑天鹅分级操作）
├── etf_selector.py       #          ETF 主题 → 首选代码 + 联接基金映射
└── fee_engine.py         #          7 天赎回费检查（含 T+1 场外基金约束）
```

**依赖图**（零循环）：

```
config / models / utils     ← 无依赖，所有模块的底层

mode_engine         ← config + models + utils
technical_engine    ← config + models + utils
ticket_engine       ← config + models + technical_engine
position_engine     ← config + models
risk_engine         ← config + models
etf_selector        ← config + models
fee_engine          ← config + models + utils
exit_engine         ← config + models + technical_engine + fee_engine

__init__            ← 全部模块 + quick_analysis()
```

## 快速开始

```python
from scripts.trading_engine import (
    EngineConfig, ModeEngine, TechnicalEngine, TicketEngine,
    PositionEngine, RiskEngine, ExitEngine, ETFSelector, FeeEngine,
    Candle, quick_analysis,
)

# === 方式一：一键分析（推荐） ===

candles = [Candle(date="...", open=1.0, high=1.02, low=0.99, close=1.01, volume=1e6), ...]
shares = [0.5, 1.2, 0.8, 0.3, 0.6]  # 逐日份额变化率 %

result = quick_analysis(
    etf_code="588200.SH",
    candles=candles,
    share_changes=shares,
    hs300_candles=candles,           # 沪深 300 K 线（可选）
    pe_percentile=35.2,               # PE 分位（可选）
    premium=1.5,                      # 溢价率（可选）
    has_catalyst=False,               # AI 判断的催化剂
    cash_ratio=0.4,
    current_positions={               # 持仓信息（可选，触发退出/费率检查）
        "588200": {"actual_pct": 45, "entry_price": 1.15, "holding_days": 4},
    },
    positions_all_triggered=False,    # 模式 D 条件 3
    left_side_batch=0,                # 左侧底仓当前批数
)

# 输出结构
print(result["mode"])             # "B"
print(result["tickets"])          # {pa, vpa, share, passed, decision, boundary_position}
print(result["final_position"])   # 65.0 (%)
print(result["bonus_items"])      # {"MACD金叉": 20, "站上MA5": 15, ...}
print(result["fee_check"])        # [{"code": "588200", "sell_now": True, "reason": "..."}]
print(result["exit_signals"])     # [{"code": "588200", "hard": False, "soft": True, ...}]
print(result["risk_alerts"])      # [...]

# === 方式二：分步调用 ===

# Phase 0: 模式识别（含黑天鹅分级）
mode_eng = ModeEngine()
mode = mode_eng.determine_mode("588200.SH", hs300_candles)
print(mode.active_mode)          # MarketMode.A / B / C / D / F
print(mode.black_swan_level)     # "none" / "level1" / "level2" / "level3"

# 技术指标
tech_eng = TechnicalEngine()
indicators = tech_eng.analyze(candles, mode.active_mode)
print(indicators["rsi14"])              # 62.3
print(indicators["macd_golden_cross"])  # True

# 三票制（含 2/3 边界底仓）
ticket_eng = TicketEngine()
tickets = ticket_eng.evaluate(candles, share_changes=[0.5, 1.2, 0.8], has_catalyst=True)
print(tickets.can_enter, tickets.decision)       # 可入场 / 观察
print(tickets.boundary_position)                  # 2/3 边界底仓 %

# 仓位计算（含分批左侧底仓）
pos_eng = PositionEngine()
advice = pos_eng.pe_to_position(10)               # PE 阶梯查表（区间中值）
print(advice.suggested_pct, advice.action)        # 90.0, "打满"
lb = pos_eng.left_side_base_position(10, False, False, current_batch=0)
print(lb.action)                                  # "左侧底仓第1批 (×40%, 36.0%)"

# 退出信号（整合硬/软清仓 + 熊市上限 + 黑天鹅分级）
exit_eng = ExitEngine()
dec = exit_eng.evaluate("588200", candles, shares, MarketMode.A,
                         pe_percentile=85, entry_price=1.15)
print(dec.should_exit, dec.hard_triggered)  # True / False
print(dec.summary)                           # "软清仓: PE高估, 大盘系统性破位"

# 熊市仓位上限
cap = exit_eng.bear_market_equity_cap(exit_eng.bear_market_cap(15))
print(f"熊市权益上限: {cap:.0%}")             # 20%

# 黑天鹅分级操作
action = exit_eng.black_swan_action(hs300_daily=-6.5)
print(action.level.value, action.action)      # "level1", "减至当前50%"

# 风控
risk_eng = RiskEngine()
dd = risk_eng.check_drawdown(-12)
print(dd.level.value)  # "warning"

# ETF 选标
sel = ETFSelector()
etf = sel.select("芯片")
print(etf.code)       # "588200.SH"
print(sel.get_fund_code("机器人"))  # "014880"（联接基金）

# 费率检查（含 T+1 场外基金约束）
fee_eng = FeeEngine()
sell_now, reason = fee_eng.should_sell_now(
    current_price=1.234, support_level=1.10,
    holding_days=3, profit_pct=0.05,
)
print(sell_now, reason)
```

## 参数配置

所有关键参数通过 `EngineConfig` 集中管理。使用 `EngineConfig.default()` 获取 V5 默认值，按需覆盖。

```python
cfg = EngineConfig.default()

# === 行情模式识别 ===
cfg.mode.black_swan_daily_pct = -6.0       # 黑天鹅触发从 -5% 改 -6%
cfg.mode.weekly_decline_weeks = 6          # 连续下跌周数从 5 改 6
cfg.mode.deep_bear_drawdown = 0.25         # 深熊回撤从 20% 改 25%

# === 技术指标 ===
cfg.technical.macd_fast = 10               # MACD 快线周期
cfg.technical.rsi_period = 10              # RSI 周期
cfg.technical.fractal_search_depth = 5     # 分型搜索深度（不影响分型定义，仅影响回看范围）

# === 三票制 ===
cfg.ticket.vpa_surge_volume_mult = 1.5     # 放量倍数从 1.2x 改 1.5x
cfg.ticket.vpa_surge_price_pct = 0.03      # 放量涨幅从 2% 改 3%
cfg.ticket.share_trigger_days = 4          # 3 日扳机从 3 改 4
cfg.ticket.share_insurance_days = 7        # 5 日保险从 5 改 7

# === 仓位计算 ===
cfg.position.base_position = 25.0          # 基础仓位从 30% 改 25%
cfg.position.theoretical_max = 80.0        # 理论仓位上限从 70% 改 80%
cfg.position.single_direction_cap = 35.0   # 单方向上限从 30% 改 35%
cfg.position.total_equity_cap = 85.0       # 总权益上限从 80% 改 85%
cfg.position.bonus_weights["MACD金叉"] = 25.0  # 金叉加分从 20 改 25
# PE 分位 → 仓位（区间中值阶梯函数）
cfg.position.pe_position_table[0]["position"] = 95  # PE<20% 仓位从 90 改 95

# === 风控 ===
cfg.risk.drawdown_levels[0] = (3, "normal", "正常回撤", 80)  # 第一档从 5% 改 3%
cfg.risk.single_max_loss = -0.20           # 单标的最大亏损从 -15% 改 -20%

# === 费率 ===
cfg.fee.penalty_rate = 0.01                # 赎回费从 1.5% 改 1%
cfg.fee.mode_b_threshold = 0.04            # 模式 B 阈值从 3% 改 4%
cfg.fee.holding_days_free = 5              # 免赎回费天数从 7 改 5

# === ETF 池 ===
cfg.etf.pool["新主题"] = {"code": "000001.SH", "scale": 100, "alts": []}
cfg.etf.otc_link_map["新主题"] = ["000001", "000002"]

# 注入自定义 config
pos_eng = PositionEngine(cfg)
exit_eng = ExitEngine(cfg)
```

## 各模块 API

### ModeEngine — 行情模式识别

| 方法 | 输入 | 输出 |
|:-----|:-----|:-----|
| `daily_to_weekly(daily_candles)` | 日 K 列表 | 聚合后的周 K 列表 |
| `classify_etf(code)` | ETF 代码 | `ETFType.VALUATION` / `EVENT_DRIVEN` |
| `identify_market_state(hs300, ...)` | 沪深 300 K 线 + positions_all_triggered | `("normal", [])` / `("bear", [C])` |
| `determine_mode(code, hs300, ...)` | ETF + 大盘 K 线 + black_swan_level | `ModeResult(active_mode, etf_type, black_swan_level, reasoning)` |

> **模式 E（宏观叠加层）**：由 AI 层根据新闻语义判断后叠加，引擎不处理。策略包括政策事件分级(S/A/B/C/X)、M2/社融/DR007 流动性指标、北向资金信号。

### TechnicalEngine — 技术指标

| 方法 | 说明 |
|:-----|:-----|
| `calc_all_ma(closes)` | 返回 `TechnicalIndicators`（ma5/10/20/60/120） |
| `calc_macd(closes)` | 返回 `(DIF[], DEA[], histogram[])` |
| `check_macd_golden_cross(dif, dea)` | 返回 `(has_cross, bars_ago)` |
| `calc_rsi(closes)` | 返回 RSI 值（默认 14 周期） |
| `detect_fractal(candles)` | 返回 `FractalResult`（顶/底/无）— 缠论定义永远是 3 根 K 线 |
| `validate_bottom_fractal(candles, fractal)` | 底分型质量验证，0-3 分 |
| `validate_top_fractal(candles, fractal)` | 顶分型质量验证，0-3 分 |
| `analyze(candles, mode)` | 一键返回技术面 dict |

### TicketEngine — 三票制

| 方法 | 说明 |
|:-----|:-----|
| `ticket_pa(candles)` | 票① PA 底分型 → `TicketResult(PASS/WEAK/FAIL)` |
| `ticket_vpa(candles)` | 票② VPA 量价确认 |
| `ticket_share(changes, premium)` | 票③ 份额验证（输出 3 日扳机 + 5 日保险） |
| `evaluate(candles, shares, premium, has_catalyst)` | 综合判定 → `ThreeTicketDecision`（含 2/3 边界底仓） |

### PositionEngine — 仓位计算

| 方法 | 说明 |
|:-----|:-----|
| `pe_to_position(pe_percentile)` | PE 阶梯查表（区间中值）→ `PositionAdvice` |
| `calc_bonus(macd, ma5, pe_low, catalyst)` | 返回加分 dict |
| `calculate(decision, bonuses, current_eq)` | 最终仓位计算（加分 + 弱点位折扣 + 风控约束） |
| `left_side_base_position(pe, golden, above_ma20, current_batch)` | 分批左侧底仓（0=第1批×40%, 1=第2批×70%, 2=第3批×100%） |

### ExitEngine — 退出信号 ★新增

| 方法 | 说明 |
|:-----|:-----|
| `evaluate(code, candles, shares, mode, pe, ...)` | 综合退出评估 → `ExitDecision` |
| `adjust_position_by_insurance(theo, t3d, t5d)` | 3 日扳机×5 日保险仓位调整 |
| `bear_market_cap(drawdown)` | 熊市阶段判定 → `BearStage` |
| `bear_market_equity_cap(stage)` | 熊市权益上限（初期 40% / 中期 20% / 深熊 10%） |
| `bear_market_stock_cap(pe, type, above_ma20, above_ma60)` | 熊市单标的仓位上限 |
| `black_swan_action(hs300_daily, portfolio_daily, circuit_breaker)` | 黑天鹅分级操作 → `BlackSwanAction` |

**退出信号体系：**

| 硬清仓（立即执行，不等 7 天） | 软清仓（触发费率检查） |
|:-----|:-----|
| ① 顶分型确认 + 放量下跌 >3%（真顶验证通过） | ① PE >= 80%（模式 A） |
| ② 产业逻辑被证伪（AI 传入） | ② 大盘系统性破位（MA20/MA60+布林下轨） |
| ③ 份额连续 5 日净赎回 | ③ 份额连续 5 日净赎回（模式 B） |

### RiskEngine — 组合风控

| 方法 | 说明 |
|:-----|:-----|
| `check_drawdown(pct)` | 回撤级别 → `RiskCheck` |
| `check_single_direction(ratio, name)` | 方向集中度 |
| `check_cash_ratio(ratio)` | 现金比例 |
| `check_single_position_loss(entry, current)` | 单标的亏损检查 |
| `rebalance_check(positions, cash, mode)` | 月度再平衡 |
| `comprehensive_check(total_equity_ratio, cash_ratio, ...)` | 一键综合（参数统一为比值 0-1） |

### ETFSelector — ETF 选标

| 方法 | 说明 |
|:-----|:-----|
| `select(theme)` | "芯片" → `ETFInfo(code="588200.SH", scale=656)` |
| `get_fund_code(theme)` | 获取场外联接基金代码 |
| `is_preferred(code)` | 是否为主题首选 |
| `find_alternative(code)` | 返回更大规模替代品提示 |
| `list_all()` | 列出所有 ETF |

### FeeEngine — 费率检查

| 方法 | 说明 |
|:-----|:-----|
| `should_sell_now(price, support, days, profit, mode)` | 返回 `(sell_now, reason)` |
| `find_support_level(candles, ma120)` | 支撑位（斐波那契 61.8% > 前低 > MA120） |

> 场外基金约束：T+1 净值确认，`holding_days` 从份额确认日起算。联接基金映射见 `etf_selector.get_fund_code()`。

## 数据结构

| 类型 | 关键字段 |
|:-----|:---------|
| `Candle` | `date, open, high, low, close, volume` |
| `FractalResult` | `kind(top/bottom/none), index, quality_score(0-3), is_valid, checks` |
| `ModeResult` | `etf_code, etf_type, base_mode, overlay_modes, active_mode, black_swan_level, reasoning` |
| `TicketResult` | `status(PASS/WEAK/FAIL), detail, value` |
| `ThreeTicketDecision` | `ticket_pa, ticket_vpa, ticket_share, passed_count, can_enter, final_position, boundary_position` |
| `PositionAdvice` | `suggested_pct, pe_percentile, action` |
| `RiskCheck` | `level(normal/warning/critical), alerts, action_required` |
| `ETFInfo` | `code, theme, scale_billion, alternatives` |
| `ExitSignal` | `exit_type, triggers[], target_ratio, batch_count, batch_interval_days, need_fee_check` |
| `ExitDecision` | `code, signals[], hard_triggered, soft_triggered, should_exit, hard_reasons, soft_reasons, fee_reason` |
| `BlackSwanAction` | `level, target_ratio, action, force_mode_d` |
| `MacroOverlay` | `policy_level, policy_actions[], position_cap_adjust, pause_reduce_days, force_exit_check` |

## 枚举

| 枚举 | 值 |
|:-----|:---|
| `MarketMode` | `A` / `B` / `C` / `D` / `E` / `F` |
| `ETFType` | `valuation` / `event` |
| `FractalKind` | `top` / `bottom` / `none` |
| `TicketStatus` | `PASS(✅)` / `WEAK(⚠️)` / `FAIL(❌)` |
| `SignalLevel` | `normal` / `warning` / `critical` |
| `BlackSwanLevel` | `none` / `level1` / `level2` / `level3` |
| `ExitType` | `hard_clear` / `soft_clear` / `reduce` / `none` |
| `BearStage` | `initial` / `mid` / `deep` / `bottom` |

## 设计原则

1. **纯函数** — 所有 Engine 方法输入数据 → 输出结果，不发起网络请求、不读文件、不访问环境变量
2. **参数外置** — 零硬编码，所有阈值/权重/比例在 `config.py` 中定义，可通过 `EngineConfig` 覆盖
3. **模块解耦** — 每个 Engine 只依赖 `config` + `models` + `utils`，互相不引用
4. **AI 只在语义层** — 引擎覆盖所有公式/查表/阈值判定 + 退出信号体系，AI 层仅处理新闻解读、产业逻辑、政策定级、最终决策合成
5. **数据不猜测** — 周线用真正的 K 线聚合（`daily_to_weekly`），PE 用区间中值阶梯函数（不线性插值），分型永远 3 根 K 线（缠论定义）

## 运行测试

```bash
# 独立测试脚本
python scripts/test_trading_engine.py

# 包自测
python scripts/trading_engine/__init__.py

# Python 交互
python -c "
import sys; sys.path.insert(0, '.')
from scripts.trading_engine import ExitEngine, Candle, MarketMode
e = ExitEngine()
# ...
"
```

## 版本

| 版本 | 日期 | 变更 |
|:----|:----|:-----|
| V5.0 | 2026-07-10 | 从 V4 单文件 ~650 行重构为模块包 |
| V5.1 | 2026-07-10 | P0 修复: PE 阶梯函数 + 周线聚合 + 模式 D 条件 + 单位一致 + 费率集成；P1 新增: ExitEngine + 熊市上限 + 黑天鹅分级 + 2/3 边界底仓 + 3日/5日保险 |
