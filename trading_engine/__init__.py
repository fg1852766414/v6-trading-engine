"""
trading_engine — V5 确定性交易分析引擎包
==========================================
用法：
    from scripts.trading_engine import (
        EngineConfig, ModeEngine, TechnicalEngine, TicketEngine,
        PositionEngine, RiskEngine, ETFSelector, FeeEngine,
        quick_analysis,
    )

    # 使用默认参数
    engine = ModeEngine()
    result = engine.determine_mode("588200.SH", hs300_candles)

    # 自定义参数
    cfg = EngineConfig.default()
    cfg.position.single_direction_cap = 35.0
    pos_engine = PositionEngine(cfg)
"""

from .config import (
    EngineConfig,
    ModeConfig,
    TechnicalConfig,
    TicketConfig,
    PositionConfig,
    RiskConfig,
    FeeConfig,
    ETFConfig,
)
from .models import (
    MarketMode, ETFType, FractalKind, TicketStatus, SignalLevel,
    BlackSwanLevel, ExitType, BearStage,
    Candle, FractalResult, TechnicalIndicators, ModeResult,
    TicketResult, ThreeTicketDecision, PositionAdvice, RiskCheck, ETFInfo,
    ExitSignal, ExitDecision, BlackSwanAction, MacroOverlay,
)
from .mode_engine import ModeEngine
from .technical_engine import TechnicalEngine
from .ticket_engine import TicketEngine
from .position_engine import PositionEngine
from .risk_engine import RiskEngine
from .etf_selector import ETFSelector
from .fee_engine import FeeEngine
from .exit_engine import ExitEngine
from .utils import sma, ema, rma


# ============================================================================
# quick_analysis — 一键分析
# ============================================================================

def quick_analysis(
    etf_code: str,
    candles: list,
    share_changes: list,
    hs300_candles: list | None = None,
    pe_percentile: float | None = None,
    premium: float | None = None,
    has_catalyst: bool = False,
    current_positions: dict | None = None,
    cash_ratio: float = 1.0,
    positions_all_triggered: bool = False,
    left_side_batch: int = 0,
    config: EngineConfig | None = None,
) -> dict:
    """
    一键综合分析。
    输入原始数据 → 输出完整决策摘要。

    参数：
        etf_code:          ETF 代码，如 "588200.SH"
        candles:           K 线列表 [Candle, ...]
        share_changes:     逐日份额变化率 [float, ...]（latest 在最后）
        hs300_candles:     沪深 300 K 线（可选，不传则跳过模式识别）
        pe_percentile:     当前 PE 分位 %（可选，仅模式 A 需要）
        premium:           溢价率 %（可选）
        has_catalyst:      AI 判断的近 3 日是否有催化剂
        current_positions: 当前持仓 {code: {actual_pct, suggested_pct, ...}}
        cash_ratio:                 现金比例
        positions_all_triggered:    所有持仓标的是否均已触发减仓信号
        left_side_batch:            左侧底仓当前批数（0=第1批, 1=第2批, 2=第3批）
        config:                     引擎配置（None 用默认）

    返回 dict，包含 mode/technical/tickets/position/risk_alerts
    """
    cfg = config or EngineConfig.default()
    mode_eng = ModeEngine(cfg)
    tech_eng = TechnicalEngine(cfg)
    ticket_eng = TicketEngine(cfg)
    pos_eng = PositionEngine(cfg)
    risk_eng = RiskEngine(cfg)

    result: dict = {}

    # Phase 0: 模式
    if hs300_candles:
        mode_r = mode_eng.determine_mode(
            etf_code, hs300_candles,
            cash_ratio=cash_ratio,
            positions_all_triggered=positions_all_triggered,
        )
        result["mode"] = mode_r.active_mode.value
        result["etf_type"] = mode_r.etf_type.value
        result["mode_reasoning"] = mode_r.reasoning
    else:
        result["mode"] = "A"
        result["etf_type"] = "unknown"

    active_mode = MarketMode(result["mode"])

    # 技术指标
    tech = tech_eng.analyze(candles, active_mode)
    result["technical"] = {
        k: v for k, v in tech.items()
        if v is not None
    }

    # 三票制
    tickets = ticket_eng.evaluate(candles, share_changes, premium, has_catalyst=has_catalyst)
    result["tickets"] = {
        "pa": {"status": tickets.ticket_pa.status.value, "detail": tickets.ticket_pa.detail},
        "vpa": {"status": tickets.ticket_vpa.status.value, "detail": tickets.ticket_vpa.detail},
        "share": {"status": tickets.ticket_share.status.value, "detail": tickets.ticket_share.detail},
        "passed": tickets.passed_count,
        "decision": tickets.decision,
        "boundary_position": tickets.boundary_position,
        "boundary_stop_pct": tickets.boundary_stop_pct,
    }

    # PE 仓位（仅模式 A 有效；模式 B 跳过，调用方也不应传入 PE）
    if active_mode == MarketMode.A and pe_percentile is not None and pe_percentile > 0:
        pos_advice = pos_eng.pe_to_position(pe_percentile)
        result["position_pe_based"] = {
            "percentile": pe_percentile,
            "suggested_pct": pos_advice.suggested_pct,
            "action": pos_advice.action,
        }

    # 加分项
    macd_golden = tech.get("macd_golden_cross", False)
    above_ma5 = (
        tech.get("ma5") is not None
        and tech.get("latest_close", 0) > (tech.get("ma5") or 0)
    )
    # PE/PS 低估加分仅在对应模式下生效
    pe_low = False
    if active_mode == MarketMode.A:
        pe_low = (pe_percentile or 100) < cfg.position.pe_undervalue_a
    # 模式 B 下可将 pe_low 替换为 ps_low（由调用方传入）
    bonuses = pos_eng.calc_bonus(macd_golden, above_ma5, pe_low, has_catalyst)
    result["bonus_items"] = bonuses

    # 最终仓位
    if tickets.can_enter:
        total_eq = sum(
            p.get("actual_pct", 0) for p in (current_positions or {}).values()
        )
        decision = pos_eng.calculate(decision=tickets, bonuses=bonuses,
                                     current_total_equity=total_eq)
        result["final_position"] = decision.final_position
        result["discount_applied"] = decision.discount_applied
    else:
        result["final_position"] = 0.0

    # 左侧底仓（仅模式 A 有效，模式 B 不看 PE）
    if active_mode == MarketMode.A and pe_percentile is not None and pe_percentile < cfg.position.left_side_pe_threshold:
        lb = pos_eng.left_side_base_position(
            pe_percentile, macd_golden, tech.get("above_ma20_stable", False),
            current_batch=left_side_batch,
        )
        if lb:
            result["left_side_base"] = {
                "suggested_pct": lb.suggested_pct,
                "action": lb.action,
            }

    # 费率检查（持仓标的 > 0 时触发）
    if current_positions and candles:
        fee_eng = FeeEngine(cfg)
        current_price = candles[-1].close
        support = fee_eng.find_support_level(candles, tech.get("ma120"))
        active_mode = MarketMode(result["mode"])
        fee_results = []
        for code, pos in current_positions.items():
            if pos.get("actual_pct", 0) <= 0:
                continue
            entry_price = pos.get("entry_price")
            holding_days = pos.get("holding_days", 0)
            if entry_price and entry_price > 0:
                profit_pct = (current_price - entry_price) / entry_price
                sell_now, reason = fee_eng.should_sell_now(
                    current_price=current_price,
                    support_level=support,
                    holding_days=holding_days,
                    profit_pct=profit_pct,
                    mode=active_mode,
                )
                fee_results.append({
                    "code": code,
                    "sell_now": sell_now,
                    "reason": reason,
                })
        if fee_results:
            result["fee_check"] = fee_results

    # 风控
    if current_positions:
        total_eq_raw = sum(p.get("actual_pct", 0) for p in current_positions.values())
        # actual_pct 是百分比（如 65.0），转换为比值（0.65）传给风控引擎
        total_eq_ratio = total_eq_raw / 100.0 if total_eq_raw > 1 else total_eq_raw
        checks = risk_eng.comprehensive_check(
            total_equity_ratio=total_eq_ratio,
            cash_ratio=cash_ratio,
        )
        result["risk_alerts"] = [
            {"level": c.level.value, "alerts": c.alerts}
            for c in checks if c.action_required
        ]

        # 退出信号（持仓标的逐个检查）
        exit_eng = ExitEngine(cfg)
        exit_results = []
        for code, pos in current_positions.items():
            if pos.get("actual_pct", 0) <= 0:
                continue
            exit_decision = exit_eng.evaluate(
                code=code,
                candles=candles,
                share_changes=share_changes,
                mode=active_mode,
                pe_percentile=pe_percentile,
                industry_logic_invalidated=pos.get("logic_invalidated", False),
                holding_days=pos.get("holding_days", 0),
                entry_price=pos.get("entry_price"),
            )
            if exit_decision.should_exit:
                exit_results.append({
                    "code": code,
                    "hard": exit_decision.hard_triggered,
                    "soft": exit_decision.soft_triggered,
                    "summary": exit_decision.summary,
                    "hard_reasons": exit_decision.hard_reasons,
                    "soft_reasons": exit_decision.soft_reasons,
                    "fee_reason": exit_decision.fee_reason,
                    "signals": [
                        {"type": s.exit_type.value, "triggers": s.triggers,
                         "target_ratio": s.target_ratio, "batches": s.batch_count}
                        for s in exit_decision.signals
                    ],
                })
        if exit_results:
            result["exit_signals"] = exit_results

    return result


__all__ = [
    # config
    "EngineConfig", "ModeConfig", "TechnicalConfig", "TicketConfig",
    "PositionConfig", "RiskConfig", "FeeConfig", "ETFConfig",
    # models
    "MarketMode", "ETFType", "FractalKind", "TicketStatus", "SignalLevel",
    "BlackSwanLevel", "ExitType", "BearStage",
    "Candle", "FractalResult", "TechnicalIndicators", "ModeResult",
    "TicketResult", "ThreeTicketDecision", "PositionAdvice", "RiskCheck", "ETFInfo",
    "ExitSignal", "ExitDecision", "BlackSwanAction", "MacroOverlay",
    # engines
    "ModeEngine", "TechnicalEngine", "TicketEngine", "PositionEngine",
    "RiskEngine", "ETFSelector", "FeeEngine", "ExitEngine",
    # utils
    "sma", "ema", "rma",
    # convenience
    "quick_analysis",
]


# ============================================================================
# 自测
# ============================================================================

if __name__ == "__main__":
    import random
    random.seed(42)

    print("=" * 60)
    print("trading_engine 包自测")
    print("=" * 60)

    # 模拟 40 根 K 线
    base = 1.0
    test_candles = []
    for i in range(40):
        close = base * (1 + i * 0.01 + random.uniform(-0.02, 0.02))
        test_candles.append(Candle(
            date=f"2026-07-{i+1:02d}",
            open=close * random.uniform(0.98, 1.0),
            high=close * random.uniform(1.0, 1.03),
            low=close * random.uniform(0.97, 1.0),
            close=close,
            volume=1_000_000 + random.uniform(-200_000, 200_000),
        ))

    # 1. 分型
    tech = TechnicalEngine()
    fractal = tech.detect_fractal(test_candles)
    print(f"\n分型: {fractal.kind.value} @ idx={fractal.index}")
    if fractal.kind.value == "bottom":
        v = tech.validate_bottom_fractal(test_candles, fractal)
        print(f"  验证: valid={v.is_valid}, score={v.quality_score}")

    # 2. 三票制
    ticket = TicketEngine()
    shares = [0.5, 1.2, 0.8]
    t = ticket.evaluate(test_candles, shares)
    print(f"\n三票: {t.passed_count}/3 -> {t.decision}")

    # 3. PE 仓位
    pos = PositionEngine()
    for pe in [10, 25, 50, 75, 90]:
        p = pos.pe_to_position(pe)
        print(f"  PE{pe}% -> {p.suggested_pct}% ({p.action})")

    # 4. 风控
    risk = RiskEngine()
    dd = risk.check_drawdown(-12)
    print(f"\n回撤: {dd.level.value}")

    # 5. ETF 选标
    sel = ETFSelector()
    for theme in ["芯片", "创新药", "机器人"]:
        s = sel.select(theme)
        print(f"  {theme} -> {s.code if s else 'N/A'}")

    # 6. 自定义参数
    cfg = EngineConfig.default()
    cfg.position.single_direction_cap = 35.0
    print(f"\n自定义 single_direction_cap: {cfg.position.single_direction_cap}%")

    print("\n所有测试通过")
