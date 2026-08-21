"""
models.py — 数据结构
=====================
所有 dataclass / enum 集中定义，零循环依赖。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class MarketMode(str, Enum):
    """行情模式"""
    A = "A"  # 估值回归
    B = "B"  # 事件驱动
    C = "C"  # 熊市防御
    D = "D"  # 空仓
    E = "E"  # 宏观叠加
    F = "F"  # 黑天鹅熔断


class ETFType(str, Enum):
    VALUATION = "valuation"
    EVENT_DRIVEN = "event"


class FractalKind(str, Enum):
    TOP = "top"
    BOTTOM = "bottom"
    NONE = "none"


class TicketStatus(str, Enum):
    PASS = "✅"
    WEAK = "⚠️"
    FAIL = "❌"


class SignalLevel(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


class BlackSwanLevel(str, Enum):
    """黑天鹅分级"""
    NONE = "none"
    L1 = "level1"   # 沪深300单日跌>5% → 减至当前50%
    L2 = "level2"   # 组合单日净值跌>7% → 减至当前30%
    L3 = "level3"   # 市场熔断/千股跌停 → 全部清仓


class ExitType(str, Enum):
    """退出信号类型"""
    HARD_CLEAR = "hard_clear"       # 硬清仓（立即执行）
    SOFT_CLEAR = "soft_clear"       # 软清仓（触发费率检查）
    REDUCE = "reduce"               # 减仓
    NONE = "none"


class BearStage(str, Enum):
    """熊市阶段"""
    INITIAL = "initial"
    MID = "mid"
    DEEP = "deep"
    BOTTOM = "bottom"


# ============================================================================
# 基础数据容器
# ============================================================================

@dataclass
class Candle:
    """单根 K 线"""
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class FractalResult:
    """分型检测结果"""
    kind: FractalKind
    index: int = -1
    quality_score: int = 0          # 0-3
    is_valid: bool = False
    checks: Dict[str, bool] = field(default_factory=dict)
    detail: str = ""


@dataclass
class TechnicalIndicators:
    """技术指标包"""
    ma5: List[float] = field(default_factory=list)
    ma10: List[float] = field(default_factory=list)
    ma20: List[float] = field(default_factory=list)
    ma60: Optional[List[float]] = None
    ma120: Optional[List[float]] = None
    macd_dif: List[float] = field(default_factory=list)
    macd_dea: List[float] = field(default_factory=list)
    macd_hist: List[float] = field(default_factory=list)
    rsi14: float = 0.0
    avg_volume_5: float = 0.0
    trend: str = "neutral"


@dataclass
class ModeResult:
    """Phase 0 模式识别结果"""
    etf_code: str
    etf_type: ETFType
    base_mode: MarketMode
    overlay_modes: List[MarketMode] = field(default_factory=list)
    active_mode: MarketMode = MarketMode.A
    black_swan_level: str = BlackSwanLevel.NONE.value
    reasoning: List[str] = field(default_factory=list)


@dataclass
class TicketResult:
    """单张票判定结果"""
    status: TicketStatus
    detail: str = ""
    value: float = 0.0


@dataclass
class ThreeTicketDecision:
    """三票制综合判定"""
    ticket_pa: TicketResult
    ticket_vpa: TicketResult
    ticket_share: TicketResult
    passed_count: int = 0
    can_enter: bool = False
    base_position: float = 0.0
    bonus_items: Dict[str, float] = field(default_factory=dict)
    final_position: float = 0.0
    discount_applied: bool = False
    decision: str = ""
    boundary_position: float = 0.0       # 2/3 边界底仓 %
    boundary_stop_pct: float = 0.0       # 边界底仓止损 %


@dataclass
class PositionAdvice:
    """仓位建议"""
    suggested_pct: float
    pe_percentile: Optional[float] = None
    action: str = ""


@dataclass
class RiskCheck:
    """风控检查结果"""
    level: SignalLevel
    alerts: List[str] = field(default_factory=list)
    action_required: bool = False
    detail: str = ""


@dataclass
class ETFInfo:
    """ETF 信息"""
    code: str
    theme: str
    scale_billion: float
    alternatives: List[str] = field(default_factory=list)


@dataclass
class ExitSignal:
    """退出信号判定结果"""
    exit_type: ExitType
    triggers: List[str] = field(default_factory=list)   # 触发原因列表
    target_ratio: float = 0.0    # 目标仓位比例（0=清仓, 0.5=减至50%）
    batch_count: int = 1         # 分批次数
    batch_interval_days: int = 7 # 每批间隔天数
    need_fee_check: bool = False # 是否需要费率检查
    detail: str = ""


@dataclass
class ExitDecision:
    """退出决策汇总"""
    code: str
    signals: List[ExitSignal] = field(default_factory=list)
    hard_triggered: bool = False
    soft_triggered: bool = False
    should_exit: bool = False
    hard_reasons: List[str] = field(default_factory=list)
    soft_reasons: List[str] = field(default_factory=list)
    fee_reason: str = ""
    summary: str = ""


@dataclass
class BlackSwanAction:
    """黑天鹅触发结果"""
    level: BlackSwanLevel
    target_ratio: float = 1.0    # 减仓后的目标仓位比例
    action: str = ""
    force_mode_d: bool = False   # 是否强制进入模式D


@dataclass
class MacroOverlay:
    """模式E宏观叠加结果"""
    policy_level: str = ""       # S/A/B/C/X
    policy_actions: List[str] = field(default_factory=list)
    position_cap_adjust: float = 0.0   # 建仓上限调整（+0.10 等）
    pause_reduce_days: int = 0         # 暂停减仓信号天数
    force_exit_check: bool = False     # X级利空强制触发清仓检查
    northbound_alert: str = ""        # 北向资金信号描述
    detail: str = ""
