"""
utils.py — 数学工具函数
========================
SMA / EMA / RMA，纯函数无副作用。
"""

from __future__ import annotations
from typing import List


def sma(values: List[float], period: int) -> List[float]:
    """简单移动平均"""
    if period <= 0 or len(values) < period:
        return [0.0] * len(values) if values else []
    result = [0.0] * len(values)
    window_sum = sum(values[:period])
    result[period - 1] = window_sum / period
    for i in range(period, len(values)):
        window_sum += values[i] - values[i - period]
        result[i] = window_sum / period
    return result


def ema(values: List[float], period: int) -> List[float]:
    """
    指数移动平均
    初始化：前 period 根的 SMA
    multiplier = 2 / (period + 1)
    """
    if period <= 0 or len(values) < period:
        return [0.0] * len(values) if values else []
    result = [0.0] * len(values)
    result[period - 1] = sum(values[:period]) / period
    multiplier = 2.0 / (period + 1)
    for i in range(period, len(values)):
        result[i] = (values[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def rma(values: List[float], period: int) -> List[float]:
    """
    Wilder's RMA（用于 RSI 计算）
    alpha = 1 / period
    """
    if period <= 0 or len(values) < period:
        return [0.0] * len(values) if values else []
    result = [0.0] * len(values)
    result[period - 1] = sum(values[:period]) / period
    alpha = 1.0 / period
    for i in range(period, len(values)):
        result[i] = alpha * values[i] + (1 - alpha) * result[i - 1]
    return result
