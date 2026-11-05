"""
dcf.py — 企業 DCF（FCFF）估值模組（v3.0 新增）
=========================================================
口徑
----
    FCFF = EBIT × (1 − 有效稅率) + 折舊攤銷 − 資本開支 − 營運資金增量

    EV   = Σ FCFF_t / (1+WACC)^t  +  TV / (1+WACC)^N
    TV   = FCFF_N × (1+g) / (WACC − g)                 （Gordon 永續增長）
    股權價值 = EV − 淨負債（計息負債 ＋ 非控股權益 − 現金）
    內在股價 = 股權價值 ÷ 已發行股數

與 NPV 模組的界線（硬性約束）
------------------------------
    * 本模組不 import capital_budget / engine，也不會讀寫專案現金流。
    * DCF 的 FCFF 是「整間企業」的現金流，NPV 的是「單一專案增量」現金流，
      兩者在 app 層各自獨立計算、獨立匯出，只共用財報解析、繪圖、匯出、溯源機制。
    * 稅務一律呼叫 tax.py 的既有實作（香港兩級利得稅、澳門所得補充稅）。

防錯
----
    WACC ≤ g 時 Gordon 公式分母 ≤ 0，數學上無解 → 直接 raise DCFError("WACC_LE_G")，
    由 UI 彈出提示並禁止計算；不會偷偷回傳 infinity 或負值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .models import TaxConfig
from .provenance import ParamBook
from .statements import BaseExtract
from .tax import tax_on_taxable

ENGINE_VERSION = "DCF-FCFF 1.0"
DEFAULT_YEARS = 5
DEFAULT_WACC = {"HK": 0.090, "MO": 0.100}
DEFAULT_G = 0.025

#: 三情景預設偏移（百分點）
DEFAULT_OFFSETS: Dict[str, Dict[str, float]] = {
    "optimistic": {"growth": 0.020, "margin": 0.015, "wacc": -0.010, "g": 0.005},
    "base": {"growth": 0.0, "margin": 0.0, "wacc": 0.0, "g": 0.0},
    "conservative": {"growth": -0.020, "margin": -0.015, "wacc": 0.010, "g": -0.005},
}
SCENARIO_LABELS = {"optimistic": "樂觀", "base": "基準", "conservative": "保守"}


class DCFError(ValueError):
    """估值無法進行（帶錯誤代碼，方便 UI 對應提示）。"""

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message, "hint": self.hint}


# ---------------------------------------------------------------- 資料結構
@dataclass
class DCFInput:
    company_name: str = ""
    ticker: str = ""
    jurisdiction: str = "HK"
    currency: str = "HKD"
    unit: str = "百萬"
    scale: float = 1e6
    base_year: int = 0
    history_years: int = 0

    revenue: float = 0.0
    ebit: float = 0.0
    da: float = 0.0
    capex: float = 0.0
    nwc: float = 0.0
    net_debt: float = 0.0
    shares: float = 0.0
    price: float = 0.0

    years: int = DEFAULT_YEARS
    wacc: float = 0.09
    g: float = DEFAULT_G
    growth: List[float] = field(default_factory=list)
    margin: List[float] = field(default_factory=list)
    da_pct: float = 0.0
    capex_pct: float = 0.0
    nwc_pct: float = 0.0
    tax_mode: str = "statutory"          # statutory | effective
    tax_rate: float = 0.165
    mo_regime: str = "budget"
    macro_gdp: float = 0.032
    offsets: Dict[str, Dict[str, float]] = field(
        default_factory=lambda: {k: dict(v) for k, v in DEFAULT_OFFSETS.items()})
    wacc_span: float = 0.02
    g_span: float = 0.01
    sens_steps: int = 9

    # -- 衍生 ---------------------------------------------------------
    @property
    def unit_label(self) -> str:
        return self.unit or self.currency


@dataclass
class DCFResult:
    inp: DCFInput
    rows: List[Dict[str, float]]
    summary: Dict[str, Any]
    scenarios: List[Dict[str, Any]]
    sensitivity: Dict[str, Any]
    tags: List[Dict[str, str]]
    checks: List[Dict[str, Any]]
    book: Optional[ParamBook] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": ENGINE_VERSION,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "company": {
                "name": self.inp.company_name, "ticker": self.inp.ticker,
                "jurisdiction": self.inp.jurisdiction,
                "currency": self.inp.currency, "unit": self.inp.unit,
                "base_year": self.inp.base_year,
                "history_years": self.inp.history_years,
            },
            "assumptions": assumptions_dict(self.inp),
            "rows": self.rows,
            "summary": self.summary,
            "scenarios": self.scenarios,
            "sensitivity": self.sensitivity,
            "tags": self.tags,
            "checks": self.checks,
            "provenance": self.book.to_dict() if self.book else {},
        }


def assumptions_dict(inp: DCFInput) -> Dict[str, Any]:
    return {
        "years": inp.years,
        "wacc": inp.wacc,
        "g": inp.g,
        "growth": inp.growth,
        "margin": inp.margin,
        "da_pct": inp.da_pct,
        "capex_pct": inp.capex_pct,
        "nwc_pct": inp.nwc_pct,
        "tax_mode": inp.tax_mode,
        "tax_rate": inp.tax_rate,
        "mo_regime": inp.mo_regime,
        "macro_gdp": inp.macro_gdp,
        "offsets": inp.offsets,
        "wacc_span": inp.wacc_span,
        "g_span": inp.g_span,
    }


# ---------------------------------------------------------------- 建構輸入
def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def default_state(base: BaseExtract, company: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """由財報基期資料產生 DCF 的初始假設（全部可被人工覆寫）。"""
    c = company or {}
    jur = str(c.get("jurisdiction") or ("HK" if (base.currency or "").upper() == "HKD" else "MO")).upper()
    years = DEFAULT_YEARS
    growth0 = 0.08 if base.history_years >= 3 else 0.06
    margin0 = base.ratios.get("ebit_margin") or 0.10
    return {
        "years": years,
        "wacc": DEFAULT_WACC.get(jur, 0.09),
        "g": DEFAULT_G,
        "growth": [growth0] * years,
        "margin": [margin0] * years,
        "da_pct": base.ratios.get("da_pct") or 0.04,
        "capex_pct": base.ratios.get("capex_pct") or 0.05,
        "nwc_pct": base.ratios.get("nwc_pct") or 0.10,
        "tax_mode": "statutory",
        "tax_rate": base.ratios.get("tax_rate") or 0.165,
        "mo_regime": "budget",
        "macro_gdp": float(c.get("macro_gdp") or 0.032),
        "offsets": {k: dict(v) for k, v in DEFAULT_OFFSETS.items()},
        "wacc_span": 0.02,
        "g_span": 0.01,
        "sens_steps": 9,
        "locks": {},
        "base_override": {},
    }


def build_input(state: Dict[str, Any], base: BaseExtract,
                company: Optional[Dict[str, Any]] = None) -> DCFInput:
    """把 UI 的 state ＋ 財報基期，組成計算用的 DCFInput（尊重人工覆寫）。"""
    c = company or {}
    locks = state.get("locks") or {}
    ov = state.get("base_override") or {}

    def pick(key: str, fallback: float) -> float:
        # 有覆寫值就用覆寫值（鎖定與否隻影響「重新抓取時會不會被刷新」）
        return _f(ov.get(key), fallback) if key in ov else fallback

    values = base.values
    years = max(1, min(int(_f(state.get("years"), DEFAULT_YEARS)), 15))

    growth = [_f(v, 0.05) for v in (state.get("growth") or [])][:years]
    growth += [growth[-1] if growth else 0.05] * (years - len(growth))
    margin = [_f(v, 0.10) for v in (state.get("margin") or [])][:years]
    margin += [margin[-1] if margin else 0.10] * (years - len(margin))

    jur = str(c.get("jurisdiction") or ("HK" if (base.currency or "").upper() == "HKD" else "MO")).upper()
    inp = DCFInput(
        company_name=str(c.get("name") or ""),
        ticker=str(c.get("ticker") or ""),
        jurisdiction=jur,
        currency=base.currency or str(c.get("currency") or ""),
        unit=base.unit or str(c.get("unit") or ""),
        scale=base.scale or 1.0,
        base_year=base.base_year,
        history_years=base.history_years,
        revenue=pick("revenue", values.get("revenue", 0.0)),
        ebit=pick("ebit", values.get("ebit", 0.0)),
        da=pick("da", values.get("da", 0.0)),
        capex=pick("capex", values.get("capex", 0.0)),
        nwc=pick("nwc", values.get("nwc", 0.0)),
        net_debt=pick("net_debt", values.get("net_debt", 0.0)),
        shares=pick("shares", values.get("shares", 0.0)),
        price=pick("price", values.get("price", 0.0)),
        years=years,
        wacc=_f(state.get("wacc"), DEFAULT_WACC.get(jur, 0.09)),
        g=_f(state.get("g"), DEFAULT_G),
        growth=growth,
        margin=margin,
        da_pct=_f(state.get("da_pct"), base.ratios.get("da_pct") or 0.0),
        capex_pct=_f(state.get("capex_pct"), base.ratios.get("capex_pct") or 0.0),
        nwc_pct=_f(state.get("nwc_pct"), base.ratios.get("nwc_pct") or 0.0),
        tax_mode=str(state.get("tax_mode") or "statutory"),
        tax_rate=_f(state.get("tax_rate"), base.ratios.get("tax_rate") or 0.165),
        mo_regime=str(state.get("mo_regime") or "budget"),
        macro_gdp=_f(state.get("macro_gdp"), 0.032),
        offsets={k: {kk: _f(vv) for kk, vv in (v or {}).items()}
                 for k, v in (state.get("offsets") or DEFAULT_OFFSETS).items()},
        wacc_span=_f(state.get("wacc_span"), 0.02),
        g_span=_f(state.get("g_span"), 0.01),
        sens_steps=int(_f(state.get("sens_steps"), 9)),
    )
    return inp


# ---------------------------------------------------------------- 稅
def _tax_config(inp: DCFInput) -> TaxConfig:
    if inp.jurisdiction == "HK":
        return TaxConfig(jurisdiction="HK", regime="hk")
    return TaxConfig(jurisdiction="MO", regime=inp.mo_regime or "budget")


def tax_amount(ebit: float, inp: DCFInput) -> float:
    """
    單一年度所得稅。

    statutory：呼叫 tax.py 的法定稅制（HK 兩級制／MO 所得補充稅），
               以絕對金額計算再換回報表單位 —— 因為 HK 首 200 萬、
               MO 豁免 60 萬都是絕對金額門檻。
    effective：使用者直接鎖定有效稅率，則 EBIT × 稅率。
    """
    if ebit <= 0:
        return 0.0
    if inp.tax_mode == "effective":
        return ebit * inp.tax_rate
    scale = inp.scale or 1.0
    return tax_on_taxable(ebit * scale, _tax_config(inp)) / scale


# ---------------------------------------------------------------- 核心計算
def _forecast(inp: DCFInput, growth: Sequence[float], margin: Sequence[float],
              wacc: float, g: float) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    if wacc <= g:
        raise DCFError(
            "WACC_LE_G",
            f"WACC（{wacc:.2%}）必須大於永續增長率 g（{g:.2%}）。",
            "請調高 WACC 或調低 g：Gordon 永續增長公式在 WACC ≤ g 時無解。",
        )

    rows: List[Dict[str, float]] = []
    revenue_prev = inp.revenue
    nwc_prev = inp.nwc
    pv_total = 0.0

    for i in range(inp.years):
        year = i + 1
        gr = growth[i]
        mg = margin[i]
        revenue = revenue_prev * (1 + gr)
        ebit = revenue * mg
        tax = tax_amount(ebit, inp)
        nopat = ebit - tax
        da = revenue * inp.da_pct
        capex = revenue * inp.capex_pct
        nwc = revenue * inp.nwc_pct
        d_nwc = nwc - nwc_prev
        fcff = nopat + da - capex - d_nwc
        df = 1.0 / ((1 + wacc) ** year)
        pv = fcff * df
        pv_total += pv
        rows.append({
            "year": year,
            "fiscal_year": inp.base_year + year,
            "growth": gr,
            "margin": mg,
            "revenue": revenue,
            "ebit": ebit,
            "tax": tax,
            "eff_tax_rate": (tax / ebit) if ebit else 0.0,
            "nopat": nopat,
            "da": da,
            "capex": capex,
            "nwc": nwc,
            "delta_nwc": d_nwc,
            "fcff": fcff,
            "discount_factor": df,
            "pv": pv,
            "cum_pv": pv_total,
        })
        revenue_prev, nwc_prev = revenue, nwc

    fcff_n = rows[-1]["fcff"]
    tv = fcff_n * (1 + g) / (wacc - g)
    pv_tv = tv / ((1 + wacc) ** inp.years)
    ev = pv_total + pv_tv
    equity = ev - inp.net_debt
    per_share = (equity / inp.shares) if inp.shares else None
    terminal_year = rows[-1]

    summary = {
        "pv_explicit": pv_total,
        "fcff_next": fcff_n * (1 + g),
        "terminal_value": tv,
        "pv_terminal": pv_tv,
        "ev": ev,
        "net_debt": inp.net_debt,
        "equity": equity,
        "shares": inp.shares,
        "intrinsic_price": per_share,
        "market_price": inp.price,
        "upside": ((per_share / inp.price - 1) if (per_share and inp.price) else None),
        "tv_share": (pv_tv / ev) if ev else 0.0,
        "ev_ebitda": (ev / (inp.ebit + inp.da)) if (inp.ebit + inp.da) else None,
        "ev_sales": (ev / inp.revenue) if inp.revenue else None,
        "wacc": wacc,
        "g": g,
        "terminal_fcff": terminal_year["fcff"],
        "explicit_share": (pv_total / ev) if ev else 0.0,
    }
    return rows, summary


def run(inp: DCFInput, book: Optional[ParamBook] = None) -> DCFResult:
    """主計算：逐年 FCFF → 終值 → EV → 股權 → 內在股價 → 情景／敏感度／風險標籤。"""
    if inp.revenue <= 0:
        raise DCFError("NO_REVENUE", "營業收入為 0，無法建立 DCF 模型。",
                       "請在財報頁確認營業收入，或手動覆寫基期收入。")
    if inp.years < 1:
        raise DCFError("BAD_YEARS", "高速增長年期必須至少 1 年。")
    if not (0 < inp.wacc < 1):
        raise DCFError("BAD_WACC", "WACC 必須介乎 0% 與 100% 之間。")
    if inp.wacc <= inp.g:
        raise DCFError(
            "WACC_LE_G",
            f"WACC（{inp.wacc:.2%}）≤ 永續增長率 g（{inp.g:.2%}），無法計算終值。",
            "請調高 WACC 或調低 g 後再試；這是數學前提，不是模型偏好。",
        )

    rows, summary = _forecast(inp, inp.growth, inp.margin, inp.wacc, inp.g)

    scenarios = _run_scenarios(inp, summary)
    sens = _sensitivity(inp, summary)
    tags = _risk_tags(inp, summary, sens)
    checks = _checks(inp, summary)

    return DCFResult(inp=inp, rows=rows, summary=summary, scenarios=scenarios,
                     sensitivity=sens, tags=tags, checks=checks, book=book)


# ---------------------------------------------------------------- 情景
def _run_scenarios(inp: DCFInput, base_summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for key in ("optimistic", "base", "conservative"):
        off = inp.offsets.get(key) or {}
        gr = [x + _f(off.get("growth")) for x in inp.growth]
        mg = [max(x + _f(off.get("margin")), 0.0) for x in inp.margin]
        wacc = inp.wacc + _f(off.get("wacc"))
        g = inp.g + _f(off.get("g"))
        item: Dict[str, Any] = {
            "key": key,
            "label": SCENARIO_LABELS[key],
            "wacc": wacc,
            "g": g,
            "growth_shift": _f(off.get("growth")),
            "margin_shift": _f(off.get("margin")),
            "valid": True,
            "note": "",
        }
        if wacc <= g:
            item.update({"valid": False, "note": "WACC ≤ g，此情景不成立",
                         "ev": None, "equity": None, "price": None, "tv_share": None})
            out.append(item)
            continue
        try:
            _, s = _forecast(inp, gr, mg, wacc, g)
        except DCFError as exc:
            item.update({"valid": False, "note": exc.message,
                         "ev": None, "equity": None, "price": None, "tv_share": None})
            out.append(item)
            continue
        item.update({
            "ev": s["ev"], "equity": s["equity"],
            "price": s["intrinsic_price"], "tv_share": s["tv_share"],
            "upside": s["upside"],
        })
        out.append(item)
    return out


# ---------------------------------------------------------------- 敏感度
def _sensitivity(inp: DCFInput, base_summary: Dict[str, Any]) -> Dict[str, Any]:
    steps = max(3, min(int(inp.sens_steps), 15))
    if steps % 2 == 0:
        steps += 1
    half = steps // 2

    def axis(center: float, span: float) -> List[float]:
        step = span / half if half else 0.0
        return [round(center + step * (i - half), 6) for i in range(steps)]

    waccs = axis(inp.wacc, inp.wacc_span)
    gs = axis(inp.g, inp.g_span)
    cells: List[List[Optional[float]]] = []
    evs: List[List[Optional[float]]] = []

    for w in waccs:
        row_price: List[Optional[float]] = []
        row_ev: List[Optional[float]] = []
        for gg in gs:
            if w <= gg:
                row_price.append(None)
                row_ev.append(None)
                continue
            _, s = _forecast(inp, inp.growth, inp.margin, w, gg)
            # 無股數時退回「每股 EV」，熱圖仍然可用
            val = s["intrinsic_price"]
            if val is None and inp.shares:
                val = None
            elif val is None:
                val = s["ev"]
            row_price.append(val)
            row_ev.append(s["ev"])
        cells.append(row_price)
        evs.append(row_ev)

    flat = [v for row in cells for v in row if v is not None]
    return {
        "metric": "intrinsic_price",
        "metric_label": "每股內在價值" if inp.shares else "企業價值 EV（無股數，退回企業口徑）",
        "wacc_axis": waccs,
        "g_axis": gs,
        "cells": cells,
        "ev_cells": evs,
        "base_index": [half, half],
        "min": min(flat) if flat else None,
        "max": max(flat) if flat else None,
        "currency": inp.currency,
        "step_price": inp.price,
    }


# ---------------------------------------------------------------- 風險標籤
def _risk_tags(inp: DCFInput, s: Dict[str, Any],
               sens: Dict[str, Any]) -> List[Dict[str, str]]:
    tags: List[Dict[str, str]] = []

    share = s.get("tv_share") or 0.0
    if share > 0.75:
        tags.append({
            "level": "high", "code": "TV_SHARE",
            "title": "估值高度依賴永續假設，主觀性高",
            "detail": f"永續終值佔企業價值 {share:.1%}（門檻 75%）。"
                      f"此結構下，WACC 與 g 各 1 個百分點的變動，就足以主導估值結論。",
        })
    elif share > 0.60:
        tags.append({
            "level": "medium", "code": "TV_SHARE_SOFT",
            "title": "終值佔比偏高",
            "detail": f"永續終值佔企業價值 {share:.1%}，宜同時檢視高速增長期假設是否過於保守。",
        })

    if inp.g > inp.macro_gdp:
        tags.append({
            "level": "medium", "code": "G_ABOVE_GDP",
            "title": "永續增長假設偏樂觀",
            "detail": f"永續增長率 g {inp.g:.2%} 高於宏觀長期名義 GDP 增長 "
                      f"{inp.macro_gdp:.2%}。長期而言，企業不可能永久快於整體經濟增長。",
        })

    if inp.history_years < 3:
        tags.append({
            "level": "medium", "code": "THIN_HISTORY",
            "title": "歷史樣本不足",
            "detail": f"可用歷史財報僅 {inp.history_years} 個年度，"
                      f"各項佔收入比例（ EBIT 利潤率、資本開支、營運資金）穩定性未經足夠驗證。",
        })

    if inp.wacc - inp.g < 0.03:
        tags.append({
            "level": "high", "code": "THIN_SPREAD",
            "title": "折現率與永續增長的差距過小",
            "detail": f"WACC − g 僅 {(inp.wacc - inp.g) * 100:.1f} 個百分點，"
                      f"終值對兩個假設極度敏感，估值區間會非常寬。",
        })

    return tags


def _checks(inp: DCFInput, s: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = [
        {"code": "WACC_GT_G", "ok": inp.wacc > inp.g,
         "label": "WACC > g（永續增長前提）"},
        {"code": "SHARES", "ok": inp.shares > 0,
         "label": "已發行股數 > 0（可計算內在股價）"},
        {"code": "HISTORY", "ok": inp.history_years >= 3,
         "label": "歷史財報 ≥ 3 年"},
        {"code": "TV_SHARE", "ok": (s.get("tv_share") or 0) <= 0.75,
         "label": "終值佔 EV ≤ 75%"},
        {"code": "MARGIN_SANE", "ok": 0 < (inp.margin[-1] if inp.margin else 0) < 0.6,
         "label": "EBIT 利潤率在合理區間（0–60%）"},
    ]
    return out


# ---------------------------------------------------------------- 溯源
def build_book(inp: DCFInput, base: BaseExtract, state: Dict[str, Any]) -> ParamBook:
    """把這次計算用到的每一個假設寫進參數帳（含來源與鎖定狀態）。"""
    locks = state.get("locks") or {}
    book = ParamBook("dcf")
    cur = f"{inp.currency} {inp.unit}".strip()

    base_defs = [
        ("revenue", "基期營業收入", inp.revenue, ""),
        ("ebit", "基期 EBIT", inp.ebit, ""),
        ("da", "折舊及攤銷", inp.da, ""),
        ("capex", "資本開支", inp.capex, ""),
        ("nwc", "營運資金", inp.nwc, ""),
        ("net_debt", "淨負債", inp.net_debt, ""),
        ("shares", "已發行股數（百萬股）", inp.shares, "百萬股"),
        ("price", "現價", inp.price, ""),
    ]
    for key, label, value, unit in base_defs:
        src = "manual" if locks.get(key) else base.sources.get(key, "auto")
        note = base.notes.get(key, "")
        if locks.get(key):
            note = "使用者手動覆寫，重新整理財報時不會被覆蓋"
        book.set(key, label, value, unit or cur, src, note, group="base",
                 locked=bool(locks.get(key)))

    book.set("years", "高速增長年期", inp.years, "年",
             "manual" if locks.get("years") else "default", group="assumption")
    book.set("wacc", "折現率 WACC", inp.wacc, "%", state.get("wacc_source") or "input",
             state.get("wacc_note") or "", group="assumption",
             locked=bool(locks.get("wacc")))
    book.set("g", "永續增長率 g", inp.g, "%", "input", group="assumption",
             locked=bool(locks.get("g")))
    book.set("da_pct", "折舊攤銷 ÷ 收入", inp.da_pct, "%", "input", group="ratio")
    book.set("capex_pct", "資本開支 ÷ 收入", inp.capex_pct, "%", "input", group="ratio")
    book.set("nwc_pct", "營運資金 ÷ 收入", inp.nwc_pct, "%", "input", group="ratio")
    book.set("tax_rate", "有效稅率", inp.tax_rate, "%",
             "manual" if inp.tax_mode == "effective" else "derived",
             "法定稅制計算" if inp.tax_mode == "statutory" else "使用者指定有效稅率",
             group="ratio")
    book.set("tax_regime", "稅制", "香港利得稅（兩級制）" if inp.jurisdiction == "HK"
             else ("澳門所得補充稅・預算案" if inp.mo_regime == "budget" else "澳門所得補充稅・累進"),
             "", "derived", "沿用 tax.py 既有實作", group="tax")
    book.set("macro_gdp", "宏觀長期名義 GDP 增長", inp.macro_gdp, "%",
             "macro" if inp.macro_gdp else "default", "用於永續增長率合理性檢查",
             group="macro")

    for i, (gr, mg) in enumerate(zip(inp.growth, inp.margin), start=1):
        book.set(f"growth_y{i}", f"第 {i} 年營收成長", gr, "%", "input", group="growth")
        book.set(f"margin_y{i}", f"第 {i} 年 EBIT 利潤率", mg, "%", "input", group="margin")

    for key, label in (("optimistic", "樂觀情景偏移"), ("base", "基準情景偏移"),
                       ("conservative", "保守情景偏移")):
        off = inp.offsets.get(key, {})
        book.set(f"offset_{key}", label,
                 f"成長 {_f(off.get('growth'))*100:+.1f}pp／利潤率 {_f(off.get('margin'))*100:+.1f}pp／"
                 f"WACC {_f(off.get('wacc'))*100:+.1f}pp／g {_f(off.get('g'))*100:+.1f}pp",
                 "", "scenario", group="scenario")
    return book


def compute(state: Dict[str, Any], base: BaseExtract,
            company: Optional[Dict[str, Any]] = None) -> DCFResult:
    """一站式：state → input → 計算 → 溯源。"""
    inp = build_input(state, base, company)
    book = build_book(inp, base, state)
    return run(inp, book)
