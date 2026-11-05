"""
statements.py — 財報解析分流引擎（v3.0 新增）
=========================================================
負責「把一份 IFRS 財報數表，變成估值模型可以直接吃的基期資料」。

三段流程（分流 → 對映 → 萃取）
    1. 分流 route
       判斷每一列屬於 損益表 / 資產負債表 / 現金流量表 / 備忘指標，
       與工作底稿標籤不一致時，以「科目性質」為準並留下 warning。
    2. 對映 map
       中英雙語別名表（IFRS 常見用語 ＋ 香港／澳門年報慣用譯法）→ 標準科目。
       對不到的列不會被默默丟掉，全部收進 `unmatched` 供使用者檢查。
    3. 萃取 extract
       算出基期 EBIT、折舊攤銷、資本開支、營運資金，
       再換算成「各項佔營業收入比例」作為 DCF 的預設假設。

重要口徑
--------
* 營運資金 NWC = 應收帳款 + 存貨 + 其他營運性流動資產 − 應付帳款 − 其他營運性流動負債
  （不含現金與計息負債；這是 FCFF 的口徑，和 NPV 專案的 NWC 計劃是兩件事。）
* EBIT 一律為「息稅前、扣除折舊攤銷後」的營業利益；
  若報表只給 EBITDA，則 EBIT = EBITDA − 折舊攤銷，並標記為 derived。
* 資本開支取現金流量表「購置物業廠房及設備」；若缺，改用資產負債表 PPE 變動推估，
  並在 note 說明推估方式。
* 稅率取「所得稅費用 ÷ 除稅前溢利」，僅作參考；DCF 實際課稅仍走 tax.py 的法定稅制。

本模組只做「讀帳」，不含任何折現、預測或估值邏輯。
"""

from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SECTION_LABELS = {
    "income": "損益表",
    "balance": "資產負債表",
    "cashflow": "現金流量表",
    "metric": "備忘指標",
}


# ---------------------------------------------------------------- 科目定義
@dataclass(frozen=True)
class ItemSpec:
    key: str
    label: str
    section: str
    aliases: Tuple[str, ...]
    kind: str = "flow"        # flow（期間數）｜ stock（期末數）
    note: str = ""


def _spec(key: str, label: str, section: str, aliases: Sequence[str],
          kind: str = "flow", note: str = "") -> ItemSpec:
    return ItemSpec(key, label, section, tuple(aliases), kind, note)


SPECS: Tuple[ItemSpec, ...] = (
    # ---- 損益表 ----
    _spec("revenue", "營業收入", "income",
          ("revenue", "turnover", "sales", "total revenue", "net sales",
           "營業收入", "收入", "收益", "營業額", "銷售收入", "主營業務收入", "總收入")),
    _spec("cost_of_sales", "銷貨成本", "income",
          ("cost of sales", "cost of revenue", "cost of goods sold", "cogs",
           "銷貨成本", "營業成本", "銷售成本", "主營業務成本")),
    _spec("gross_profit", "毛利", "income", ("gross profit", "毛利", "毛損")),
    _spec("admin_expenses", "營業費用", "income",
          ("administrative expenses", "selling and distribution expenses",
           "operating expenses", "distribution costs", "staff costs",
           "行政費用", "銷售及分銷費用", "營業費用", "經營費用", "分銷成本")),
    _spec("other_gains", "其他收益", "income",
          ("other gains", "other income", "other net income", "其他收益", "其他收入")),
    _spec("depreciation_amortisation", "折舊及攤銷", "income",
          ("depreciation and amortisation", "depreciation and amortization",
           "depreciation", "amortisation", "amortization",
           "折舊及攤銷", "折舊", "攤銷", "折舊與攤銷")),
    _spec("ebit", "息稅前營業利益", "income",
          ("operating profit", "profit from operations", "ebit",
           "營業利益", "經營溢利", "營業利潤", "息稅前利潤")),
    _spec("ebitda", "EBITDA", "income", ("ebitda", "息稅折舊攤銷前利潤")),
    _spec("finance_costs", "財務費用", "income",
          ("finance costs", "interest expense", "finance charges",
           "財務費用", "融資成本", "利息支出", "財務成本")),
    _spec("pbt", "除稅前溢利", "income",
          ("profit before tax", "profit before taxation", "pbt",
           "除稅前溢利", "稅前利潤", "除稅前利潤")),
    _spec("tax_expense", "所得稅費用", "income",
          ("income tax expense", "taxation", "income tax", "profit tax",
           "所得稅費用", "利得稅", "稅項", "所得補充稅")),
    _spec("net_profit", "年度溢利", "income",
          ("profit for the year", "net profit", "profit attributable to shareholders",
           "年度溢利", "淨利潤", "股東應佔溢利")),

    # ---- 資產負債表 ----
    _spec("receivables", "應收帳款", "balance",
          ("trade receivables", "accounts receivable", "trade and other receivables",
           "應收帳款", "貿易應收款項", "應收貿易款項"), "stock"),
    _spec("inventory", "存貨", "balance", ("inventories", "inventory", "存貨"), "stock"),
    _spec("payables", "應付帳款", "balance",
          ("trade payables", "accounts payable", "trade and other payables",
           "應付帳款", "貿易應付款項", "應付貿易款項"), "stock"),
    _spec("other_current_assets", "其他流動資產", "balance",
          ("other current assets", "prepayments and other receivables",
           "其他流動資產", "預付款項"), "stock"),
    _spec("other_current_liabilities", "其他流動負債", "balance",
          ("other current liabilities", "accruals and other payables",
           "其他流動負債", "應計費用"), "stock"),
    _spec("current_assets", "流動資產總額", "balance",
          ("current assets", "total current assets", "流動資產", "流動資產總額"), "stock"),
    _spec("current_liabilities", "流動負債總額", "balance",
          ("current liabilities", "total current liabilities", "流動負債", "流動負債總額"), "stock"),
    _spec("cash", "現金及現金等價物", "balance",
          ("cash and cash equivalents", "bank balances and cash", "cash",
           "現金及現金等價物", "銀行結餘及現金", "貨幣資金"), "stock"),
    _spec("ppe", "物業廠房及設備", "balance",
          ("property plant and equipment", "property, plant and equipment", "ppe",
           "fixed assets", "物業廠房及設備", "固定資產"), "stock"),
    _spec("total_assets", "資產總額", "balance",
          ("total assets", "資產總額", "總資產"), "stock"),
    _spec("total_liabilities", "負債總額", "balance",
          ("total liabilities", "負債總額", "總負債"), "stock"),
    _spec("equity", "權益總額", "balance",
          ("total equity", "equity", "shareholders equity", "權益總額", "股東權益"), "stock"),
    _spec("debt", "計息負債", "balance",
          ("bank borrowings", "borrowings", "interest bearing borrowings", "loans",
           "bank loans", "銀行借款", "借款", "計息負債", "貸款"), "stock"),
    _spec("lease_liabilities", "租賃負債", "balance",
          ("lease liabilities", "租賃負債"), "stock"),
    _spec("minority", "非控股權益", "balance",
          ("non-controlling interests", "minority interests", "非控股權益", "少數股東權益"), "stock"),
    # ---- 企業診斷模組（杜邦／Altman Z''）所需之補充科目：純新增，不影響既有萃取 ----
    _spec("retained_earnings", "保留溢利", "balance",
          ("retained earnings", "retained profits", "accumulated profits",
           "保留溢利", "留存收益", "未分配利潤", "累計溢利"), "stock"),

    # ---- 現金流量表 ----
    _spec("capex", "資本開支", "cashflow",
          ("purchase of property plant and equipment", "capital expenditure",
           "purchases of property, plant and equipment", "additions to ppe",
           "購置物業廠房及設備", "資本開支", "購建固定資產")),
    _spec("operating_cash_flow", "經營活動現金流", "cashflow",
          ("net cash from operating activities", "net cash generated from operating activities",
           "operating cash flow", "經營活動現金流", "經營業務現金流入淨額")),
    _spec("dividends_paid", "已付股息", "cashflow",
          ("dividends paid", "dividend paid", "dividends paid to shareholders",
           "已付股息", "派付股息", "支付的股利"), note="現金流量表融資活動（正數＝派息金額）"),

    # ---- 備忘指標 ----
    _spec("shares", "已發行股數", "metric",
          ("number of shares", "shares outstanding", "issued shares",
           "已發行股數", "股數", "普通股股數"), "stock"),
    _spec("price", "股價", "metric",
          ("share price", "closing price", "股價", "收市價"), "stock"),
    _spec("eps", "每股盈利", "metric",
          ("basic earnings per share", "earnings per share", "basic eps", "eps",
           "每股基本盈利", "每股盈利", "每股收益"), "stock"),
)

SPEC_BY_KEY: Dict[str, ItemSpec] = {s.key: s for s in SPECS}
SECTION_OF: Dict[str, str] = {s.key: s.section for s in SPECS}
LABELS: Dict[str, str] = {s.key: s.label for s in SPECS}

_PUNCT = re.compile(r"[\s\u3000\-_/·、,，.。:：;；()（）\[\]【】'\"’“”&＋+]")
_SUFFIX = re.compile(r"(?<=\w)s$")


def norm(text: Any) -> str:
    """把科目名正規化：全小寫、去標點空白、去尾端複數 s。"""
    s = str(text or "").strip().lower()
    s = _PUNCT.sub("", s)
    s = _SUFFIX.sub("", s)
    return s


_ALIAS_INDEX: Dict[str, List[Tuple[str, int]]] = {}
for _s in SPECS:
    for _a in (_s.label,) + _s.aliases:
        _ALIAS_INDEX.setdefault(norm(_a), []).append((_s.key, len(norm(_a))))


def _segments(name: str) -> List[str]:
    """
    把「中英並列」的科目名拆成片段。

    年報常見寫法：`Purchase of property, plant and equipment 購置物業廠房及設備`
    整串正規化後中英黏在一起，永遠對不到別名；先拆成拉丁片段與中文片段，
    再逐段比對，命中率與準確度都會好很多。
    """
    out: List[str] = []
    cur: List[str] = []
    cls = ""
    for ch in str(name or ""):
        if ch.isascii() and (ch.isalnum() or ch in "&.,'-/ "):
            c = "L"
        elif "\u4e00" <= ch <= "\u9fff":
            c = "C"
        else:
            c = ""
        if c != cls and cur:
            out.append("".join(cur))
            cur = []
        cls = c
        if c:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return [s for s in out if len(norm(s)) >= 2]


def match_item(name: str) -> Tuple[Optional[str], str, float]:
    """
    科目名 → 標準科目。

    回傳 (key, reason, score)；score 0 代表對不到。
    比對順序：完全相符 → 前後綴相符 → 包含（別名需 ≥ 6 字元）。
    中英並列的科目名會先拆段，避免中文別名把英文別名的完全相符蓋掉。
    """
    candidates = [str(name or "")] + _segments(name)
    best: Tuple[Optional[str], str, float] = (None, "", 0.0)

    for cand in candidates:
        n = norm(cand)
        if not n:
            continue
        hits = _ALIAS_INDEX.get(n)
        if hits:
            key, ln = sorted(hits, key=lambda kv: -kv[1])[0]
            score = 100.0 + min(ln, 20)
            if score > best[2]:
                best = (key, "完全相符", score)
            continue
        for alias, entries in _ALIAS_INDEX.items():
            if len(alias) < 4:
                continue
            if n.startswith(alias) or n.endswith(alias):
                for key, ln in entries:
                    score = 70.0 + min(ln, 20)
                    if score > best[2]:
                        best = (key, f"前後綴相符（{alias}）", score)
            elif alias in n and len(alias) >= 6:
                for key, ln in entries:
                    score = 50.0 + min(ln, 20)
                    if score > best[2]:
                        best = (key, f"包含相符（{alias}）", score)

    if best[0] is None:
        return None, "無法對映（不在 IFRS 標準科目表）", 0.0
    return best


# ---------------------------------------------------------------- 資料結構
@dataclass
class RouteRow:
    raw: str
    section: str          # 分流後的報表
    key: Optional[str]
    label: str
    reason: str
    score: float
    declared_section: str = ""
    rerouted: bool = False


@dataclass
class ParsedStatements:
    meta: Dict[str, Any]
    years: List[int]
    values: Dict[str, Dict[int, float]]       # key -> {year: value}
    routes: List[RouteRow]
    unmatched: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    currency: str = ""
    unit: str = ""

    # -- 便捷 ---------------------------------------------------------
    def series(self, key: str) -> Dict[int, float]:
        return self.values.get(key, {})

    def latest(self, key: str, year: Optional[int] = None) -> Optional[float]:
        s = self.values.get(key)
        if not s:
            return None
        if year is not None and year in s:
            return s[year]
        return s[max(s)]

    def has(self, key: str) -> bool:
        return bool(self.values.get(key))

    def section_count(self) -> Dict[str, int]:
        out = {k: 0 for k in SECTION_LABELS}
        for r in self.routes:
            if r.key:
                out[r.section] = out.get(r.section, 0) + 1
        return out


@dataclass
class BaseExtract:
    """基期資料 + 佔收入比例（DCF 的預設假設來源）。"""

    base_year: int
    years: List[int]
    history_years: int
    currency: str
    unit: str
    scale: float = 1.0
    values: Dict[str, float] = field(default_factory=dict)
    ratios: Dict[str, float] = field(default_factory=dict)
    sources: Dict[str, str] = field(default_factory=dict)      # auto / derived
    notes: Dict[str, str] = field(default_factory=dict)
    history: List[Dict[str, float]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_year": self.base_year,
            "years": self.years,
            "history_years": self.history_years,
            "currency": self.currency,
            "unit": self.unit,
            "scale": self.scale,
            "values": self.values,
            "ratios": self.ratios,
            "sources": self.sources,
            "notes": self.notes,
            "history": self.history,
            "warnings": self.warnings,
            "missing": self.missing,
        }


# ---------------------------------------------------------------- 解析入口
def _as_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).strip().replace(",", "").replace(" ", "")
        if not s or s in {"-", "—", "N/A", "n.a.", "NA"}:
            return None
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()")
        try:
            f = float(s)
        except ValueError:
            return None
        if neg:
            f = -f
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _year_of(label: Any) -> Optional[int]:
    m = re.search(r"(19|20)\d{2}", str(label))
    return int(m.group(0)) if m else None


#: 報表單位 → 換算成「元」的倍數（稅制門檻、每股盈餘都要用絕對金額）
UNIT_SCALES: Tuple[Tuple[str, float], ...] = (
    ("十億", 1e9), ("billion", 1e9), ("bn", 1e9),
    ("百萬", 1e6), ("million", 1e6), ("mn", 1e6),
    ("千萬", 1e7),
    ("千", 1e3), ("thousand", 1e3),
    ("元", 1.0), ("", 1.0),
)


def infer_scale(unit: Any) -> float:
    """
    由單位文字推斷倍數。`百萬港元` → 1,000,000；`千美元` → 1,000；`元` → 1。

    DCF 內部的稅額計算必須用絕對金額（HK 兩級制首 200 萬、MO 豁免 60 萬），
    這裡的倍數就是「報表單位 ↔ 絕對金額」的橋樑。
    """
    text = str(unit or "").strip().lower().replace(" ", "")
    if not text:
        return 1.0
    for token, mult in UNIT_SCALES:
        if token and token in text:
            return mult
    return 1.0


def parse_payload(payload: Dict[str, Any]) -> ParsedStatements:
    """
    解析一份「公司財報 JSON」。

    標準格式：
        {"meta": {"name": "...", "currency": "HKD", "unit": "百萬", ...},
         "statements": {"income": {"Revenue": {"2025": 4520}}, "balance": {...}}}

    也接受扁平格式（沒有 statements，直接把科目放最上層）。
    """
    meta = dict(payload.get("meta") or {})
    blocks = payload.get("statements") or {}
    if not blocks:
        blocks = {"": {k: v for k, v in payload.items()
                       if k not in ("meta", "statements")}}

    warnings: List[str] = list(payload.get("warnings") or [])
    values: Dict[str, Dict[int, float]] = {}
    routes: List[RouteRow] = []
    unmatched: List[str] = []
    years: set[int] = set()

    for declared, rows in blocks.items():
        if not isinstance(rows, dict):
            continue
        for raw_name, series in rows.items():
            if not isinstance(series, dict):
                continue
            key, reason, score = match_item(raw_name)
            if key is None:
                unmatched.append(str(raw_name))
                routes.append(RouteRow(str(raw_name), "unmatched", None, "",
                                       reason or "無法對映", score, declared))
                continue

            spec = SPEC_BY_KEY[key]
            rerouted = bool(declared) and declared != spec.section
            if rerouted:
                warnings.append(
                    f"科目「{raw_name}」原列於 {SECTION_LABELS.get(declared, declared)}，"
                    f"依科目性質重新分流至 {SECTION_LABELS[spec.section]}。")

            bucket = values.setdefault(key, {})
            for yl, v in series.items():
                y = _year_of(yl)
                f = _as_float(v)
                if y is None or f is None:
                    continue
                bucket[y] = f
                years.add(y)

            routes.append(RouteRow(str(raw_name), spec.section, key, spec.label,
                                   reason, score, declared, rerouted))

    # 重複對映到同一科目（例如同時有 "Revenue" 與 "Turnover"）時提示
    seen: Dict[str, int] = {}
    for r in routes:
        if r.key:
            seen[r.key] = seen.get(r.key, 0) + 1
    for key, n in seen.items():
        if n > 1 and key in values:
            warnings.append(
                f"科目「{LABELS[key]}」有 {n} 個來源列，已依年份合併（同年以最後一列為準）。")

    years_sorted = sorted(years)
    if not years_sorted:
        warnings.append("未偵測到任何年度欄位，請確認表頭格式（例如 2023／2024／2025）。")

    return ParsedStatements(
        meta=meta, years=years_sorted, values=values, routes=routes,
        unmatched=unmatched, warnings=warnings,
        currency=str(meta.get("currency") or ""),
        unit=str(meta.get("unit") or ""),
    )


def parse_csv(text: str, meta: Optional[Dict[str, Any]] = None) -> ParsedStatements:
    """
    解析 CSV 財報數表。

    寬表：第一欄科目、其餘欄年度（2023, 2024, 2025A …）
    長表：欄位含「科目/項目」＋「年度」＋「金額」
    """
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("CSV 內容為空")

    header = [c.strip() for c in rows[0]]
    body = rows[1:]
    idx_item = 0
    year_cols = [(i, _year_of(h)) for i, h in enumerate(header) if _year_of(h)]
    long_mode = any(h in ("年度", "year", "Year", "期間") for h in header)

    statements: Dict[str, Dict[str, Dict[str, float]]] = {"": {}}
    if long_mode:
        i_year = next(i for i, h in enumerate(header) if h in ("年度", "year", "Year", "期間"))
        i_val = next((i for i, h in enumerate(header)
                      if h in ("金額", "amount", "Amount", "數值")), len(header) - 1)
        for r in body:
            if len(r) <= max(i_item, i_year, i_val):
                continue
            name = r[i_item].strip()
            y = _year_of(r[i_year])
            v = _as_float(r[i_val])
            if not name or y is None or v is None:
                continue
            statements[""].setdefault(name, {})[str(y)] = v
    else:
        if not year_cols:
            raise ValueError("CSV 表頭找不到年度欄（例如 2023, 2024, 2025）")
        for r in body:
            if not r:
                continue
            name = r[0].strip()
            if not name:
                continue
            series: Dict[str, float] = {}
            for i, y in year_cols:
                if i < len(r):
                    v = _as_float(r[i])
                    if v is not None:
                        series[str(y)] = v
            if series:
                statements[""].setdefault(name, {}).update(series)

    payload = {"meta": meta or {}, "statements": statements}
    parsed = parse_payload(payload)
    if not parsed.currency:
        parsed.currency = str((meta or {}).get("currency") or "")
    return parsed


# ---------------------------------------------------------------- 萃取
def _pick(parsed: ParsedStatements, key: str, year: int) -> Optional[float]:
    s = parsed.values.get(key)
    if not s:
        return None
    if year in s:
        return s[year]
    earlier = [y for y in s if y <= year]
    return s[max(earlier)] if earlier else s[min(s)]


def _avg_ratio(parsed: ParsedStatements, num: str, den: str = "revenue",
               years: int = 3, absolute: bool = False) -> Optional[float]:
    ns, ds = parsed.values.get(num), parsed.values.get(den)
    if not ns or not ds:
        return None
    ratios = []
    for y in sorted(ds)[-years:]:
        if y in ns and ds[y]:
            v = abs(ns[y]) if absolute else ns[y]
            ratios.append(v / ds[y])
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


def nwc_series(parsed: ParsedStatements) -> Dict[int, float]:
    """逐年營運資金（口徑與 extract_base 一致），供歷史比例與圖表用。"""
    out: Dict[int, float] = {}
    for y in parsed.years:
        recv = _pick(parsed, "receivables", y)
        pay = _pick(parsed, "payables", y)
        if recv is not None and pay is not None:
            out[y] = (recv + (_pick(parsed, "inventory", y) or 0.0)
                      + (_pick(parsed, "other_current_assets", y) or 0.0)
                      - pay - (_pick(parsed, "other_current_liabilities", y) or 0.0))
            continue
        ca, cl = _pick(parsed, "current_assets", y), _pick(parsed, "current_liabilities", y)
        if ca is not None and cl is not None:
            out[y] = ((ca - (_pick(parsed, "cash", y) or 0.0))
                      - (cl - (_pick(parsed, "debt", y) or 0.0)))
    return out


def extract_base(parsed: ParsedStatements, base_year: Optional[int] = None) -> BaseExtract:
    """把已解析財報壓成「基期 + 比例」的估值輸入。"""
    if not parsed.years:
        raise ValueError("財報沒有可用年度，無法萃取基期資料")

    years = list(parsed.years)
    year = base_year or years[-1]
    if year not in years:
        near = [y for y in years if y <= year]
        year = near[-1] if near else years[-1]

    warnings: List[str] = []
    missing: List[str] = []
    values: Dict[str, float] = {}
    sources: Dict[str, str] = {}
    notes: Dict[str, str] = {}

    revenue = _pick(parsed, "revenue", year)
    if revenue is None:
        raise ValueError("財報缺少「營業收入」，無法建立 DCF 模型")
    values["revenue"] = revenue
    sources["revenue"] = "auto"

    # -- 損益表 ------------------------------------------------------
    cos = _pick(parsed, "cost_of_sales", year)
    opex = _pick(parsed, "admin_expenses", year)
    other = _pick(parsed, "other_gains", year) or 0.0
    da = _pick(parsed, "depreciation_amortisation", year)

    ebit = _pick(parsed, "ebit", year)
    if ebit is None:
        ebitda = _pick(parsed, "ebitda", year)
        if ebitda is not None and da is not None:
            ebit = ebitda - da
            sources["ebit"] = "derived"
            notes["ebit"] = "由 EBITDA − 折舊攤銷 推導"
        elif cos is not None:
            opex_v = opex or 0.0
            ebit = revenue - cos - opex_v + other
            if da is not None:
                # opex 若已含折舊（部分年報把 D&A 併入行政費用），不重複扣
                notes["ebit"] = "由 收入 − 銷貨成本 − 營業費用 ＋ 其他收益 推導"
            sources["ebit"] = "derived"
        else:
            missing.append("EBIT")
            warnings.append("財報未提供營業利益，且無法由收入與成本推導，請手動填寫 EBIT。")
            ebit = 0.0
            sources["ebit"] = "default"
    else:
        sources["ebit"] = "auto"
    values["ebit"] = ebit

    if da is None:
        # 以 PPE 變動推估：D&A ≈ 期初 PPE − 期末 PPE + 資本開支
        ppe_now = _pick(parsed, "ppe", year)
        ppe_prev = parsed.values.get("ppe", {}).get(year - 1)
        if ppe_now is not None and ppe_prev is not None:
            capex0 = _pick(parsed, "capex", year) or 0.0
            da = max(ppe_prev - ppe_now + capex0, 0.0)
            sources["da"] = "derived"
            notes["da"] = "財報未單列折舊攤銷，以 PPE 期初期末變動 ＋ 資本開支 推估"
            warnings.append("折舊攤銷為推估值，建議以年報附註核對後手動覆寫。")
        else:
            da = 0.0
            sources["da"] = "default"
            missing.append("折舊攤銷")
            warnings.append("財報缺少折舊攤銷，暫以 0 處理，請手動填寫。")
    else:
        sources["da"] = "auto"
    values["da"] = da
    values["ebitda"] = ebit + da

    # -- 資本開支 ----------------------------------------------------
    capex = _pick(parsed, "capex", year)
    if capex is not None:
        capex = abs(capex)
        sources["capex"] = "auto"
    else:
        capex = 0.0
        sources["capex"] = "default"
        missing.append("資本開支")
        warnings.append("現金流量表未提供資本開支，暫以 0 處理，請手動填寫。")
    values["capex"] = capex

    # -- 營運資金 ----------------------------------------------------
    recv = _pick(parsed, "receivables", year)
    inv = _pick(parsed, "inventory", year)
    pay = _pick(parsed, "payables", year)
    oca = _pick(parsed, "other_current_assets", year) or 0.0
    ocl = _pick(parsed, "other_current_liabilities", year) or 0.0
    if recv is not None and pay is not None:
        nwc = recv + (inv or 0.0) + oca - pay - ocl
        sources["nwc"] = "derived"
        notes["nwc"] = "NWC = 應收帳款 ＋ 存貨 ＋ 其他流動資產 − 應付帳款 − 其他流動負債"
    else:
        ca, cl = _pick(parsed, "current_assets", year), _pick(parsed, "current_liabilities", year)
        cash = _pick(parsed, "cash", year) or 0.0
        debt = _pick(parsed, "debt", year) or 0.0
        if ca is not None and cl is not None:
            nwc = (ca - cash) - (cl - debt)
            sources["nwc"] = "derived"
            notes["nwc"] = "以（流動資產 − 現金）−（流動負債 − 計息負債）推估"
            warnings.append("營運資金為推估值（未逐項取得應收／存貨／應付），建議核對後覆寫。")
        else:
            nwc = 0.0
            sources["nwc"] = "default"
            missing.append("營運資金")
            warnings.append("財報缺少營運資金科目，暫以 0 處理，請手動填寫。")
    values["nwc"] = nwc

    # -- 稅與資本結構 ------------------------------------------------
    tax_exp = _pick(parsed, "tax_expense", year)
    pbt = _pick(parsed, "pbt", year)
    if tax_exp is not None and pbt:
        eff_tax = max(min(tax_exp / pbt, 0.5), 0.0)
        sources["tax_rate"] = "derived"
        notes["tax_rate"] = f"基期有效稅率 = 所得稅費用 {tax_exp:,.0f} ÷ 除稅前溢利 {pbt:,.0f}"
    else:
        eff_tax = 0.165 if (parsed.currency or "").upper() == "HKD" else 0.12
        sources["tax_rate"] = "default"
        notes["tax_rate"] = "財報未提供稅項，暫以當地法定標準稅率代入"
    values["tax_rate"] = eff_tax

    debt = _pick(parsed, "debt", year) or 0.0
    cash = _pick(parsed, "cash", year) or 0.0
    minority = _pick(parsed, "minority", year) or 0.0
    shares = _pick(parsed, "shares", year) or float(parsed.meta.get("shares") or 0.0)
    price = _pick(parsed, "price", year) or float(parsed.meta.get("price") or 0.0)

    values["debt"] = debt
    values["cash"] = cash
    values["minority"] = minority
    values["shares"] = shares
    values["price"] = price
    values["net_debt"] = debt + minority - cash
    values["equity_book"] = _pick(parsed, "equity", year) or 0.0
    sources.setdefault("debt", "auto" if debt else "default")
    sources.setdefault("shares", "auto" if shares else "default")

    if shares <= 0:
        missing.append("已發行股數")
        warnings.append("缺少已發行股數，內在股價將無法計算（企業價值仍可得出）。")

    # -- 比例假設 ----------------------------------------------------
    def ratio(key: str, base_value: Optional[float] = None) -> float:
        v = values[key] if base_value is None else base_value
        return (v / revenue) if revenue else 0.0

    ratios = {
        "ebit_margin": ratio("ebit"),
        "da_pct": ratio("da"),
        "capex_pct": ratio("capex"),
        "nwc_pct": ratio("nwc"),
        "tax_rate": eff_tax,
    }

    # 歷史比例（3 年平均，僅作 UI 參考，不自動取代基期值）
    nwc_s = nwc_series(parsed)
    rev_s = parsed.values.get("revenue", {})
    nwc_ratios = [nwc_s[y] / rev_s[y] for y in sorted(nwc_s) if y in rev_s and rev_s[y]]
    hist_ratios = {
        "ebit_margin_3y": _avg_ratio(parsed, "ebit"),
        "da_pct_3y": _avg_ratio(parsed, "depreciation_amortisation"),
        "capex_pct_3y": _avg_ratio(parsed, "capex", absolute=True),
        "nwc_pct_3y": (sum(nwc_ratios[-3:]) / len(nwc_ratios[-3:])) if nwc_ratios else None,
    }

    # -- 歷史序列 ----------------------------------------------------
    history: List[Dict[str, float]] = []
    for y in years:
        r = parsed.values.get("revenue", {}).get(y)
        if r is None:
            continue
        e = parsed.values.get("ebit", {}).get(y)
        if e is None and parsed.has("ebitda"):
            e = (parsed.values["ebitda"].get(y) or 0) - (parsed.values.get("depreciation_amortisation", {}).get(y) or 0)
        history.append({
            "year": y,
            "revenue": r,
            "ebit": e if e is not None else 0.0,
            "ebit_margin": (e / r) if (e is not None and r) else 0.0,
        })

    if len(years) < 3:
        warnings.append(f"可用歷史財報僅 {len(years)} 個年度，歷史樣本不足。")

    return BaseExtract(
        base_year=year, years=years, history_years=len(years),
        currency=parsed.currency, unit=parsed.unit,
        scale=infer_scale(parsed.unit),
        values=values, ratios={**ratios, **hist_ratios},
        sources=sources, notes=notes, history=history,
        warnings=warnings, missing=missing,
    )


def parse_company(payload: Dict[str, Any], base_year: Optional[int] = None
                  ) -> Tuple[ParsedStatements, BaseExtract]:
    parsed = parse_payload(payload)
    return parsed, extract_base(parsed, base_year)


def routing_summary(parsed: ParsedStatements) -> List[Dict[str, Any]]:
    """給 UI 的「分流結果」表格：每一列去了哪張報表、用什麼理由。"""
    out = []
    for r in parsed.routes:
        out.append({
            "raw": r.raw,
            "section": r.section,
            "section_label": SECTION_LABELS.get(r.section, "未對映"),
            "key": r.key,
            "label": r.label,
            "reason": r.reason,
            "score": round(r.score, 1),
            "declared": SECTION_LABELS.get(r.declared_section, r.declared_section),
            "rerouted": r.rerouted,
        })
    return out
