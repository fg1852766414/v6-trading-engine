"""
config.py — 全局可配参数
=======================
所有关键参数集中管理，零硬编码。
每个 Engine 通过构造函数注入 config，不直接引用全局常量。

使用方式：
    from scripts.trading_engine.config import EngineConfig
    cfg = EngineConfig.default()           # 使用 V5 默认参数
    cfg.position.single_direction_cap = 0.35  # 按需覆盖
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


# ============================================================================
# 各模块独立配置
# ============================================================================

@dataclass
class ModeConfig:
    """Phase 0 行情模式识别参数"""

    # --- 标的类型分类 ---
    event_driven_codes: List[str] = field(default_factory=lambda: [
        "588200.SH", "512480", "159995", "589130",   # 半导体
        "159992.SZ", "515120", "517380", "159858",   # 创新药
        "562500.SH", "159530.SZ",                     # 机器人
        "515880.SH",                                   # 通信
        "512710.SH", "512660", "512680",              # 军工
    ])
    valuation_codes: List[str] = field(default_factory=lambda: [
        "512880.SH", "512000", "159841", "512570",
        "159326.SZ", "561380", "560390",
        "159611.SZ", "562550",
        "512400.SH",
        "518880.SH",
    ])
    broad_index_codes: List[str] = field(default_factory=lambda: [
        "510300.SH", "510050.SH", "510500.SH", "512100.SH",
    ])

    # --- 大盘状态识别 ---
    death_cross_days: int = 15          # MA20/MA60 死叉检查回看天数
    death_cross_ratio: float = 0.80     # 死叉确认比例（天数*ratio）
    weekly_decline_bars: int = 25       # 连续下跌检查 K 线数
    weekly_decline_weeks: int = 5       # 连续下跌周数
    below_year_line_days: int = 10      # 跌破年线持续天数
    below_year_line_ratio: float = 0.80
    deep_bear_drawdown: float = 0.20    # 深熊回撤阈值
    deep_bear_weeks: int = 8            # 深熊连续下跌周数
    deep_bear_bars: int = 40            # 对应 K 线数

    # --- 黑天鹅 ---
    black_swan_daily_pct: float = -5.0  # 单日跌幅触发
    black_swan_portfolio_pct: float = -7.0  # 组合净值触发

    # --- 模式 D 现金阈值 ---
    mode_d_cash_ratio: float = 0.50


@dataclass
class TechnicalConfig:
    """技术指标计算参数"""

    # 均线周期
    ma_periods: List[int] = field(default_factory=lambda: [5, 10, 20, 60, 120])

    # MACD
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    macd_golden_lookback: int = 15      # 金叉检查回看 bar 数
    macd_dead_lookback: int = 15

    # RSI
    rsi_period: int = 14

    # 分型检测参数
    # 缠论分型永远由 3 根 K 线定义（左/中/右），不可配置。
    # fractal_search_depth 控制从最新 K 线往回找多远（避免找到太旧的分型）
    fractal_search_depth: int = 3
    fractal_min_quality: int = 2         # 真分型最低质量分

    # 底分型质量验证
    bottom_near_support_pct: float = 0.03   # 靠近支撑位的误差（3%）
    bottom_volume_ma_period: int = 5        # 均量参考周期
    bottom_right_leg_pct: float = 0.01      # 右腿收盘超左腿高点 %

    # 顶分型质量验证
    top_near_resistance_pct: float = 0.03
    top_volume_surge_mult: float = 1.5      # 放量倍数
    top_right_leg_pct: float = -0.01        # 右腿收盘低左腿低点 %

    # 站稳 MA20 判断
    mode_a_stable_days: int = 10            # 模式 A 需要的天数观察窗口
    mode_a_stable_ratio: float = 0.80       # 站上 MA20 的天数比例
    mode_b_stable_days: int = 2             # 模式 B 窗口
    mode_b_stable_ratio: float = 1.0        # 模式 B 需 100%

    # 支撑/阻力位查找
    support_lookback: int = 60              # 前低/前高回看天数


@dataclass
class TicketConfig:
    """三票制判定参数"""

    # --- 票① PA 底分型 ---
    pa_min_quality: int = 2                 # 真底最低分

    # --- 票② VPA 量价 ---
    vpa_volume_ma_period: int = 5           # 均量参考周期
    vpa_shrink_days: int = 2                # 缩量企稳所需天数
    vpa_surge_volume_mult: float = 1.2      # 放量倍数
    vpa_surge_price_pct: float = 0.02       # 放量涨幅 %（2%）
    vpa_weak_margin: float = 0.01           # 弱信号判定误差（1%）

    # --- 票③ 份额验证 ---
    share_consecutive_days: int = 3         # 连续净申购天数
    share_premium_warning: float = 2.0      # 溢价警告 %
    share_trigger_days: int = 3             # 3 日扳机
    share_insurance_days: int = 5           # 5 日保险


@dataclass
class PositionConfig:
    """仓位计算参数"""

    # PE 分位 → 建议仓位（区间中值）
    # 含义：PE 越低仓位越高，每个区间取中值作为确定性建议
    #   区间边界      PE 分位范围        仓位范围      中值     操作
    #   pe_max=20    PE < 20%         80% - 100%    90%      打满
    #   pe 20-40     PE [20%, 40%)    40% - 60%     50%      正常持有
    #   pe 40-60     PE [40%, 60%)    20% - 30%     25%      轻仓或观望
    #   pe 60-80     PE [60%, 80%)     0% - 10%      5%      减仓窗口
    #   pe_min=80    PE >= 80%         0%             0%      清仓
    pe_position_table: List[dict] = field(default_factory=lambda: [
        {"pe_max": 20,  "position": 90, "action": "打满"},
        {"pe_min": 20,  "pe_max": 40,  "position": 50, "action": "正常持有"},
        {"pe_min": 40,  "pe_max": 60,  "position": 25, "action": "轻仓或观望"},
        {"pe_min": 60,  "pe_max": 80,  "position":  5, "action": "减仓窗口"},
        {"pe_min": 80,  "position":  0, "action": "清仓"},
    ])

    # 基础仓位和理论上限
    base_position: float = 30.0             # 三票通过后的基础仓位 %
    theoretical_max: float = 70.0           # 理论仓位上限 %

    # 加分项权重
    bonus_weights: Dict[str, float] = field(default_factory=lambda: {
        "MACD金叉": 20.0,
        "站上MA5": 15.0,
        "PE/PS低估": 15.0,
        "催化剂": 10.0,
    })

    # PE 低估阈值（用于 bonus）
    pe_undervalue_a: float = 40.0           # 模式 A: PE < 40%
    ps_undervalue_b: float = 50.0           # 模式 B: PS < 50%

    # 弱点位折扣
    weakness_discount: float = 0.50         # 仓位减半

    # 单方向/总权益/现金约束
    single_direction_cap: float = 30.0      # 单一主题上限 %
    total_equity_cap: float = 80.0          # 权益总仓位上限 %
    cash_floor: float = 20.0                # 现金下限 %

    # 左侧底仓
    left_side_pe_threshold: float = 15.0    # PE < 15% 触发
    left_side_batches: List[float] = field(default_factory=lambda: [0.40, 0.70, 1.0])

    # PE 分位行业可靠性调整
    pe_weight_adjust: Dict[str, float] = field(default_factory=lambda: {
        "broad_index": 1.0,
        "consumer": 0.875,
        "cyclical": 0.75,
        "growth": 0.625,
    })

    # 2/3 边界底仓比例
    boundary_base_ratio: float = 0.25       # 基础仓位 × 25%
    boundary_stop_loss_pct: float = 0.05    # 止损 5%


@dataclass
class RiskConfig:
    """组合风控参数"""

    # 回撤级别: [(drawdown_pct, level, description, force_equity_cap)]
    drawdown_levels: List[Tuple[float, str, str, float]] = field(default_factory=lambda: [
        (5, "normal", "正常回撤", 80),
        (10, "warning", "检查高估值标的", 40),
        (15, "critical", "强制降至20%", 20),
        (20, "critical", "全部清仓→模式D", 0),
    ])

    single_max_loss: float = -0.15          # 单标的强制减仓线

    # 再平衡
    rebalance_deviation: float = 0.20       # 偏离 >20% 触发调仓


@dataclass
class FeeConfig:
    """费率检查参数"""

    penalty_rate: float = 0.015             # 1.5% 赎回费
    mode_b_threshold: float = 0.03          # 模式 B 放宽到 3%
    profit_skip: float = 0.15               # 浮盈超 15% 直接卖
    holding_days_free: int = 7              # 免赎回费持有天数
    default_probability: float = 0.30       # 默认触发概率（无历史数据时）
    max_drawdown_prob_weight: float = 0.50  # 近1年最大回撤取 50% 做概率

    # 支撑位优先级参数
    fib_level: float = 0.618                # 斐波那契回撤位
    support_lookback: int = 60              # 近 N 日前低


@dataclass
class ETFConfig:
    """ETF 选标参数"""

    # ETF 选标池：{主题: {code, scale_亿, alternatives}}
    pool: Dict[str, dict] = field(default_factory=lambda: {
        "半导体":  {"code": "588200.SH", "scale": 656, "alts": ["512480", "159995", "589130"]},
        "芯片":    {"code": "588200.SH", "scale": 656, "alts": ["512480", "159995", "589130"]},
        "创新药":  {"code": "159992.SZ", "scale": 168, "alts": ["515120", "517380", "159858"]},
        "证券":    {"code": "512880.SH", "scale": 620, "alts": ["512000", "159841", "512570"]},
        "机器人":  {"code": "562500.SH", "scale": 187, "alts": ["159530"]},
        "机器人纯": {"code": "159530.SZ", "scale": 186, "alts": ["562500"]},
        "电网设备": {"code": "159326.SZ", "scale": 203, "alts": ["561380", "560390"]},
        "电力":    {"code": "159611.SZ", "scale": 87,  "alts": ["562550"]},
        "通信":    {"code": "515880.SH", "scale": 516, "alts": []},
        "军工":    {"code": "512710.SH", "scale": 76,  "alts": ["512660", "512680"]},
        "有色金属": {"code": "512400.SH", "scale": 222, "alts": []},
        "黄金":    {"code": "518880.SH", "scale": 894, "alts": []},
    })

    # 场外联接基金映射
    otc_link_map: Dict[str, List[str]] = field(default_factory=lambda: {
        "机器人": ["014880", "020972"],
        "创新药": ["014564"],
        "证券":   ["025857"],
    })


@dataclass
class ExitConfig:
    """清仓/减仓信号参数"""

    # 硬清仓条件
    hard_exit_top_drop_pct: float = 3.0       # 顶分型 + 当日跌幅>N%
    hard_exit_share_days: int = 5             # 份额连续N日净赎回

    # 软清仓条件（触发费率检查）
    soft_exit_pe_threshold: float = 80.0     # PE >= N% 分位
    soft_exit_share_days: int = 5             # 份额连续N日净赎回

    # 清仓执行
    exit_batches: int = 3                     # 分N次降仓
    exit_interval_days: int = 7              # 每次间隔N天

    # 减仓信号
    reduce_pe_threshold: float = 60.0         # PE >= N% 分位（模式A）
    reduce_share_days: int = 3                # 份额连续N日净赎回
    reduce_break_ma: str = "weekly_sma20"     # 模式A破周线SMA20
    reduce_break_ma_b: str = "daily_ma10"     # 模式B破日线MA10

    # 大盘层面减仓触发
    market_drop_pct: float = 3.0              # 大盘单日暴跌>N%


@dataclass
class BearMarketConfig:
    """模式C 熊市防御 + 模式F 黑天鹅参数"""

    # 熊市仓位上限表: [(阶段, hs300表现描述, 权益上限%, 操作)]
    bear_stages: List[dict] = field(default_factory=lambda: [
        {"stage": "initial",  "desc": "MA20死叉MA60",        "equity_cap": 40, "action": "减仓至40%"},
        {"stage": "mid",      "desc": "连跌5周+跌破年线",     "equity_cap": 20, "action": "减仓至20%"},
        {"stage": "deep",     "desc": "从高点回撤>20%",       "equity_cap": 10, "action": "仅保留底仓"},
        {"stage": "bottom",   "desc": "PE<15%分位+缩量企稳", "equity_cap": 0,  "action": "不加仓，等待模式切换"},
    ])

    # 熊市回撤阈值 → 阶段映射（从 bear_stages 的 equity_cap 反推）
    # 配置值：{最小回撤%(含): 阶段名}
    bear_drawdown_thresholds: Dict[float, str] = field(default_factory=lambda: {
        20: "deep",     # 回撤≥20% → 深熊
        10: "mid",      # 回撤≥10% → 中熊
        0: "initial",   # 回撤<10% → 初期
    })

    # 熊市标的处理: 模式A标的
    bear_a_rules: List[dict] = field(default_factory=lambda: [
        {"pe_min": 60,  "action": "清仓"},
        {"pe_min": 40,  "pe_max": 60, "action": "减至20%"},
        {"pe_max": 20,  "action": "可持有底仓10-15%"},
    ])

    # 熊市标的处理: 模式B标的
    bear_b_rules: List[dict] = field(default_factory=lambda: [
        {"break_level": "MA60",          "action": "减至10%"},
        {"break_level": "MA20",           "action": "减至20%"},
        {"break_level": "MACD死叉+份额流出", "action": "清仓"},
    ])

    # 熊市现金分配
    bear_cash_allocation: Dict[str, float] = field(default_factory=lambda: {
        "money_market": 0.50,   # 货币基金 T+0
        "short_bond": 0.30,     # 短债基金 1-3个月
        "reserve": 0.20,        # 预留子弹
    })

    # 熊市结束信号
    bear_exit_conditions: List[str] = field(default_factory=lambda: [
        "沪深300周线MA20重新上穿MA60（金叉）",
        "连续2周收盘价在MA20上方",
        "沪深300周线MACD金叉",
        "至少1个持仓标的的PE/PS分位<30%",
    ])

    # 熊市退出操作
    bear_exit_batches: List[float] = field(default_factory=lambda: [0.30, 0.50, 1.0])
    bear_exit_interval_weeks: int = 1

    # --- 模式F 黑天鹅3级触发 ---
    # 1级: 沪深300单日跌幅>N%
    black_swan_level1_daily_pct: float = -5.0
    black_swan_level1_action: str = "所有标的减仓至当前仓位的50%"
    black_swan_level1_ratio: float = 0.50

    # 2级: 组合单日净值跌幅>N%
    black_swan_level2_portfolio_pct: float = -7.0
    black_swan_level2_action: str = "所有标的减仓至当前仓位的30%"
    black_swan_level2_ratio: float = 0.30

    # 3级: 市场熔断/千股跌停
    black_swan_level3_action: str = "全部清仓，100%转货基"
    black_swan_level3_ratio: float = 0.0

    # 黑天鹅恢复条件
    black_swan_recovery_policy: List[str] = field(default_factory=lambda: [
        "重大政策利好（降准>1%/暂停IPO/国家队入场）+市场连续2日企稳",
        "连续5个交易日不出现单日跌幅>3% + 至少1个标的PE/PS<20%分位",
    ])
    black_swan_recovery_batches: List[float] = field(default_factory=lambda: [0.20, 0.40, 0.60])
    black_swan_recovery_interval_days: int = 3


@dataclass
class MacroConfig:
    """模式E 宏观叠加层参数"""

    # 政策事件等级 -> 操作映射
    # 等级判定由AI完成，引擎只做"等级->操作"的确定性映射
    policy_event_map: Dict[str, dict] = field(default_factory=lambda: {
        "S": {"effect": "全面利好，改变趋势", "actions": ["暂停所有减仓信号7天", "建仓上限+10%"]},
        "A": {"effect": "中期利好",           "actions": ["暂停减仓3天", "建仓上限+5%"]},
        "B": {"effect": "结构性利好",         "actions": ["对应标的权重+10%"]},
        "C": {"effect": "短期情绪",           "actions": ["不改变仓位，仅记录"]},
        "X": {"effect": "结构性利空",         "actions": ["立即触发清仓检查，不等7天"]},
    })

    # 流动性指标阈值
    liquidity_m2_up_trend_months: int = 2       # M2连续N月回升
    liquidity_m2_down_trend_months: int = 2
    liquidity_social_financial_months: int = 2  # 社融连续N月超/低预期

    # 北向资金信号阈值（亿元/日）
    northbound_inflow_days: int = 5
    northbound_inflow_per_day: float = 50.0
    northbound_outflow_days: int = 5
    northbound_outflow_per_day: float = 50.0
    northbound_panic_single_day: float = 100.0
    northbound_surge_single_day: float = 100.0


# ============================================================================
# 总配置
# ============================================================================

@dataclass
class EngineConfig:
    """全部引擎的配置聚合体"""
    mode: ModeConfig = field(default_factory=ModeConfig)
    technical: TechnicalConfig = field(default_factory=TechnicalConfig)
    ticket: TicketConfig = field(default_factory=TicketConfig)
    position: PositionConfig = field(default_factory=PositionConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    fee: FeeConfig = field(default_factory=FeeConfig)
    etf: ETFConfig = field(default_factory=ETFConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    bear: BearMarketConfig = field(default_factory=BearMarketConfig)
    macro: MacroConfig = field(default_factory=MacroConfig)

    @classmethod
    def default(cls) -> "EngineConfig":
        """返回 V5 默认配置"""
        return cls()
