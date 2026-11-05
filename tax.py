"""
tax.py — 所得稅計算：澳門所得補充稅 ＋ 香港利得稅（v2.2）
=========================================================
對外函數：
  tax_on_taxable(taxable, cfg)     可課稅收益 → 稅額
  marginal_rate(profit, cfg)       邊際稅率（處置損益、稅盾說明用）
  tax_on_gain(gain, cfg, base)     處置損益的增量稅負（可為負＝抵稅）
  rd_extra_deduction(expense, cfg) 研發開支的「額外」可扣減金額
  effective_rate(profit, cfg)      有效稅率（報表用）

適用法律（TaxConfig.jurisdiction）
  MO  澳門所得補充稅，三種稅率口徑：
      budget       年度預算案：首 60 萬豁免，超出 12%（預設；2024／2025 年度均如此）
      progressive  規章累進級距：32,000 以下豁免；3%／5%／7%／9%／12%
      fixed        自訂單一稅率（教學、跨境比較）
      組別差異：A 組虧損可結轉 3 年、可享研發加計扣除；B 組不可結轉
  HK  香港利得稅（法團兩級制）：
      首 200 萬港元 8.25%、其餘 16.5%；虧損可無限期結轉；
      研發開支加計扣除：首 200 萬 3 倍、其餘 2 倍、無上限
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .models import TaxConfig


# ---------------------------------------------------------------- 級距
def _progressive(taxable: float, brackets: List[tuple]) -> float:
    """累進稅：brackets = [(該段上限, 該段稅率), ...]，逐段對「超出部分」課稅。"""
    if taxable <= 0:
        return 0.0
    tax, prev = 0.0, 0.0
    for limit, rate in brackets:
        seg = min(taxable, limit) - prev
        if seg > 0:
            tax += seg * rate
        prev = limit
        if taxable <= limit:
            break
    return tax


def tax_on_taxable(taxable: float, cfg: TaxConfig) -> float:
    """可課稅收益 → 應納稅額。虧損年度不課稅。"""
    if taxable <= 0:
        return 0.0
    if cfg.jurisdiction == "HK":
        # 香港利得稅・法團兩級制：首 200 萬 8.25%、其餘 16.5%
        low = min(taxable, cfg.hk_threshold) * cfg.hk_low_rate
        high = max(taxable - cfg.hk_threshold, 0.0) * cfg.hk_high_rate
        return low + high
    if cfg.regime == "budget":
        return max(taxable - cfg.free_threshold, 0.0) * 0.12
    if cfg.regime == "progressive":
        return _progressive(taxable, cfg.brackets)
    return taxable * cfg.fixed_rate


def marginal_rate(profit: float, cfg: TaxConfig) -> float:
    """邊際稅率：多用於「這筆額外收益會被課多少稅」。"""
    if cfg.jurisdiction == "HK":
        return cfg.hk_low_rate if profit <= cfg.hk_threshold else cfg.hk_high_rate
    if cfg.regime == "budget":
        return 0.12 if profit >= cfg.free_threshold else 0.0
    if cfg.regime == "fixed":
        return cfg.fixed_rate
    for limit, rate in cfg.brackets:
        if profit <= limit:
            return rate
    return cfg.brackets[-1][1]


def tax_on_gain(gain: float, cfg: TaxConfig, operating_taxable: float = 0.0,
                already_negative: bool = False) -> float:
    """
    資產處置損益的稅務影響（增量法）：

        稅 = 稅(營業可課稅收益 + 處置損益) − 稅(營業可課稅收益)

    這樣才不會重複享用 60 萬豁免額，且虧損（負 gain）能正確產生抵稅效果。
    """
    if gain == 0:
        return 0.0
    before = 0.0 if already_negative else tax_on_taxable(operating_taxable, cfg)
    after = tax_on_taxable(operating_taxable + gain, cfg)
    return after - before


def effective_rate(taxable: float, cfg: TaxConfig) -> float:
    if taxable <= 0:
        return 0.0
    return tax_on_taxable(taxable, cfg) / taxable


# ---------------------------------------------------------------- 研發加計
def rd_total_deduction(expense: float, cfg: TaxConfig) -> float:
    """
    研發開支的可扣減總額（含本身）。依年度預算案：
      首 300 萬 → 扣減 3 倍；超出部分 → 扣減 2 倍；總扣減上限 1,500 萬。
    B 組或未啟用時回傳 0。
    """
    if not cfg.rd_eligible or expense <= 0:
        return 0.0
    first = min(expense, cfg.rd_first_limit)
    excess = max(expense - cfg.rd_first_limit, 0.0)
    total = first * cfg.rd_first_multiple + excess * cfg.rd_excess_multiple
    return min(total, cfg.rd_cap)


def rd_extra_deduction(expense: float, cfg: TaxConfig) -> float:
    """「額外」扣減金額 = 可扣減總額 − 開支本身（因開支已在 EBITDA 扣除）。"""
    if not cfg.rd_eligible or expense <= 0:
        return 0.0
    return max(rd_total_deduction(expense, cfg) - expense, 0.0)


# ---------------------------------------------------------------- 虧損結轉
@dataclass
class LossPool:
    """
    稅務虧損結轉池。

    A 組：可結轉 3 年（cfg.carry_years）；B 組：0 年（不可結轉）。
    以「(剩餘金額, 到期年度)」清單管理，逐年先扣最早到期的虧損。
    """

    years: int = 0

    def __post_init__(self) -> None:
        self._items: List[List[float]] = []   # [[amount, expiry_year], ...]

    def add(self, amount: float, year: int) -> None:
        if amount > 0 and self.years > 0:
            self._items.append([amount, year + self.years])

    def use(self, taxable: float, year: int) -> float:
        """以虧損抵扣 taxable，回傳實際抵扣金額。"""
        if taxable <= 0:
            return 0.0
        self._items = [it for it in self._items if it[1] >= year and it[0] > 0]
        used = 0.0
        for item in self._items:
            if used >= taxable:
                break
            take = min(item[0], taxable - used)
            item[0] -= take
            used += take
        self._items = [it for it in self._items if it[0] > 1e-9]
        return used

    @property
    def balance(self) -> float:
        return sum(it[0] for it in self._items)
