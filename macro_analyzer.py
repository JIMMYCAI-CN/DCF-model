"""
macro_analyzer.py — 宏觀參數提供者（v3.0，凍結模組）
=========================================================
定位：**只提供參數，不參與任何估值演算**。

    DCF 模組只能呼叫 `fetch()` 取得 WACC 與長期名義 GDP 增長率，
    不可以反向修改本模組，也不可以把折現邏輯寫進來。

對外介面
--------
    fetch(company, *, simulate_failure=False) -> MacroSnapshot
        company 只需要 market_cap / debt / equity / industry / jurisdiction 等欄位。
        抓取失敗丟 MacroUnavailable，由呼叫端（DCF/UI）降級為手動 WACC，不阻斷計算。

    long_run_gdp(jurisdiction) -> float
        長期名義 GDP 增長率，用於「永續增長率是否偏樂觀」的風險判斷。

計算口徑（CAPM ＋ 資本結構加權）
--------------------------------
    Ke   = Rf + β × ERP
    Kd   = Rf + 信用利差
    WACC = E/(D+E) × Ke + D/(D+E) × Kd × (1 − 法定稅率)

資料來源在此以「可重現的參數表」實作（示範環境沒有即時行情連線），
每一項都附 source 標籤，方便 DCF 報告做參數溯源。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, Optional


class MacroUnavailable(RuntimeError):
    """宏觀資料抓取失敗（網路、授權、資料延遲…）。呼叫端應降級為手動輸入。"""


# ---------------------------------------------------------------- 參數表
#: 各地區無風險利率（10 年期政府債券孳息，示範口徑）
RISK_FREE: Dict[str, float] = {"HK": 0.0365, "MO": 0.0390}
#: 股權風險溢價
EQUITY_RISK_PREMIUM: Dict[str, float] = {"HK": 0.0560, "MO": 0.0620}
#: 長期名義 GDP 增長率（永續增長率的上限參考）
LONG_RUN_GDP: Dict[str, float] = {"HK": 0.0320, "MO": 0.0260}
#: 長期通脹
INFLATION: Dict[str, float] = {"HK": 0.0210, "MO": 0.0190}
#: 法定標準稅率（僅用於計算稅盾，實際課稅仍由 tax.py 負責）
STATUTORY_TAX: Dict[str, float] = {"HK": 0.165, "MO": 0.12}
#: 行業 β（去槓桿前的資產 β 再依資本結構調整）
INDUSTRY_BETA: Dict[str, float] = {
    "電子製造": 1.12, "食品製造": 0.78, "酒店及休閒": 1.28,
    "零售貿易": 0.95, "物業發展": 1.05, "科技服務": 1.20,
    "物流運輸": 0.92, "金融服務": 1.10,
}
DEFAULT_BETA = 1.00
#: 信用利差（依淨負債／市值分級）
SPREAD_TIERS = ((0.0, 0.0120), (0.3, 0.0180), (0.6, 0.0245), (1.0, 0.0320), (9.9, 0.0420))


@dataclass
class MacroSnapshot:
    jurisdiction: str
    as_of: str
    risk_free: float
    equity_risk_premium: float
    beta: float
    cost_of_equity: float
    cost_of_debt: float
    tax_shield_rate: float
    weight_equity: float
    weight_debt: float
    wacc: float
    nominal_gdp_growth: float
    inflation: float
    industry: str = ""
    sources: Dict[str, str] = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def long_run_gdp(jurisdiction: str) -> float:
    return LONG_RUN_GDP.get(str(jurisdiction or "").upper(), 0.030)


def _spread(net_debt_to_cap: float) -> float:
    for limit, spread in SPREAD_TIERS:
        if net_debt_to_cap <= limit:
            return spread
    return SPREAD_TIERS[-1][1]


def fetch(company: Optional[Dict[str, Any]] = None, *,
          simulate_failure: bool = False,
          jurisdiction: Optional[str] = None) -> MacroSnapshot:
    """
    取得宏觀參數（含 WACC）。

    company 可含：jurisdiction、industry、market_cap、debt、cash、net_debt、beta
    任何一項缺少都會用合理預設，並在 notes 說明。
    """
    if simulate_failure:
        raise MacroUnavailable("宏觀資料服務無回應（示範用錯誤，用於驗證降級流程）")

    c = dict(company or {})
    jur = str(jurisdiction or c.get("jurisdiction") or "HK").upper()
    if jur not in RISK_FREE:
        jur = "HK"

    industry = str(c.get("industry") or "")
    beta = c.get("beta")
    notes = []
    if beta is None:
        beta = INDUSTRY_BETA.get(industry, DEFAULT_BETA)
        notes.append(f"β 採用行業參考值（{industry or '未分類'}）：{beta:.2f}")
    beta = float(beta)

    rf = RISK_FREE[jur]
    erp = EQUITY_RISK_PREMIUM[jur]

    market_cap = float(c.get("market_cap") or 0.0)
    debt = float(c.get("debt") or 0.0)
    equity = market_cap or float(c.get("equity") or 0.0)
    if equity <= 0:
        equity = max(debt, 1.0)
        notes.append("缺少市值與權益帳面值，資本結構暫以負債等額替代，請手動核對。")

    total = equity + debt
    w_e = equity / total if total else 1.0
    w_d = debt / total if total else 0.0

    ke = rf + beta * erp
    kd = rf + _spread(debt / total if total else 0.0)
    tax_shield = STATUTORY_TAX[jur]
    wacc = w_e * ke + w_d * kd * (1 - tax_shield)

    # 保守夾擠：避免極端資本結構產生不合理折現率
    if not (0.03 <= wacc <= 0.30):
        notes.append(f"模型 WACC {wacc:.2%} 超出合理區間，已夾擠至 3%–30%。")
        wacc = min(max(wacc, 0.03), 0.30)

    return MacroSnapshot(
        jurisdiction=jur,
        as_of=datetime.now().strftime("%Y-%m-%d %H:%M"),
        risk_free=rf,
        equity_risk_premium=erp,
        beta=beta,
        cost_of_equity=ke,
        cost_of_debt=kd,
        tax_shield_rate=tax_shield,
        weight_equity=w_e,
        weight_debt=w_d,
        wacc=wacc,
        nominal_gdp_growth=LONG_RUN_GDP[jur],
        inflation=INFLATION[jur],
        industry=industry,
        sources={
            "risk_free": f"{jur} 10 年期政府債券孳息",
            "equity_risk_premium": f"{jur} 股權風險溢價（市場隱含）",
            "beta": "行業 β 參考值",
            "cost_of_debt": "無風險利率 ＋ 信用利差",
            "wacc": "CAPM ＋ 資本結構加權（稅後）",
            "nominal_gdp_growth": "長期名義 GDP 增長（央行／統計局口徑）",
        },
        notes=notes,
    )
