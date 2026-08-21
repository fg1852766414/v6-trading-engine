"""
etf_selector.py — ETF 选标
============================
主题名 → 首选代码 / 场外联接基金 / 替代标的提示。
ETF 池数据从 config.etf.pool 注入，可运行时替换。
"""

from __future__ import annotations
from typing import List, Optional

from .config import EngineConfig, ETFConfig
from .models import ETFInfo


class ETFSelector:
    """ETF 选标引擎"""

    def __init__(self, config: EngineConfig | None = None):
        self.cfg: ETFConfig = (config or EngineConfig.default()).etf

    def select(self, theme: str) -> Optional[ETFInfo]:
        """
        根据主题名匹配规模最大的 ETF。
        返回 ETFInfo 或 None。
        """
        theme_lower = theme.strip()
        for key, info in self.cfg.pool.items():
            if key in theme_lower:
                return ETFInfo(
                    code=info["code"],
                    theme=key,
                    scale_billion=info["scale"],
                    alternatives=info.get("alts", []),
                )
        return None

    def get_fund_code(self, theme: str) -> Optional[str]:
        """获取场外联接基金代码"""
        for key, codes in self.cfg.otc_link_map.items():
            if key in theme:
                return codes[0] if codes else None
        return None

    def is_preferred(self, code: str) -> bool:
        """检查给定代码是否为某主题的首选 ETF"""
        code_clean = code.upper()
        for info in self.cfg.pool.values():
            if info["code"].upper() == code_clean:
                return True
        return False

    def find_alternative(self, code: str) -> Optional[str]:
        """
        如果给定代码不是首选，返回推荐的大规模替代品。
        """
        code_clean = code.upper()
        # 已经是首选
        for theme, info in self.cfg.pool.items():
            if info["code"].upper() == code_clean:
                return None
        # 在次选池中
        for theme, info in self.cfg.pool.items():
            for alt in info.get("alts", []):
                if alt.upper() == code_clean:
                    return (
                        f"建议用 {info['code']}（{theme}主题，规模 {info['scale']}亿）"
                        f"代替 {code}"
                    )
        return None

    def list_all(self) -> List[ETFInfo]:
        """列出所有 ETF"""
        return [
            ETFInfo(
                code=v["code"], theme=k,
                scale_billion=v["scale"],
                alternatives=v.get("alts", []),
            )
            for k, v in self.cfg.pool.items()
        ]
