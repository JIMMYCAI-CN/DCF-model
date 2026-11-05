"""
diagnostics.py — 企業基本面與估值配套模組（五階杜邦・四合一引擎 v2.0）
================================================================
定位：**參考校準工具**，只輸出參考數據、交叉比對與風險警示。
      绝不覆寫 DCF／NPV 的任何使用者假設；只在「使用者尚未手動修改」時
      提供建議預設值（由呼叫方判斷是否套用）。

四個模組
  1 杜邦分析      五階杜邦：ROE = EBIT 利潤率 × 利息負擔 × 稅負擔 × 資產週轉 × 權益乘數
                  另獨立輸出歷年營收、營收成長率、營收趨勢線與 EBIT 利潤率
  2 信用風險      Altman Z''-Score（非製造業版）＋ 流動／速動／負債率／利息覆蓋
                  含行業調整（航空租賃高槓桿，Z'' 門檻需下移）
  3 絕對估值      Gordon DDM ＋ FCF Yield（對照無風險利率）
  4 相對估值      P/E、P/B、EV/EBITDA、EV/Sales；均值／中位數／四分位反推股價
                  ＋ 與 DDM 方向衝突的成因診斷

共用規則
  * 財報解析一律呼叫 dcfcore.statements（不另寫一套）；
  * 稅制一律走 dcfcore.tax（澳門所得補充稅為預設口徑）；
  * 宏觀參數一律呼叫 dcfcore.macro_analyzer（折現率／無風險利率的唯一來源）；
  * 參數溯源一律用 dcfcore.provenance；
  * 三情景共用一組偏移（營收成長／EBIT 利潤率／Ke），四個模組＋DCF 同步套用；
  * 任何模組資料不足時只計算可行部分，並在 note 說明限制，不拋錯。
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dcfcore import macro_analyzer, statements as st
from dcfcore import tax as taxlib
from dcfcore.models import TaxConfig
from dcfcore.provenance import ParamBook

# ---------------------------------------------------------------- 常數
ALTMAN_COEF = {"x1": 6.56, "x2": 3.26, "x3": 6.72, "x4": 1.05}
ZONE_SAFE, ZONE_GREY, ZONE_DISTRESS = 2.6, 1.1, 1.1

ZONE_LABELS = {"safe": "安全區", "grey": "灰色區", "distress": "危險區"}
ZONE_COLORS = {"safe": "#0CA678", "grey": "#F08C00", "distress": "#E03131"}

SCENARIO_ORDER: Tuple[Tuple[str, str], ...] = (
    ("optimistic", "樂觀"), ("base", "基準"), ("conservative", "保守"))

#: 統一三情景偏移（三個旋鈕）：營收成長 pp／EBIT 利潤率 pp／權益折現率 Ke pp
DEFAULT_SCENARIO_OFFSETS: Dict[str, Dict[str, float]] = {
    "optimistic": {"growth": 0.020, "margin": 0.015, "ke": -0.010},
    "base": {"growth": 0.0, "margin": 0.0, "ke": 0.0},
    "conservative": {"growth": -0.020, "margin": -0.015, "ke": 0.010},
}
SCENARIO_KEYS = ("growth", "margin", "ke")

#: 舊版逐模組預設（保留相容；新程式一律用 scenario）
DEFAULT_OFFSETS = {
    "scenario": {k: dict(v) for k, v in DEFAULT_SCENARIO_OFFSETS.items()},
    "dupont": {"optimistic": {"margin": 0.010, "turnover": 0.03, "em": 0.05},
               "conservative": {"margin": -0.010, "turnover": -0.03, "em": -0.05}},
    "credit": {"optimistic": {"margin": 0.010, "debt": -0.05},
               "conservative": {"margin": -0.010, "debt": 0.05}},
    "ddm": {"optimistic": {"g": 0.005, "ke": -0.005},
            "conservative": {"g": -0.005, "ke": 0.005}},
    "comps": {"optimistic": {"multiple": 0.10}, "conservative": {"multiple": -0.10}},
}

#: 相對估值對「增長／利潤率」偏移的彈性（倍數法情景用；可於 state 覆寫）
COMPS_ELASTICITY = {"growth": 2.5, "margin": 1.5}

#: 杜邦情景：營收增長對資產週轉的傳導係數（資產隨機隊／產能滯後成長）
TURNOVER_TRANSMISSION = 0.5

RATIO_LABELS = {
    "current_ratio": "流動比率", "quick_ratio": "速動比率",
    "debt_ratio": "資產負債率", "interest_coverage": "利息覆蓋率",
}

#: 行業調整規則：只調整「分級門檻」與說明，不改 Z'' 原始計算
INDUSTRY_RULES: Tuple[Dict[str, Any], ...] = (
    {
        "id": "aircraft_leasing",
        "keywords": ("leasing", "lessor", "rental", "aviation", "aircraft",
                     "租賃", "航空", "飛機", "飛機租賃"),
        "label": "航空／設備租賃",
        "z_safe": 1.00, "z_grey": 0.40,
        "note": ("航空租賃屬「高槓桿＋重資產」模式：機隊以有抵押借款融資，"
                 "資產負債率常見 70–80%，流動資產（現金）刻意維持低位。"
                 "Altman 原始 Z'' 門檻（2.6／1.1）以製造業與一般商業為校準基準，"
                 "套用於租賃業會系統性低估其信用體質，故分級門檻下移為 1.00／0.40，"
                 "並改以「利息覆蓋率＋機隊賬面值／淨負債＋負債到期結構」為主軸判讀。"),
        "ratio_note": ("租賃業的流動比率與速動比率天然偏低（短期負債多為機隊相關應付款），"
                       "不宜與一般工商企業直接比較；利息覆蓋率與資產負債率才是核心指標。"),
        "watch": ("需同時追蹤：① 機隊賬面淨值／淨負債（LTV）② 加權平均債務成本與到期分佈 "
                  "③ 承租人地區集中度與租金回收率 ④ 航空週期對租金與殘值的影響。"),
    },
)


def industry_rule(industry: str) -> Optional[Dict[str, Any]]:
    """依行業文字挑選調整規則（找不到則回傳 None，代表用原始門檻）。"""
    text = str(industry or "").lower()
    if not text:
        return None
    for rule in INDUSTRY_RULES:
        if any(kw.lower() in text for kw in rule["keywords"]):
            return dict(rule)
    return None


#: 稅制口徑（預設澳門所得補充稅；只借用 dcfcore.tax 的法定計算，不另寫一套）
TAX_PRESETS: Dict[str, Dict[str, Any]] = {
    "MO": {"label": "澳門所得補充稅（年度預算案：首 60 萬澳門元豁免，超出 12%）",
           "cfg": lambda: TaxConfig(jurisdiction="MO", regime="budget")},
    "HK": {"label": "香港利得稅（法團兩級制：首 200 萬 8.25%，其餘 16.5%）",
           "cfg": lambda: TaxConfig(jurisdiction="HK", regime="hk")},
}


# ---------------------------------------------------------------- 工具
def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    try:
        if a is None or b is None or abs(b) < 1e-12:
            return None
        return float(a) / float(b)
    except (TypeError, ValueError):
        return None


def _avg(xs: Sequence[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def _cagr(first: Optional[float], last: Optional[float], n_years: int) -> Optional[float]:
    if not first or not last or first <= 0 or last <= 0 or n_years <= 0:
        return None
    return (last / first) ** (1.0 / n_years) - 1.0


def _loglin(years: Sequence[int], values: Sequence[Optional[float]]) -> Optional[Dict[str, Any]]:
    """對 ln(營收) 做最小平方回歸 → 年化趨勢增長率（趨勢線）。"""
    pts = [(float(y), math.log(float(v))) for y, v in zip(years, values)
           if v is not None and float(v) > 0]
    if len(pts) < 2:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    if abs(sxx) < 1e-12:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pts)
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((p[1] - my) ** 2 for p in pts)
    ss_res = sum((p[1] - (intercept + slope * p[0])) ** 2 for p in pts)
    return {"growth": math.exp(slope) - 1.0, "slope": slope,
            "intercept": intercept,
            "r2": (1 - ss_res / ss_tot) if ss_tot > 1e-12 else None,
            "fitted": {int(y): math.exp(intercept + slope * y) for y, _ in pts}}


def _median(xs: Sequence[float]) -> Optional[float]:
    vals = sorted(x for x in xs if x is not None)
    n = len(vals)
    if not n:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


def _pct(x: Optional[float], nd: int = 2) -> str:
    return "—" if x is None else f"{x * 100:.{nd}f}%"


def _num(x: Optional[float], nd: int = 2) -> str:
    return "—" if x is None else f"{x:,.{nd}f}"


def _pick(vals: Dict[str, Dict[int, float]], key: str, year: int) -> Optional[float]:
    d = vals.get(key) or {}
    v = d.get(year)
    return None if v is None else float(v)


def _scenario_offsets(offsets: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
    """取得統一三情景偏移（相容舊結構：scenario / 逐模組 / 扁平三情景）。"""
    out = {k: {kk: float(vv) for kk, vv in DEFAULT_SCENARIO_OFFSETS[k].items()}
           for k in DEFAULT_SCENARIO_OFFSETS}
    src = offsets or {}
    if isinstance(src.get("scenario"), dict):
        for k, v in src["scenario"].items():
            if isinstance(v, dict):
                out.setdefault(k, {}).update(
                    {kk: float(vv) for kk, vv in v.items() if kk in SCENARIO_KEYS})
        return out
    for key in ("optimistic", "conservative"):
        v = src.get(key)
        if isinstance(v, dict):
            for kk in SCENARIO_KEYS:
                if kk in v:
                    out[key][kk] = float(v[kk])
    # 極舊結構：由 dupont / ddm / credit 拼出
    if isinstance(src.get("dupont"), dict):
        for key in ("optimistic", "conservative"):
            m = (src["dupont"].get(key) or {}).get("margin")
            if m is not None:
                out[key]["margin"] = float(m)
    if isinstance(src.get("ddm"), dict):
        for key in ("optimistic", "conservative"):
            d = src["ddm"].get(key) or {}
            if d.get("ke") is not None:
                out[key]["ke"] = float(d["ke"])
            if d.get("g") is not None:
                out[key]["growth"] = float(d["g"]) * 2
    return out


def _scen_get(off: Dict[str, Dict[str, float]], key: str, knob: str) -> float:
    if key == "base":
        return 0.0
    return float((off.get(key) or {}).get(knob, 0.0) or 0.0)


# ---------------------------------------------------------------- 年度資料
@dataclass
class YearRow:
    year: int
    # 原始科目
    revenue: Optional[float] = None
    ebit: Optional[float] = None
    pbt: Optional[float] = None
    tax_expense: Optional[float] = None
    net_profit: Optional[float] = None
    da: Optional[float] = None
    capex: Optional[float] = None
    cfo: Optional[float] = None
    dividends: Optional[float] = None
    finance_costs: Optional[float] = None
    total_assets: Optional[float] = None
    total_liabilities: Optional[float] = None
    equity: Optional[float] = None
    current_assets: Optional[float] = None
    current_liabilities: Optional[float] = None
    inventory: Optional[float] = None
    cash: Optional[float] = None
    debt: Optional[float] = None
    retained_earnings: Optional[float] = None
    shares: Optional[float] = None
    # 營收／成長
    rev_yoy: Optional[float] = None
    rev_index: Optional[float] = None
    # 五階杜邦
    ebit_margin: Optional[float] = None
    interest_burden: Optional[float] = None
    interest_clean: bool = True
    tax_burden: Optional[float] = None
    net_margin: Optional[float] = None
    asset_turnover: Optional[float] = None
    equity_multiplier: Optional[float] = None
    roe: Optional[float] = None
    roe_check: Optional[float] = None
    # Altman
    working_capital: Optional[float] = None
    x1: Optional[float] = None
    x2: Optional[float] = None
    x3: Optional[float] = None
    x4: Optional[float] = None
    z: Optional[float] = None
    zone: str = ""
    zone_adj: str = ""
    # 財務比率
    current_ratio: Optional[float] = None
    quick_ratio: Optional[float] = None
    debt_ratio: Optional[float] = None
    interest_coverage: Optional[float] = None
    # 現金
    fcf: Optional[float] = None
    eps: Optional[float] = None
    dps: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class DiagResult:
    company: str
    ticker: str
    jurisdiction: str
    currency: str
    unit: str
    years: List[int]
    price: Optional[float]
    shares: Optional[float]
    industry: str
    rows: List[Dict[str, Any]]
    dupont: Dict[str, Any]
    credit: Dict[str, Any]
    ddm: Dict[str, Any]
    comps: Dict[str, Any]
    summary: Dict[str, Any]
    tags: List[Dict[str, str]] = field(default_factory=list)
    suggestions: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    book: Optional[ParamBook] = None
    engine_version: str = "DIAG 2.0"
    tax: Dict[str, Any] = field(default_factory=dict)
    scenarios: List[Dict[str, Any]] = field(default_factory=list)
    conflict: Dict[str, Any] = field(default_factory=dict)
    dcf_scenarios: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine_version,
            "company": self.company, "ticker": self.ticker,
            "jurisdiction": self.jurisdiction, "currency": self.currency,
            "unit": self.unit, "industry": self.industry,
            "years": self.years, "price": self.price, "shares": self.shares,
            "rows": self.rows,
            "dupont": self.dupont, "credit": self.credit,
            "ddm": self.ddm, "comps": self.comps,
            "summary": self.summary, "tags": self.tags,
            "suggestions": self.suggestions, "notes": self.notes,
            "tax": self.tax, "scenarios": self.scenarios,
            "conflict": self.conflict, "dcf_scenarios": self.dcf_scenarios,
            "provenance": self.book.snapshot() if self.book else [],
        }


# ---------------------------------------------------------------- 主體
class Diagnostics:
    """四合一分析：輸入共用財報解析結果，輸出四個模組的結果 dict。"""

    def __init__(self, parsed, meta: Optional[Dict[str, Any]] = None):
        self.parsed = parsed
        self.meta = dict(meta or {})
        self.years: List[int] = list(getattr(parsed, "years", []) or [])
        self.values: Dict[str, Dict[int, float]] = getattr(parsed, "values", {}) or {}
        self.currency = str(self.meta.get("currency") or getattr(parsed, "currency", "") or "")
        self.unit = str(self.meta.get("unit") or getattr(parsed, "unit", "") or "")
        self.price = self._scalar("price")
        self.shares = self._scalar("shares")
        self.industry = str(self.meta.get("industry") or "")
        self.rule = industry_rule(self.industry)
        self.tax_jur = str(self.meta.get("tax_jur") or "MO").upper()
        if self.tax_jur not in TAX_PRESETS:
            self.tax_jur = "MO"
        self.tax_cfg = TAX_PRESETS[self.tax_jur]["cfg"]()
        self.tax_label = TAX_PRESETS[self.tax_jur]["label"]
        self.rows: List[YearRow] = self._build_rows()
        self.macro = None
        self.notes: List[str] = []
        self.book = ParamBook("diag")
        self._collect_notes()

    # -- 基礎 ----------------------------------------------------------
    def _scalar(self, key: str) -> Optional[float]:
        v = self.meta.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
        d = self.values.get(key) or {}
        if d:
            return float(d[max(d)])
        return None

    def _build_rows(self) -> List[YearRow]:
        out: List[YearRow] = []
        for y in self.years:
            r = YearRow(
                year=y,
                revenue=_pick(self.values, "revenue", y),
                ebit=_pick(self.values, "ebit", y),
                pbt=_pick(self.values, "pbt", y),
                tax_expense=_pick(self.values, "tax_expense", y),
                net_profit=_pick(self.values, "net_profit", y),
                da=_pick(self.values, "depreciation_amortisation", y),
                capex=_pick(self.values, "capex", y),
                cfo=_pick(self.values, "operating_cash_flow", y),
                dividends=_pick(self.values, "dividends_paid", y),
                finance_costs=_pick(self.values, "finance_costs", y),
                total_assets=_pick(self.values, "total_assets", y),
                total_liabilities=_pick(self.values, "total_liabilities", y),
                equity=_pick(self.values, "equity", y),
                current_assets=_pick(self.values, "current_assets", y),
                current_liabilities=_pick(self.values, "current_liabilities", y),
                inventory=_pick(self.values, "inventory", y),
                cash=_pick(self.values, "cash", y),
                debt=_pick(self.values, "debt", y),
                retained_earnings=_pick(self.values, "retained_earnings", y),
                shares=self.shares,
            )
            # 缺流動資產／負債總額時，用已解析的組成分項加總（仍屬同一套解析結果）
            if r.current_assets is None:
                parts = [_pick(self.values, k, y) for k in
                         ("receivables", "inventory", "cash", "other_current_assets")]
                r.current_assets = sum(p for p in parts if p is not None) or None
            if r.current_liabilities is None:
                parts = [_pick(self.values, k, y) for k in
                         ("payables", "other_current_liabilities")]
                if any(p is not None for p in parts):
                    r.current_liabilities = sum(p for p in parts if p is not None)
            # 缺保留溢利時，以權益總額作代理並記錄限制
            if r.retained_earnings is None and r.equity is not None:
                r.retained_earnings = r.equity
                r.__dict__.setdefault("_re_proxy", True)
            self._derive(r, out[-1] if out else None)
            out.append(r)
        # 營收指數（首個有營收年度 = 100）
        base = next((r.revenue for r in out if r.revenue), None)
        for r in out:
            if base and r.revenue:
                r.rev_index = r.revenue / base * 100
        return out

    def _derive(self, r: YearRow, prev: Optional[YearRow]) -> None:
        # 營收成長率
        if prev is not None and prev.revenue and r.revenue:
            r.rev_yoy = r.revenue / prev.revenue - 1.0

        # 五階杜邦
        r.ebit_margin = _div(r.ebit, r.revenue)
        pbt = r.pbt
        if pbt is None and r.ebit is not None:
            if r.finance_costs is not None:
                pbt = r.ebit - abs(r.finance_costs)
            elif r.net_profit is not None and r.tax_expense is not None:
                pbt = r.net_profit + r.tax_expense
        r.pbt = pbt
        r.interest_burden = _div(pbt, r.ebit)
        # 稅前溢利高於 EBIT ⇒ 報表 EBIT 不含處置收益／減值回撥等非營業項目，
        # 此時「利息負擔」實為「EBIT → 稅前」轉換係數，需標記避免誤讀。
        r.interest_clean = (pbt is None or not r.ebit
                            or pbt <= r.ebit * 1.0001)
        r.tax_burden = _div(r.net_profit, pbt)
        ta_avg = r.total_assets
        eq_avg = r.equity
        if prev is not None and prev.total_assets and r.total_assets:
            ta_avg = (prev.total_assets + r.total_assets) / 2
        if prev is not None and prev.equity and r.equity:
            eq_avg = (prev.equity + r.equity) / 2
        r.net_margin = _div(r.net_profit, r.revenue)
        r.asset_turnover = _div(r.revenue, ta_avg)
        r.equity_multiplier = _div(ta_avg, eq_avg)
        five = (r.ebit_margin, r.interest_burden, r.tax_burden,
                r.asset_turnover, r.equity_multiplier)
        if None not in five:
            r.roe_check = five[0] * five[1] * five[2] * five[3] * five[4]
        # 主 ROE 用三因子（淨利率口徑最穩），五階乘積作交叉驗證
        if None not in (r.net_margin, r.asset_turnover, r.equity_multiplier):
            r.roe = r.net_margin * r.asset_turnover * r.equity_multiplier
        elif r.roe_check is not None:
            r.roe = r.roe_check

        # Altman Z''
        r.working_capital = (r.current_assets - r.current_liabilities
                             if None not in (r.current_assets, r.current_liabilities) else None)
        r.x1 = _div(r.working_capital, r.total_assets)
        r.x2 = _div(r.retained_earnings, r.total_assets)
        r.x3 = _div(r.ebit, r.total_assets)
        r.x4 = _div(r.equity, r.total_liabilities)
        if None not in (r.x1, r.x2, r.x3, r.x4):
            r.z = (ALTMAN_COEF["x1"] * r.x1 + ALTMAN_COEF["x2"] * r.x2
                   + ALTMAN_COEF["x3"] * r.x3 + ALTMAN_COEF["x4"] * r.x4)
            r.zone = self._zone(r.z)
            r.zone_adj = self._zone(r.z, adjusted=True)

        # 財務比率
        r.current_ratio = _div(r.current_assets, r.current_liabilities)
        if None not in (r.current_assets, r.inventory, r.current_liabilities):
            r.quick_ratio = _div(r.current_assets - r.inventory, r.current_liabilities)
        r.debt_ratio = _div(r.total_liabilities, r.total_assets)
        if r.finance_costs is not None and abs(r.finance_costs) > 1e-9 and r.ebit is not None:
            r.interest_coverage = r.ebit / abs(r.finance_costs)

        # 現金
        if r.cfo is not None:
            r.fcf = r.cfo - abs(r.capex or 0.0)
        if r.shares and r.net_profit is not None:
            r.eps = _div(r.net_profit, r.shares)
        if r.shares and r.dividends is not None:
            r.dps = _div(abs(r.dividends), r.shares)

    def _zone(self, z: Optional[float], adjusted: bool = False) -> str:
        if z is None:
            return ""
        safe, grey = ZONE_SAFE, ZONE_GREY
        if adjusted and self.rule:
            safe, grey = float(self.rule["z_safe"]), float(self.rule["z_grey"])
        if z > safe:
            return "safe"
        if z >= grey:
            return "grey"
        return "distress"

    def _collect_notes(self) -> None:
        if len(self.years) < 3:
            self.notes.append(f"歷史財報僅 {len(self.years)} 年，趨勢判讀與 CAGR 僅供粗略參考。")
        if not self.price:
            self.notes.append("缺少市價：相對估值與股息收益率無法計算，Z'' 與杜邦不受影響。")
        if not self.shares:
            self.notes.append("缺少發行股數：每股數值（EPS／DPS）與每股估值無法計算。")
        if any(getattr(r, "_re_proxy", False) for r in self.rows):
            self.notes.append("部分年度缺少「保留溢利」，Z'' 的 X2 以權益總額代理（偏保守）。")
        missing_ebit = [r.year for r in self.rows if r.ebit is None or r.revenue is None]
        if missing_ebit:
            self.notes.append("以下年度缺少營收或 EBIT，五階杜邦與營收成長率將留空："
                              + "、".join(str(y) for y in missing_ebit) + "。")
        if self.rule:
            self.notes.append(f"已套用行業調整（{self.rule['label']}）：Z'' 分級門檻下移至 "
                              f"{self.rule['z_safe']:.2f}／{self.rule['z_grey']:.2f}，原始 Z'' 數值不變。")

    # -- 市場資料 ------------------------------------------------------
    def market_cap(self) -> Optional[float]:
        if self.price and self.shares:
            return self.price * self.shares
        return None

    def net_debt(self) -> Optional[float]:
        last = self.rows[-1] if self.rows else None
        if last is None:
            return None
        debt = (last.debt or 0.0) + (0.0)
        return debt - (last.cash or 0.0)

    def fetch_macro(self, simulate_failure: bool = False):
        try:
            self.macro = macro_analyzer.fetch(
                {"jurisdiction": self.meta.get("jurisdiction") or "HK",
                 "industry": self.industry,
                 "market_cap": self.market_cap(), "net_debt": self.net_debt()},
                simulate_failure=simulate_failure)
        except Exception:
            self.macro = None
        return self.macro

    # ============================================================ 稅務
    def tax_info(self) -> Dict[str, Any]:
        """稅制口徑與（若有）報表隱含有效稅率——計算一律走 dcfcore.tax。"""
        last = self.rows[-1] if self.rows else None
        eff = None
        if last is not None and last.pbt and last.tax_expense is not None and last.pbt > 0:
            eff = max(0.0, min(0.6, last.tax_expense / last.pbt))
        sample = 10_000_000.0     # 以大型企業規模取邊際稅率（豁免額影響可忽略）
        marginal = taxlib.marginal_rate(sample, self.tax_cfg)
        blended = _div(taxlib.tax_on_taxable(sample, self.tax_cfg), sample)
        return {
            "jurisdiction": self.tax_jur,
            "label": self.tax_label,
            "statutory_rate": marginal,
            "blended_rate_at_10m": blended,
            "effective_rate_reported": eff,
            "free_threshold": getattr(self.tax_cfg, "free_threshold", None),
            "engine": "dcfcore.tax",
            "note": ("稅額一律以 dcfcore.tax 的法定公式計算（不在此模組另寫一套）；"
                     "報表有效稅率僅作對照。"),
        }

    def after_tax_margin(self, ebit_margin: Optional[float],
                         base_turnover: Optional[float] = None) -> Optional[float]:
        """EBIT 利潤率 → 稅後淨利率（用共用稅務引擎＋邊際稅率近似）。"""
        if ebit_margin is None:
            return None
        rate = self.tax_info().get("statutory_rate") or 0.12
        return ebit_margin * (1 - rate)

    # ============================================================ 模組一
    def run_dupont(self, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        st_dict = state or {}
        ov = st_dict.get("overrides") or {}
        off = _scenario_offsets(st_dict.get("offsets"))
        years = self.years

        # ---- 逐年五階杜邦
        rows: List[Dict[str, Any]] = []
        for r in self.rows:
            margin = ov.get(f"dupont.margin.{r.year}", r.net_margin)
            turnover = ov.get(f"dupont.turnover.{r.year}", r.asset_turnover)
            em = ov.get(f"dupont.em.{r.year}", r.equity_multiplier)
            ebit_margin = ov.get(f"dupont.ebit_margin.{r.year}", r.ebit_margin)
            interest = ov.get(f"dupont.interest.{r.year}", r.interest_burden)
            tax_b = ov.get(f"dupont.tax.{r.year}", r.tax_burden)
            roe = (margin * turnover * em
                   if None not in (margin, turnover, em) else r.roe)
            rows.append({
                "year": r.year, "net_margin": margin, "asset_turnover": turnover,
                "equity_multiplier": em, "roe": roe,
                "ebit_margin": ebit_margin, "interest_burden": interest,
                "interest_clean": r.interest_clean,
                "tax_burden": tax_b,
                "revenue": r.revenue, "ebit": r.ebit, "pbt": r.pbt,
                "net_profit": r.net_profit, "total_assets": r.total_assets,
                "equity": r.equity,
                "five_product": (ebit_margin * interest * tax_b * turnover * em
                                 if None not in (ebit_margin, interest, tax_b,
                                                 turnover, em) else None),
            })
        avg = {k: _avg([x[k] for x in rows]) for k in
               ("net_margin", "asset_turnover", "equity_multiplier", "roe",
                "ebit_margin", "interest_burden", "tax_burden")}

        # ---- 營收序列＋成長率＋趨勢線
        rev_rows = []
        for r in self.rows:
            rev_rows.append({"year": r.year, "revenue": r.revenue,
                             "yoy": r.rev_yoy, "index": r.rev_index,
                             "share_of_latest": None})
        latest_rev = next((x["revenue"] for x in reversed(rev_rows) if x["revenue"]), None)
        for x in rev_rows:
            if latest_rev and x["revenue"]:
                x["share_of_latest"] = x["revenue"] / latest_rev
        rev_vals = [(r.year, r.revenue) for r in self.rows if r.revenue]
        cagr = _cagr(rev_vals[0][1], rev_vals[-1][1],
                     max(len(rev_vals) - 1, 1)) if len(rev_vals) >= 2 else None
        trend = _loglin([y for y, _ in rev_vals], [v for _, v in rev_vals]) if len(rev_vals) >= 3 else None
        yoy_vals = [x["yoy"] for x in rev_rows if x["yoy"] is not None]
        rev = {
            "rows": rev_rows,
            "cagr": cagr,
            "trend_growth": (trend or {}).get("growth"),
            "trend_r2": (trend or {}).get("r2"),
            "fitted": (trend or {}).get("fitted") or {},
            "avg_yoy": _avg(yoy_vals[-3:]) if yoy_vals else None,
            "latest": latest_rev,
            "first_year": rev_vals[0][0] if rev_vals else None,
            "last_year": rev_vals[-1][0] if rev_vals else None,
            "n_years": len(rev_vals),
            "unit": self.unit, "currency": self.currency,
        }

        # ---- EBIT 利潤率（獨立輸出）
        ebit_rows = []
        for r in self.rows:
            ebit_rows.append({
                "year": r.year, "revenue": r.revenue, "ebit": r.ebit,
                "margin": r.ebit_margin, "industry_margin": None, "gap": None,
                "yoy": r.rev_yoy,
            })
        ebit_avg3 = _avg([r.ebit_margin for r in self.rows[-3:]])
        ebit_latest = self.rows[-1].ebit_margin if self.rows else None
        ebit = {"rows": ebit_rows, "avg3": ebit_avg3, "latest": ebit_latest,
                "values": [x["margin"] for x in ebit_rows],
                "years": [x["year"] for x in ebit_rows]}

        driver = self._driver(avg)
        scenarios = self._dupont_scenarios(rows, off)

        tags = []
        if avg["roe"] is not None and avg["roe"] < 0.05:
            tags.append({"level": "med", "code": "LOW_ROE",
                         "title": "股東回報偏低",
                         "detail": f"近年平均 ROE 僅 {_pct(avg['roe'])}，低於一般股本成本要求。"})
        if avg["equity_multiplier"] and avg["equity_multiplier"] > 2.2:
            tags.append({"level": "met", "code": "HIGH_LEV",
                         "title": "ROE 由槓桿推高",
                         "detail": f"權益乘數 {avg['equity_multiplier']:.2f} 倍，ROE 的品質需打折看待。"})
        if avg["roe"] is not None and avg["roe"] > 0.15 and (avg["equity_multiplier"] or 0) < 1.8:
            tags.append({"level": "ok", "code": "QUALITY_ROE",
                         "title": "ROE 由經營效率驅動",
                         "detail": "高 ROE 且槓桿溫和，回報品質較佳。"})
        if cagr is not None and cagr < 0:
            tags.append({"level": "med", "code": "REV_SHRINK",
                         "title": "營收歷史收縮",
                         "detail": f"營收 CAGR {_pct(cagr)}，需確認是週期性回落還是結構性流失。"})
        if trend and cagr is not None and trend["growth"] - cagr > 0.02:
            tags.append({"level": "info", "code": "REV_TREND_UP",
                         "title": "營收趨勢線高於首尾 CAGR",
                         "detail": f"對數趨勢增長 {_pct(trend['growth'])} 高於首尾 CAGR {_pct(cagr)}，"
                                   "代表近年增速加快（或起點基數偏低）。"})
        if avg["interest_burden"] is not None and avg["interest_burden"] < 0.5:
            tags.append({"level": "med", "code": "HEAVY_INTEREST",
                         "title": "利息負擔侵蝕利潤",
                         "detail": f"利息負擔（EBT/EBIT）僅 {avg['interest_burden']:.2f}，"
                                   f"EBIT 有 {100 - avg['interest_burden'] * 100:.0f}% 被財務費用吃掉。"})
        if avg["tax_burden"] is not None and avg["tax_burden"] < 0.6:
            tags.append({"level": "info", "code": "HIGH_TAX",
                         "title": "稅負偏高",
                         "detail": f"稅負擔（淨利/稅前）{avg['tax_burden']:.2f}，"
                                   "可能來自不可扣減項目或跨境預提稅。"})
        bad_years = [x["year"] for x in rows if not x.get("interest_clean", True)]
        if bad_years:
            tags.append({"level": "info", "code": "EBIT_PBT_GAP",
                         "title": "EBIT 與稅前溢利背離",
                         "detail": ("、".join(str(y) for y in bad_years) + " 年度的稅前溢利高於 EBIT，"
                                    "代表報表 EBIT 未包含飛機處置收益／減值回撥等非營業項目。"
                                    "該年度的「利息負擔（EBT/EBIT）」大於 1，應理解為"
                                    "「EBIT → 稅前」的轉換係數，不能直接當成利息侵蝕程度。")})

        return {
            "rows": rows, "avg": avg, "driver": driver,
            "scenarios": scenarios, "tags": tags,
            "rev": rev, "ebit": ebit,
            "cagr": cagr, "rev_cagr": cagr,
            "rev_trend_growth": (trend or {}).get("growth"),
            "hist_ebit_margin": ebit_avg3, "hist_margin": _avg([r.net_margin for r in self.rows[-3:]]),
            "history_years": len(years),
            "scenario_offsets": off,
            "available": bool(rows and avg["roe"] is not None),
            "note": "" if len(years) >= 3 else "樣本年度少於 3 年，CAGR 與平均值的代表性有限。",
        }

    def _driver(self, avg: Dict[str, Optional[float]]) -> Dict[str, str]:
        m, t, e = avg.get("net_margin"), avg.get("asset_turnover"), avg.get("equity_multiplier")
        if None in (m, t, e):
            return {"key": "", "title": "資料不足", "text": "缺少完整因子，無法判定 ROE 來源。"}
        score = {"margin": (m - 0.08) / 0.08,
                 "turnover": (t - 1.0) / 1.0,
                 "leverage": (e - 1.6) / 0.8}
        key = max(score, key=score.get)
        im = avg.get("interest_burden")
        imb = (f"利息負擔 {im:.2f}（EBIT 僅 {im * 100:.0f}% 落到稅前）" if im is not None else "")
        text = {
            "margin": f"ROE 主要由**高淨利率**驅動（平均淨利率 {_pct(m)}）：定價力或成本控制是核心優勢，"
                      f"但需留意利潤率能否持續。" + (f"　{imb}" if imb else ""),
            "turnover": f"ROE 主要由**高資產週轉**驅動（平均週轉率 {t:.2f} 次）：屬薄利多銷、效率型商業模式，"
                        f"對需求波動較敏感。" + (f"　{imb}" if imb else ""),
            "leverage": f"ROE 主要由**財務槓桿**放大（權益乘數 {e:.2f} 倍）：股東回報對利率與再融資條件敏感，"
                        f"景氣下行時風險放大。" + (f"　{imb}" if imb else ""),
        }[key]
        title = {"margin": "利潤率驅動", "turnover": "資產效率驅動",
                 "leverage": "財務槓桿驅動"}[key]
        return {"key": key, "title": title, "text": text}

    def _dupont_scenarios(self, rows: List[Dict[str, Any]],
                          off: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        if not rows:
            return []
        base = rows[-1]
        if None in (base["ebit_margin"], base["asset_turnover"], base["equity_multiplier"]):
            return []
        out = []
        tax_rate = self.tax_info().get("statutory_rate") or 0.12
        for key, label in SCENARIO_ORDER:
            dg = _scen_get(off, key, "growth")
            dm = _scen_get(off, key, "margin")
            ebit_margin = base["ebit_margin"] + dm
            turnover = base["asset_turnover"] * (1 + dg * TURNOVER_TRANSMISSION)
            em = base["equity_multiplier"]
            interest = base["interest_burden"]
            # 稅負擔沿用報表實際比率（含不可扣減項目）；缺值才退回法定稅率（澳門 12%）
            tax_b = base["tax_burden"] if base["tax_burden"] is not None else (1 - tax_rate)
            if interest is not None:
                net_margin = ebit_margin * interest * tax_b
            else:
                net_margin = ebit_margin * tax_b
            roe = net_margin * turnover * em
            out.append({"key": key, "label": label,
                        "growth": dg, "margin": dm, "ke": _scen_get(off, key, "ke"),
                        "ebit_margin": ebit_margin, "interest_burden": interest,
                        "tax_burden": tax_b, "net_margin": net_margin,
                        "asset_turnover": turnover, "equity_multiplier": em,
                        "roe": roe})
        return out

    # ============================================================ 模組二
    def run_credit(self, state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        st_dict = state or {}
        ov = st_dict.get("overrides") or {}
        off = _scenario_offsets(st_dict.get("offsets"))
        rows = []
        for r in self.rows:
            z = ov.get(f"credit.z.{r.year}", r.z)
            rows.append({
                "year": r.year, "z": z,
                "zone": self._zone(z) if z is not None else "",
                "zone_adj": self._zone(z, adjusted=True) if z is not None else "",
                "current_ratio": r.current_ratio, "quick_ratio": r.quick_ratio,
                "debt_ratio": r.debt_ratio, "interest_coverage": r.interest_coverage,
                "x1": r.x1, "x2": r.x2, "x3": r.x3, "x4": r.x4,
                "working_capital": r.working_capital, "total_assets": r.total_assets,
            })
        last = rows[-1] if rows else {}
        tags = []
        z = last.get("z")
        rule = self.rule
        if z is not None:
            zone = last["zone"]
            if zone == "distress":
                detail = f"最新 Z'' = {z:.2f}（<1.1），財務困境機率偏高，需檢視再融資安排。"
                if rule:
                    detail += (f"　惟本業屬{rule['label']}，按行業門檻（安全 >{rule['z_safe']:.2f}）"
                               f"判讀為「{ZONE_LABELS.get(last['zone_adj'], '—')}」。")
                tags.append({"level": "high", "code": "Z_DISTRESS",
                             "title": "Altman Z'' 處於危險區",
                             "detail": detail})
            elif zone == "grey":
                tags.append({"level": "med", "code": "Z_GREY",
                             "title": "Altman Z'' 處於灰色區",
                             "detail": f"最新 Z'' = {z:.2f}（1.1–2.6），信用體質不穩，建議追蹤現金流與到期結構。"})
            else:
                tags.append({"level": "ok", "code": "Z_SAFE",
                             "title": "Altman Z'' 處於安全區",
                             "detail": f"最新 Z'' = {z:.2f}（>2.6），短期違約風險較低。"})
        if rule and z is not None:
            tags.append({"level": "info", "code": "Z_INDUSTRY_ADJ",
                         "title": f"行業調整：{rule['label']}的高槓桿常態",
                         "detail": (f"原始 Z'' {z:.2f} 為「{ZONE_LABELS.get(last['zone'], '—')}」，"
                                    f"按行業門檻（安全 >{rule['z_safe']:.2f}、灰色 ≥{rule['z_grey']:.2f}）"
                                    f"則為「{ZONE_LABELS.get(last['zone_adj'], '—')}」。{rule['note']}")})
        dr = last.get("debt_ratio")
        if dr is not None and dr > 0.6:
            lvl = "med" if rule else "high"
            tags.append({"level": lvl, "code": "HIGH_LEVERAGE",
                         "title": "槓桿偏高" + ("（租賃業常態）" if rule else ""),
                         "detail": f"資產負債率 {_pct(dr)}（>60%），利息與再融資壓力較大。"
                                   + (f"　{rule['ratio_note']}" if rule else "")})
        icr = last.get("interest_coverage")
        if icr is not None and icr < 2.0:
            tags.append({"level": "high", "code": "LOW_ICR",
                         "title": "利息覆蓋不足",
                         "detail": f"利息覆蓋率僅 {icr:.2f} 倍（<2 倍），EBIT 對利息支出的緩衝薄弱。"})
        cr = last.get("current_ratio")
        if cr is not None and cr < 1.0:
            tags.append({"level": "med" if not rule else "info", "code": "LIQUIDITY",
                         "title": "流動性壓力" + ("（租賃業常態：以長期融資匹配機隊）" if rule else ""),
                         "detail": f"流動比率 {cr:.2f}（<1），短期償債需依賴續借或增資。"
                                   + (f"　{rule['ratio_note']}" if rule else "")})
        if not self.price:
            tags.append({"level": "info", "code": "NO_PRICE",
                         "title": "缺少上市股價",
                         "detail": "Z'' 與財務比率不受影響；市值相關指標（P/B、EV 系列）暫不計算。"})

        scenarios = self._credit_scenarios(last, off)
        return {
            "rows": rows, "last": last, "tags": tags, "scenarios": scenarios,
            "industry_rule": rule,
            "thresholds": ({"safe": rule["z_safe"], "grey": rule["z_grey"]}
                           if rule else {"safe": ZONE_SAFE, "grey": ZONE_GREY}),
            "available": any(r["z"] is not None for r in rows),
            "note": "" if self.price else "未提供股價：僅計算帳面比率與 Z''。",
        }

    def _credit_scenarios(self, last: Dict[str, Any],
                          off: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        if not last or last.get("z") is None:
            return []
        out = []
        base_x3 = last.get("x3") or 0.0
        base_x4 = last.get("x4") or 0.0
        turnover = 1.0   # EBIT 利潤率變動 → x3 的換算基礎（營收／總資產）
        if self.rows and self.rows[-1].revenue and last.get("total_assets"):
            turnover = self.rows[-1].revenue / last["total_assets"]
        for key, label in SCENARIO_ORDER:
            dm = _scen_get(off, key, "margin")
            x3 = base_x3 + dm * turnover
            z = (ALTMAN_COEF["x1"] * (last.get("x1") or 0) + ALTMAN_COEF["x2"] * (last.get("x2") or 0)
                 + ALTMAN_COEF["x3"] * x3 + ALTMAN_COEF["x4"] * base_x4)
            out.append({"key": key, "label": label, "z": z, "zone": self._zone(z),
                        "zone_adj": self._zone(z, adjusted=True),
                        "x3": x3, "x4": base_x4,
                        "growth": _scen_get(off, key, "growth"),
                        "margin": dm, "ke": _scen_get(off, key, "ke")})
        return out

    # ============================================================ 模組三
    def run_ddm(self, state: Optional[Dict[str, Any]] = None,
                use_macro: bool = True,
                suggest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        st_dict = state or {}
        ov = st_dict.get("overrides") or {}
        off = _scenario_offsets(st_dict.get("offsets"))
        last = self.rows[-1] if self.rows else None
        notes: List[str] = []
        if last is None:
            return {"available": False, "disabled": True,
                    "reason": "沒有可用年度資料。", "tags": [], "scenarios": []}

        sug = suggest or {}
        dps0 = ov.get("ddm.dps", last.dps)
        payout = _div(last.dps, last.eps) if (last.dps and last.eps) else None
        roe_avg = _avg([r.roe for r in self.rows[-3:]])
        retention = (1 - payout) if payout is not None else None
        g_sustain = retention * roe_avg if (retention is not None and roe_avg) else None

        macro = self.macro if self.macro is not None else (self.fetch_macro() if use_macro else None)
        ke_macro = macro.cost_of_equity if macro else None
        if "ddm.ke" in ov:
            ke = float(ov["ddm.ke"])
            ke_source = "manual"
        elif ke_macro is not None:
            ke = float(ke_macro)
            ke_source = "macro"
        else:
            ke = 0.10
            ke_source = "fallback"
        gdp = float(macro.nominal_gdp_growth) if macro else 0.032
        rev_cagr = sug.get("revenue_cagr")
        cands = [x for x in (g_sustain, gdp) if x is not None]
        g_default = min(cands) if cands else gdp
        # 歷史營收成長率作為上限參考（永續增長不應長期高於歷史營收增長）
        if rev_cagr is not None and rev_cagr > 0:
            g_default = min(g_default, max(rev_cagr, 0.0))
        # 防錯：g 過於貼近 Ke 時，Gordon 結果會爆衝；先壓到 Ke−3pp 以內
        if g_default > ke - 0.03:
            g_default = max(0.0, ke - 0.03)
        g = float(ov.get("ddm.g", g_default))
        notes.append("Ke 來源：" + {"macro": "宏觀 CAPM 權益成本（macro_analyzer）",
                                   "manual": "手動輸入（已鎖定）",
                                   "fallback": "宏觀不可用 → 10% 保守替代（已標示）"}[ke_source])
        notes.append(f"g 預設：min(可持續增長 {_pct(g_sustain)}、名義 GDP {_pct(gdp)}、"
                     f"歷史營收成長 {_pct(rev_cagr)})，並壓在 Ke−3pp 之內。"
                     if rev_cagr is not None else
                     "g 預設：min(可持續增長、名義 GDP)，並壓在 Ke−3pp 之內。")
        g = max(min(g, 0.15), -0.05)

        div_yield = _div(dps0, self.price) if (dps0 and self.price) else None
        mcap = self.market_cap()
        fcf = last.fcf
        fcf_yield = _div(fcf, mcap) if (fcf is not None and mcap) else None
        rf = macro.risk_free if macro else None

        result = {
            "available": True, "disabled": False, "reason": "",
            "dps": dps0, "payout": payout, "retention": retention,
            "g_sustain": g_sustain, "g": g, "ke": ke, "ke_source": ke_source,
            "g_source": ("manual" if "ddm.g" in ov else "derived"),
            "price": None, "upside": None, "div_yield": div_yield,
            "fcf": fcf, "fcf_yield": fcf_yield, "risk_free": rf,
            "market_price": self.price, "notes": notes, "tags": [], "scenarios": [],
        }

        if not dps0 or dps0 <= 0:
            result.update({"disabled": True,
                           "reason": "最近年度沒有派息紀錄，DDM 不適用（已自動停用）；"
                                     "仍可參考下方 FCF Yield。"})
            result["tags"].append({"level": "info", "code": "NO_DIVIDEND",
                                   "title": "無派息紀錄",
                                   "detail": "企業未派息或財報未揭露股息，Gordon DDM 無法成立。"})
        elif ke <= g:
            result.update({"disabled": True,
                           "reason": f"折現率 Ke（{_pct(ke)}）≤ 股息永續增長率 g（{_pct(g)}），"
                                     "Gordon 公式分母 ≤ 0，禁止計算。"})
            result["tags"].append({"level": "high", "code": "KE_LE_G",
                                   "title": "DDM 無法計算：Ke ≤ g",
                                   "detail": "請調低 g 或提高 Ke 後再試。"})
        else:
            price = dps0 * (1 + g) / (ke - g)
            result["price"] = price
            result["upside"] = _div(price - self.price, self.price) if self.price else None
            if self.price:
                gap = (price / self.price - 1)
                if gap > 0.3:
                    result["tags"].append({"level": "med", "code": "DDM_ABOVE",
                                           "title": "DDM 估值高於市價三成以上",
                                           "detail": f"DDM 內在價 {price:,.2f} vs 市價 {self.price:,.2f}"
                                                     f"（{gap * 100:+.1f}%），需確認派息可持續性。"})
                elif gap < -0.3:
                    result["tags"].append({"level": "med", "code": "DDM_BELOW",
                                           "title": "DDM 估值低於市價三成以上",
                                           "detail": f"DDM 內在價 {price:,.2f} vs 市價 {self.price:,.2f}"
                                                     f"（{gap * 100:+.1f}%），市場或已反映更高增長。"})

        if payout is not None and payout > 1.0:
            result["tags"].append({"level": "high", "code": "PAYOUT_OVER",
                                   "title": "派息超出盈利",
                                   "detail": f"派息率 {_pct(payout)} > 100%，派息可能不可持續。"})
        if fcf_yield is not None and rf is not None and fcf_yield < rf:
            result["tags"].append({"level": "med", "code": "FCF_BELOW_RF",
                                   "title": "FCF 收益率低於無風險利率",
                                   "detail": f"FCF 收益率 {_pct(fcf_yield)} < 無風險利率 {_pct(rf)}，"
                                             "現金回報吸引力不足。"})

        result["scenarios"] = self._ddm_scenarios(dps0, ke, g, self.price, off)
        return result

    def _ddm_scenarios(self, dps0, ke, g, price,
                       off: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        if not dps0:
            return []
        out = []
        for key, label in SCENARIO_ORDER:
            dg = _scen_get(off, key, "growth")
            dk = _scen_get(off, key, "ke")
            g2 = g + dg
            ke2 = ke + dk
            if ke2 <= g2:
                out.append({"key": key, "label": label, "g": g2, "ke": ke2,
                            "price": None, "valid": False, "growth": dg, "margin": 0.0})
                continue
            p = dps0 * (1 + g2) / (ke2 - g2)
            out.append({"key": key, "label": label, "g": g2, "ke": ke2,
                        "price": p, "valid": True, "growth": dg,
                        "margin": _scen_get(off, key, "margin"),
                        "upside": _div(p - price, price) if price else None})
        return out

    # ============================================================ 模組四
    def run_comps(self, state: Optional[Dict[str, Any]] = None,
                  peers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        import peers as peer_lib

        st_dict = state or {}
        ov = st_dict.get("overrides") or {}
        off = _scenario_offsets(st_dict.get("offsets"))
        markets = st_dict.get("markets") or ["HK"]
        last = self.rows[-1] if self.rows else None
        if last is None:
            return {"available": False, "rows": [], "tags": [],
                    "reason": "沒有可用年度資料。"}

        mcap = self.market_cap()
        nd = self.net_debt()
        ev = (mcap + nd) if (mcap is not None and nd is not None) else None
        eps = ov.get("comps.eps", last.eps)
        bvps = _div(last.equity, self.shares) if (last.equity and self.shares) else None
        ebitda = (last.ebit or 0) + (last.da or 0) if last.ebit is not None else None

        target = {
            "pe": _div(self.price, eps) if (self.price and eps) else None,
            "pb": _div(mcap, last.equity) if (mcap and last.equity) else None,
            "ev_ebitda": _div(ev, ebitda) if (ev and ebitda and ebitda > 0) else None,
            "ev_sales": _div(ev, last.revenue) if (ev and last.revenue) else None,
        }
        target = {k: ov.get(f"comps.{k}", v) for k, v in target.items()}

        pool = peers if peers is not None else peer_lib.select(self.industry, markets)
        stats = {k: peer_lib.stats([p.get(k) for p in pool])
                 for k in ("pe", "pb", "ev_ebitda", "ev_sales", "ebit_margin", "growth")}

        labels = {"pe": "P/E", "pb": "P/B", "ev_ebitda": "EV/EBITDA", "ev_sales": "EV/Sales"}
        rows = []
        for k in ("pe", "pb", "ev_ebitda", "ev_sales"):
            tm = target.get(k)
            s = stats[k]
            row = {"metric": k, "label": labels[k], "target": tm,
                   "median": s["median"], "mean": s["mean"], "q1": s["q1"], "q3": s["q3"],
                   "n": s["n"], "implied_price": None, "premium": None, "valid": False,
                   "reason": ""}
            if tm is None or tm <= 0:
                row["reason"] = "目標公司此指標不適用（虧損或缺少市值）"
            elif s["n"] < 3 or s["median"] is None:
                row["reason"] = f"可比樣本僅 {s['n']} 家，不足以推估"
            else:
                med = s["median"]
                if k == "pe":
                    implied = med * (eps or 0)
                elif k == "pb":
                    implied = med * (bvps or 0)
                elif k == "ev_ebitda":
                    implied_ev = med * (ebitda or 0)
                    implied = _div(implied_ev - (nd or 0), self.shares)
                else:
                    implied_ev = med * (last.revenue or 0)
                    implied = _div(implied_ev - (nd or 0), self.shares)
                row["implied_price"] = implied
                row["premium"] = _div(implied - self.price, self.price) if (implied and self.price) else None
                row["valid"] = bool(implied and implied > 0)
                if not row["valid"]:
                    row["reason"] = "反推股權價值為負或不合理（淨負債過高）"
            rows.append(row)

        valid_premiums = [r["premium"] for r in rows if r["valid"] and r["premium"] is not None]
        median_premium = _median(valid_premiums)
        verdict, vtext = self._comps_verdict(median_premium)

        tags = []
        if verdict == "expensive":
            tags.append({"level": "med", "code": "COMP_EXPENSIVE",
                         "title": "相對行業顯著高估",
                         "detail": f"各指標反推股價中位數較市價低 {abs(median_premium) * 100:.1f}%"
                                   f"（>30% 視為顯著）。"})
        elif verdict == "cheap":
            tags.append({"level": "ok", "code": "COMP_CHEAP",
                         "title": "相對行業顯著低估",
                         "detail": f"各指標反推股價中位數較市價高 {median_premium * 100:.1f}%。"})
        elif verdict == "fair":
            tags.append({"level": "info", "code": "COMP_FAIR",
                         "title": "處於行業正常區間",
                         "detail": f"反推股價與市價差距 {median_premium * 100:+.1f}%（±30% 內）。"})
        hidden = [r["label"] for r in rows if not r["valid"]]
        if hidden:
            tags.append({"level": "info", "code": "COMP_HIDDEN",
                         "title": "部分指標已隱藏",
                         "detail": "、".join(hidden) + " 不適用或樣本不足，未納入反推。"})
        if len(pool) < 5:
            tags.append({"level": "info", "code": "COMP_SMALL",
                         "title": "可比樣本偏少",
                         "detail": f"目前樣本 {len(pool)} 家，離群值影響較大，建議自行補充同業。"})

        scenarios = self._comps_scenarios(rows, self.price, off)
        implied_prices = [r["implied_price"] for r in rows if r["valid"]]
        return {
            "available": bool(implied_prices), "rows": rows, "target": target,
            "stats": stats, "peers": pool, "n_peers": len(pool),
            "implied_median": _median(implied_prices),
            "implied_low": min(implied_prices) if implied_prices else None,
            "implied_high": max(implied_prices) if implied_prices else None,
            "median_premium": median_premium, "verdict": verdict, "verdict_text": vtext,
            "tags": tags, "scenarios": scenarios,
            "industry": self.industry,
            "industry_label": peer_lib.industry_label(self.industry),
            "industry_margin": stats["ebit_margin"]["median"],
            "industry_growth": stats["growth"]["median"],
            "note": "" if self.price else "缺少市價，僅列示倍數，不作溢價／折價判斷。",
        }

    @staticmethod
    def _comps_verdict(premium: Optional[float]) -> Tuple[str, str]:
        if premium is None:
            return "na", "缺少可用指標，無法判斷相對高低。"
        if premium > 0.3:
            return "cheap", f"以行業中位數反推，目標價較市價高 {premium * 100:.1f}%，相對低估。"
        if premium < -0.3:
            return "expensive", f"以行業中位數反推，目標價較市價低 {abs(premium) * 100:.1f}%，相對高估。"
        return "fair", f"反推股價與市價差距 {premium * 100:+.1f}%，處於行業正常區間。"

    def _comps_scenarios(self, rows, price,
                         off: Dict[str, Dict[str, float]]) -> List[Dict[str, Any]]:
        implied = [r["implied_price"] for r in rows if r["valid"]]
        if not implied:
            return []
        base = _median(implied)
        out = []
        for key, label in SCENARIO_ORDER:
            dg = _scen_get(off, key, "growth")
            dm = _scen_get(off, key, "margin")
            shift = dg * COMPS_ELASTICITY["growth"] + dm * COMPS_ELASTICITY["margin"]
            val = base * (1 + shift)
            out.append({"key": key, "label": label, "price": val,
                        "multiple_shift": shift,
                        "growth": dg, "margin": dm, "ke": _scen_get(off, key, "ke"),
                        "upside": _div(val - price, price) if price else None})
        return out

    # ============================================================ 情景橋接
    def run_dcf_scenarios(self, ctx: Optional[Dict[str, Any]],
                          off: Dict[str, Dict[str, float]]) -> Dict[str, Any]:
        """用共用 DCF 引擎（唯讀、狀態副本）算出三情景股價。

        絕不寫回使用者的 state；僅在 DCF 頁籤已載入財報並有可用 state 時執行。
        若基期 FCFF 為負（重資產租賃業的機隊擴張資本開支），會再以「穩態資本開支
        ＝折舊」重算一次，並明確標示，不偷偷替換使用者的假設。
        """
        if not ctx or ctx.get("base") is None or not ctx.get("state"):
            return {"available": False,
                    "reason": "尚未在「① DCF 股價估值」頁籤載入並可用的財報／假設。"}
        try:
            from dcfcore import dcf as engine
            base = ctx["base"]
            state0 = copy.deepcopy(ctx["state"])
            meta = dict(ctx.get("meta") or {})
            # 稅制一律沿用本模組口徑（預設澳門所得補充稅），不採用公司註冊地以外的假設
            meta["jurisdiction"] = self.tax_jur
            state0["mo_regime"] = "budget"
            state0["offsets"] = {
                "optimistic": {"growth": _scen_get(off, "optimistic", "growth"),
                               "margin": _scen_get(off, "optimistic", "margin"),
                               "wacc": _scen_get(off, "optimistic", "ke"),
                               "g": _scen_get(off, "optimistic", "growth") * TURNOVER_TRANSMISSION},
                "base": {"growth": 0.0, "margin": 0.0, "wacc": 0.0, "g": 0.0},
                "conservative": {"growth": _scen_get(off, "conservative", "growth"),
                                 "margin": _scen_get(off, "conservative", "margin"),
                                 "wacc": _scen_get(off, "conservative", "ke"),
                                 "g": _scen_get(off, "conservative", "growth") * TURNOVER_TRANSMISSION},
            }
            res = engine.compute(state0, base, meta)
            normalised, note = False, ""
            if (res.summary.get("intrinsic_price") or 0) <= 0:
                ratios = getattr(base, "ratios", {}) or {}
                da_pct = float(state0.get("da_pct") or ratios.get("da_pct") or 0.0)
                capex_pct = float(state0.get("capex_pct") or ratios.get("capex_pct") or 0.0)
                if da_pct > 0 and capex_pct > da_pct * 1.2:
                    st2 = copy.deepcopy(state0)
                    st2["capex_pct"] = da_pct
                    res = engine.compute(st2, base, meta)
                    normalised = True
                    note = (f"基期資本開支佔營收 {capex_pct * 100:.0f}%（＝折舊的 "
                            f"{capex_pct / da_pct:.1f} 倍），屬機隊擴張期；原始 FCFF 為負，"
                            f"情景已改以「穩態資本開支＝折舊（{da_pct * 100:.0f}% 營收）」重算。")
            price0 = res.summary.get("intrinsic_price") or 0
            rows = []
            for s in (res.scenarios or []):
                p = s.get("price")
                rows.append({"key": s.get("key"), "label": s.get("label"),
                             "price": p if (p and p > 0) else None,
                             "valid": bool(s.get("valid")) and bool(p and p > 0),
                             "ev": s.get("ev"), "equity": s.get("equity"),
                             "tv_share": s.get("tv_share"),
                             "wacc": s.get("wacc"), "g": s.get("g"),
                             "note": s.get("note", "")})
            priced = price0 > 0
            reason = ""
            if not priced:
                ev0 = res.summary.get("ev") or 0.0
                nd = self.net_debt() or 0.0
                reason = (f"股權價值為負：企業價值 {ev0:,.0f} {self.unit} 低於淨負債 {nd:,.0f}，"
                          "DCF 無法給出正股價（此為高槓桿租賃公司的常見結果：市場以遠高於"
                          "現金流折現的倍數定價）。三情景仍提供企業價值 EV 供對照。")
            return {"available": True, "priced": priced, "rows": rows,
                    "base_price": price0, "base_ev": res.summary.get("ev"),
                    "normalised": normalised, "normalise_note": note,
                    "reason": reason,
                    "tax_note": f"情景 DCF 按{self.tax_label}重算",
                    "offsets_applied": state0["offsets"]}
        except Exception as exc:                              # noqa: BLE001
            return {"available": False, "reason": f"DCF 情景無法計算：{exc}"}

    # ============================================================ 匯總
    def run_all(self, state: Optional[Dict[str, Any]] = None,
                dcf: Optional[Dict[str, Any]] = None,
                peers: Optional[List[Dict[str, Any]]] = None,
                dcf_ctx: Optional[Dict[str, Any]] = None) -> DiagResult:
        st_dict = state or {}
        off = _scenario_offsets(st_dict.get("offsets"))
        dupont = self.run_dupont(st_dict)
        credit = self.run_credit(st_dict)
        ddm = self.run_ddm(st_dict, suggest=dupont)
        comps = self.run_comps(st_dict, peers)
        dcf_sc = self.run_dcf_scenarios(dcf_ctx, off)

        # 行業參考值回填到 EBIT 利潤率表／營收表
        ind_margin = comps.get("industry_margin")
        ind_growth = comps.get("industry_growth")
        for row in dupont["ebit"]["rows"]:
            if ind_margin is not None and row["margin"] is not None:
                row["industry_margin"] = ind_margin
                row["gap"] = row["margin"] - ind_margin
        for row in dupont["rev"]["rows"]:
            row["industry_growth"] = ind_growth

        tags = []
        for mod in (dupont, credit, ddm, comps):
            tags.extend(mod.get("tags") or [])
        tags = self._dedupe_tags(tags)

        scenarios = self._scenario_table(dupont, credit, ddm, comps, dcf_sc, off)
        conflict = self._conflict(ddm, comps)
        suggestions = {
            "ebit_margin": dupont.get("hist_ebit_margin"),
            "revenue_cagr": dupont.get("cagr"),
            "revenue_trend": dupont.get("rev_trend_growth"),
            "industry_margin": comps.get("industry_margin"),
            "industry_growth": comps.get("industry_growth"),
            "industry_label": comps.get("industry_label"),
            "ke": ddm.get("ke"), "ke_source": ddm.get("ke_source"),
            "ddm_g": ddm.get("g"), "payout": ddm.get("payout"),
            "tax": self.tax_info(),
        }
        summary = self._summary(dupont, credit, ddm, comps, dcf, conflict,
                                scenarios, dcf_sc)

        res = DiagResult(
            company=str(self.meta.get("name") or self.meta.get("company_name") or ""),
            ticker=str(self.meta.get("ticker") or ""),
            jurisdiction=str(self.meta.get("jurisdiction") or ""),
            currency=self.currency, unit=self.unit,
            years=self.years, price=self.price, shares=self.shares,
            industry=self.industry,
            rows=[r.to_dict() for r in self.rows],
            dupont=dupont, credit=credit, ddm=ddm, comps=comps,
            summary=summary, tags=tags, suggestions=suggestions,
            notes=list(self.notes) + [n for n in (ddm.get("notes") or [])],
            book=self._book(dupont, ddm, comps),
            tax=self.tax_info(), scenarios=scenarios,
            conflict=conflict, dcf_scenarios=dcf_sc,
        )
        return res

    def _scenario_table(self, dupont, credit, ddm, comps, dcf_sc,
                        off) -> List[Dict[str, Any]]:
        """三情景 × 各模型：同一組偏移，輸出各模型的回應。"""
        dmap = {s["key"]: s for s in (dupont.get("scenarios") or [])}
        cmap = {s["key"]: s for s in (credit.get("scenarios") or [])}
        mmap = {s["key"]: s for s in (ddm.get("scenarios") or [])}
        pmap = {s["key"]: s for s in (comps.get("scenarios") or [])}
        fmap = {s.get("key"): s for s in ((dcf_sc or {}).get("rows") or [])}
        out = []
        for key, label in SCENARIO_ORDER:
            dg = _scen_get(off, key, "growth")
            dm = _scen_get(off, key, "margin")
            dk = _scen_get(off, key, "ke")
            d = dmap.get(key) or {}
            out.append({
                "key": key, "label": label,
                "growth": dg, "margin": dm, "ke": dk,
                "ebit_margin": d.get("ebit_margin"),
                "roe": d.get("roe"),
                "z": (cmap.get(key) or {}).get("z"),
                "zone": (cmap.get(key) or {}).get("zone"),
                "ddm_price": (mmap.get(key) or {}).get("price"),
                "ddm_valid": bool((mmap.get(key) or {}).get("valid")),
                "comps_price": (pmap.get(key) or {}).get("price"),
                "dcf_price": (fmap.get(key) or {}).get("price"),
                "dcf_ev": (fmap.get(key) or {}).get("ev"),
                "dcf_valid": bool((fmap.get(key) or {}).get("valid")),
                "dcf_note": (fmap.get(key) or {}).get("note", ""),
            })
        return out

    # -- 衝突診斷 ------------------------------------------------------
    def _conflict(self, ddm: Dict[str, Any], comps: Dict[str, Any]) -> Dict[str, Any]:
        """DDM 與相對估值方向衝突的成因診斷。"""
        price = self.price
        dp = ddm.get("price")
        cp = comps.get("implied_median")
        base = {"conflict": False, "ddm_upside": None, "comps_upside": None,
                "gap": None, "text": "", "causes": [], "advice": ""}
        if not price or not dp or not cp:
            base["text"] = ("缺少市價或其中一種方法的結果，無法判斷 DDM 與相對估值是否方向衝突。"
                            + ("" if ddm.get("price") else f"　DDM：{ddm.get('reason', '不適用')}"))
            return base
        du = dp / price - 1
        cu = cp / price - 1
        base.update({"ddm_upside": du, "comps_upside": cu, "gap": dp / cp - 1})
        conflict = (du * cu < 0) and min(abs(du), abs(cu)) > 0.10
        base["conflict"] = conflict

        causes: List[str] = []
        last = self.rows[-1] if self.rows else None
        payout = ddm.get("payout")
        if payout is not None and payout < 0.45:
            causes.append(f"派息率僅 {_pct(payout)}：DDM 只認「已派出的現金股息」，"
                          "保留盈餘留在公司內不進 DDM 分子，因此對高留存／再投資型公司會系統性偏低；"
                          "而倍數法（P/B、EV/EBITDA）反映的是整體資產與現金流的定價。")
        if payout is not None and payout > 0.9:
            causes.append(f"派息率達 {_pct(payout)}：DDM 對「派息可持續性」極度敏感，"
                          "一旦市場預期減派，DDM 會給出偏高的估值，而倍數法未必同步調整。")
        if last is not None and last.debt_ratio is not None and last.debt_ratio > 0.65:
            causes.append(f"高槓桿（資產負債率 {_pct(last.debt_ratio)}）：DDM 用股權現金流、"
                          "對利息與再融資成本高度敏感；倍數法中的 EV/EBITDA 屬「債務前」口徑，"
                          "能把槓桿帶來的資產規模計入，兩者對同一家公司會給出不同結論。")
        if self.rule:
            causes.append(f"{self.rule['label']}屬重資產、週期性行業：DDM 的永續增長假設無法表達"
                          "「租金週期＋機隊殘值」的波動，倍數法（尤其 P/B）則直接錨定機隊賬面值，"
                          "在週期高點／低點容易與 DDM 反向。")
        if comps.get("n_peers", 0) < 5:
            causes.append(f"可比樣本僅 {comps.get('n_peers')} 家：中位數易受個別公司（規模、"
                          "機隊機型、承租人結構）影響，反推價的參考度下降。")
        if ddm.get("g") is not None and ddm.get("g") > 0.05:
            causes.append(f"DDM 的 g = {_pct(ddm['g'])} 偏高：永續增長對終值極敏感，"
                          "g 每上調 1pp、內在價可跳升雙位數百分點，容易與倍數法背離。")
        ke_src = ddm.get("ke_source")
        if ke_src == "fallback":
            causes.append("Ke 來自宏觀模型不可用時的保守替代值（10%）：折現率偏差會直接放大 DDM 與市場定價的差距。")
        if not causes:
            causes.append("兩種方法隐含的假設不同：DDM 以「股息現金流＋永續增長」定價，"
                          "相對估值以「同業當下的倍數」定價，短中期背離屬正常現象。")

        if conflict:
            text = (f"方向衝突：DDM 內在價 {dp:,.2f}（{du * 100:+.1f}%）認為"
                    f"{'低估' if du > 0 else '高估'}，但相對估值中位數 {cp:,.2f}"
                    f"（{cu * 100:+.1f}%）認為{'低估' if cu > 0 else '高估'}。")
            advice = ("處理方式：① 以 DCF（企業自由現金流）為主錨，因為它同時計入債務前現金流與"
                      "再投資需求；② 把 DDM 當作「派息政策下的下限」，不是內在價的全部；"
                      "③ 檢查同業樣本是否同類（規模、機隊、槓桿），必要時縮小樣本；"
                      "④ 若兩者差距 > 30%，優先檢視派息率、g 與 Ke 三個假設，而非直接平均。")
        else:
            text = (f"方向一致：DDM {dp:,.2f}（{du * 100:+.1f}%）與相對估值 {cp:,.2f}"
                    f"（{cu * 100:+.1f}%）同向，差距 {abs(dp / cp - 1) * 100:.1f}%。")
            advice = ("方向一致時，仍應注意幅度差異：若差距 > 20%，代表市場對增長或派息的"
                      "預期與同業倍數隱含的假設不同，建議以情景分析檢視。")
        return {**base, "text": text, "causes": causes, "advice": advice}

    @staticmethod
    def _dedupe_tags(tags: List[Dict[str, str]]) -> List[Dict[str, str]]:
        seen, out = set(), []
        for t in tags:
            code = t.get("code")
            if code in seen:
                continue
            seen.add(code)
            out.append(t)
        order = {"high": 0, "med": 1, "info": 2, "ok": 3}
        return sorted(out, key=lambda t: order.get(t.get("level", "info"), 2))

    def _summary(self, dupont, credit, ddm, comps, dcf,
                 conflict: Dict[str, Any],
                 scenarios: List[Dict[str, Any]],
                 dcf_sc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """整合四個模組＋（若存在）DCF，輸出交叉對照。"""
        lines: List[str] = []
        if dupont.get("available"):
            avg = dupont["avg"]
            rev = dupont.get("rev") or {}
            lines.append(f"基本面：近年平均 ROE {_pct(avg['roe'])}，"
                         f"{dupont['driver']['title']}（EBIT 利潤率 {_pct(avg.get('ebit_margin'))}、"
                         f"利息負擔 {_num(avg.get('interest_burden'))}、稅負擔 {_num(avg.get('tax_burden'))}、"
                         f"週轉 {_num(avg.get('asset_turnover'))} 次、乘數 {_num(avg.get('equity_multiplier'))} 倍）。")
            if rev.get("cagr") is not None:
                lines.append(f"營收：{rev.get('first_year')}–{rev.get('last_year')} 由 "
                             f"{_num(rev.get('latest'))} {self.unit} 起算，CAGR {_pct(rev['cagr'])}，"
                             f"對數趨勢增長 {_pct(rev.get('trend_growth'))}"
                             + (f"（R² {rev['trend_r2']:.2f}）" if rev.get("trend_r2") is not None else "")
                             + f"；最近三年平均成長 {_pct(rev.get('avg_yoy'))}。")
        zlast = (credit.get("last") or {}).get("z")
        if zlast is not None:
            zone_txt = ZONE_LABELS.get(credit['last']['zone'], '—')
            if self.rule:
                zone_txt += f"／行業調整後 {ZONE_LABELS.get(credit['last'].get('zone_adj'), '—')}"
            lines.append(f"信用：Altman Z'' {zlast:.2f}（{zone_txt}），"
                         f"資產負債率 {_pct(credit['last'].get('debt_ratio'))}、"
                         f"流動比率 {_num(credit['last'].get('current_ratio'))}、"
                         f"利息覆蓋 {_num(credit['last'].get('interest_coverage'))} 倍。")
        if ddm.get("price"):
            lines.append(f"絕對估值（DDM）：內在價 {ddm['price']:,.2f}"
                         + (f"，較市價 {ddm['upside'] * 100:+.1f}%" if ddm.get("upside") is not None else "")
                         + f"；股息率 {_pct(ddm.get('div_yield'))}；Ke {_pct(ddm.get('ke'))}"
                         f"（{'宏觀模型' if ddm.get('ke_source') == 'macro' else ddm.get('ke_source')}）"
                         f"、g {_pct(ddm.get('g'))}。")
        elif ddm.get("disabled"):
            lines.append(f"絕對估值（DDM）：不適用——{ddm.get('reason')}")
        if comps.get("implied_median"):
            lines.append(f"相對估值：行業中位數反推 {comps['implied_median']:,.2f}"
                         f"（區間 {comps['implied_low']:,.2f}–{comps['implied_high']:,.2f}），"
                         f"{comps['verdict_text']}")

        if conflict.get("text"):
            lines.append("交叉檢查：" + conflict["text"])

        band = [x for x in (ddm.get("price"), comps.get("implied_median"),
                            (dcf or {}).get("intrinsic_price")) if x]
        cross = {}
        if dcf and dcf.get("intrinsic_price"):
            gap = None
            if comps.get("implied_median"):
                gap = dcf["intrinsic_price"] / comps["implied_median"] - 1
            cross = {"dcf_price": dcf["intrinsic_price"], "vs_comps": gap}
            if gap is not None:
                lines.append(f"交叉對照：DCF 內在價 {dcf['intrinsic_price']:,.2f} 較可比反推"
                             f"{comps['implied_median']:,.2f} {'高' if gap > 0 else '低'} {abs(gap) * 100:.1f}%。")
            if band:
                lines.append(f"三種方法價格區間 {min(band):,.2f} – {max(band):,.2f}；"
                             "若 DCF 落在此區間外，建議檢查增長與折現率假設。")

        dcf_band = [s["dcf_price"] for s in scenarios if s.get("dcf_price")]
        if len(dcf_band) >= 2:
            lines.append(f"三情景（營收成長／EBIT 利潤率／Ke 同一組偏移）：DCF 股價 "
                         f"{min(dcf_band):,.2f} – {max(dcf_band):,.2f}；"
                         f"DDM {_spread([s['ddm_price'] for s in scenarios])}；"
                         f"可比 {_spread([s['comps_price'] for s in scenarios])}。")
            if (dcf_sc or {}).get("normalised"):
                lines.append("DCF 情景說明：" + (dcf_sc or {}).get("normalise_note", ""))
        elif dcf_sc and not dcf_sc.get("available") and dcf_sc.get("reason"):
            lines.append("DCF 情景：" + dcf_sc["reason"])
        elif dcf_sc and not dcf_sc.get("priced"):
            evs = [s.get("dcf_ev") for s in scenarios if s.get("dcf_ev")]
            if evs:
                lines.append(f"三情景（營收成長／EBIT 利潤率／Ke 同一組偏移）：DCF 企業價值 "
                             f"{min(evs):,.0f} – {max(evs):,.0f} {self.unit}；"
                             f"DDM {_spread([s['ddm_price'] for s in scenarios])}；"
                             f"可比 {_spread([s['comps_price'] for s in scenarios])}。")
            lines.append("DCF 情景：" + (dcf_sc.get("reason") or "股價不適用"))

        tax = self.tax_info()
        lines.append(f"稅制：{tax['label']}；本模組所有稅額與稅後利潤換算均呼叫 "
                     f"dcfcore.tax（法定邊際稅率 {_pct(tax['statutory_rate'])}，"
                     f"報表隱含有效稅率 {_pct(tax.get('effective_rate_reported'))} 僅作對照）。")

        if len(band) < 2:
            verdict = "僅單一方法可得價格，建議補齊其他方法或市值資料後再交叉驗證"
        elif max(band) / min(band) < 1.35:
            verdict = "估值區間收斂，結論較穩健"
        else:
            verdict = "各方法差異較大，建議以情景分析檢視關鍵假設"
        text = "\n".join(lines) + "\n" + verdict
        return {"lines": lines, "verdict": verdict, "band": band, "cross": cross,
                "text": text, "scenarios": scenarios}

    def _book(self, dupont, ddm, comps) -> ParamBook:
        b = self.book
        rev = dupont.get("rev") or {}
        for key, label, val, src, unit in (
            ("rev_cagr", "歷史營收 CAGR", dupont.get("cagr"), "derived", "%"),
            ("rev_trend", "營收對數趨勢增長", dupont.get("rev_trend_growth"), "derived", "%"),
            ("hist_ebit_margin", "歷史平均 EBIT 利潤率", dupont.get("hist_ebit_margin"),
             "derived", "%"),
            ("hist_net_margin", "歷史平均淨利率", dupont.get("hist_margin"), "derived", "%"),
            ("ddm_g", "DDM 永續增長率 g", ddm.get("g"), "derived", "%"),
            ("ddm_ke", "DDM 折現率 Ke", ddm.get("ke"),
             "macro" if ddm.get("ke_source") == "macro" else "manual", "%"),
            ("industry_margin", "行業 EBIT 利潤率中位數", comps.get("industry_margin"),
             "auto", "%"),
            ("industry_growth", "行業成長預期中位數", comps.get("industry_growth"),
             "auto", "%"),
        ):
            if val is None:
                continue
            b.set(key, label, val, unit=unit, source=src,
                  note="企業診斷模組（僅供參考，不覆寫 DCF 假設）")
        b.set("tax_jur", "稅制口徑", self.tax_jur, unit="", source="legal",
              note=self.tax_label)
        return b


def _spread(vals: Sequence[Optional[float]]) -> str:
    xs = [v for v in vals if v]
    if not xs:
        return "n/a"
    if len(xs) == 1:
        return f"{xs[0]:,.2f}"
    return f"{min(xs):,.2f} – {max(xs):,.2f}"


# ---------------------------------------------------------------- 便捷入口
def analyse(parsed, meta: Optional[Dict[str, Any]] = None,
            state: Optional[Dict[str, Any]] = None,
            dcf: Optional[Dict[str, Any]] = None,
            dcf_ctx: Optional[Dict[str, Any]] = None) -> DiagResult:
    """外部呼叫：一次跑完四模組並回傳 DiagResult。"""
    return Diagnostics(parsed, meta).run_all(state, dcf, dcf_ctx=dcf_ctx)
