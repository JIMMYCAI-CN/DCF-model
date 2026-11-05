"""
peers.py — 可比公司樣本池（離線內建示範資料）
================================================================
本機環境無法即時抓取行情，因此內建一份「示範樣本池」，供模組四計算
均值／中位數／上下四分位。所有數字皆為示範用途，不代表任何真實上市公司。

使用方式：
  * 依 meta["industry"] 自動挑選同業；找無對應時退回該市場全部樣本。
  * 使用者可在 UI 直接改表格數值（改過即手動覆寫，不會被自動刷新）。
  * 亦可載入自己的 CSV（欄位：名稱,市場,行業,PE,PB,EV_EBITDA,EV_Sales）。

資料結構：每筆為 dict，鍵：name / market / industry / pe / pb / ev_ebitda /
ev_sales / ebit_margin / growth。數值為 None 代表該指標不適用（例如虧損）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MARKETS = ("HK", "MO", "A")
MARKET_LABELS = {"HK": "港股", "MO": "澳門上市", "A": "A 股"}

#: 行業關鍵字 → 樣本池 bucket
INDUSTRY_BUCKETS: Dict[str, str] = {
    "電子": "electronics", "科技": "electronics", "半導體": "electronics",
    "製造": "electronics", "工業": "electronics",
    "餐飲": "catering", "飲食": "catering", "食品": "catering", "零售": "catering",
    "酒店": "hospitality", "文旅": "hospitality", "博彩": "hospitality",
    "旅遊": "hospitality", "娛樂": "hospitality",
    "公用": "utility", "電力": "utility", "燃氣": "utility", "水務": "utility",
    "地產": "property", "房地產": "property", "物業": "property",
    # 航空／設備租賃（BOC Aviation 等）：高槓桿重資產，倍數特性與工商企業不同
    "leasing": "leasing", "lessor": "leasing", "rental": "leasing",
    "租賃": "leasing", "航空": "leasing", "飛機": "leasing", "aviation": "leasing",
}

_INDUSTRY_LABELS = {
    "electronics": "電子／科技製造", "catering": "餐飲／零售",
    "hospitality": "酒店／文旅", "utility": "公用事業", "property": "地產",
    "leasing": "航空／設備租賃",
}


def _p(name: str, market: str, industry: str, pe: Optional[float], pb: Optional[float],
       ev_ebitda: Optional[float], ev_sales: Optional[float],
       ebit_margin: float, growth: float) -> Dict[str, Any]:
    return {"name": name, "market": market, "industry": industry, "pe": pe, "pb": pb,
            "ev_ebitda": ev_ebitda, "ev_sales": ev_sales,
            "ebit_margin": ebit_margin, "growth": growth}


#: 示範樣本池（全部為虛構名稱，數值僅供操作性示範）
PEERS: List[Dict[str, Any]] = [
    # ---- 港股・電子／科技製造 ----
    _p("港股電子同業 A", "HK", "electronics", 11.2, 1.45, 7.4, 0.92, 0.062, 0.071),
    _p("港股電子同業 B", "HK", "electronics", 9.4, 1.18, 6.5, 0.78, 0.054, 0.055),
    _p("港股電子同業 C", "HK", "electronics", 14.8, 1.92, 9.1, 1.15, 0.081, 0.093),
    _p("港股電子同業 D", "HK", "electronics", 12.6, 1.61, 8.0, 1.02, 0.070, 0.064),
    _p("港股電子同業 E", "HK", "electronics", 8.6, 0.96, 5.9, 0.71, 0.048, 0.038),
    _p("港股電子同業 F", "HK", "electronics", 16.3, 2.15, 10.2, 1.28, 0.088, 0.112),
    _p("港股電子同業 G", "HK", "electronics", 10.5, 1.32, 6.8, 0.86, 0.058, 0.047),
    _p("港股電子同業 H", "HK", "electronics", 13.1, 1.74, 8.4, 1.06, 0.074, 0.083),
    # ---- 港股・餐飲／零售 ----
    _p("港股餐飲同業 A", "HK", "catering", 16.8, 2.10, 8.9, 1.25, 0.081, 0.052),
    _p("港股餐飲同業 B", "HK", "catering", 21.5, 3.05, 11.4, 1.62, 0.098, 0.075),
    _p("港股餐飲同業 C", "HK", "catering", 13.2, 1.65, 7.6, 1.02, 0.072, 0.041),
    _p("港股餐飲同業 D", "HK", "catering", 18.4, 2.42, 9.8, 1.38, 0.089, 0.062),
    _p("港股餐飲同業 E", "HK", "catering", 12.5, 1.52, 7.1, 0.94, 0.066, 0.033),
    _p("港股餐飲同業 F", "HK", "catering", 24.1, 3.36, 12.6, 1.85, 0.106, 0.088),
    _p("港股餐飲同業 G", "HK", "catering", 15.6, 1.98, 8.4, 1.18, 0.078, 0.057),
    # ---- 澳門・酒店／文旅 ----
    _p("澳門文旅同業 A", "MO", "hospitality", 18.2, 2.35, 10.6, 2.05, 0.128, 0.086),
    _p("澳門文旅同業 B", "MO", "hospitality", 24.6, 3.18, 13.4, 2.68, 0.152, 0.121),
    _p("澳門文旅同業 C", "MO", "hospitality", 15.4, 1.86, 9.1, 1.72, 0.108, 0.062),
    _p("澳門文旅同業 D", "MO", "hospitality", 21.8, 2.74, 11.9, 2.34, 0.141, 0.098),
    _p("澳門文旅同業 E", "MO", "hospitality", 13.6, 1.52, 8.2, 1.48, 0.096, 0.045),
    _p("澳門文旅同業 F", "MO", "hospitality", 28.3, 3.65, 15.1, 3.05, 0.168, 0.135),
    _p("澳門文旅同業 G", "MO", "hospitality", 19.5, 2.48, 10.9, 2.12, 0.132, 0.091),
    # ---- 澳門・公用 ----
    _p("澳門公用同業 A", "MO", "utility", 15.2, 1.42, 9.4, 2.86, 0.185, 0.032),
    _p("澳門公用同業 B", "MO", "utility", 17.8, 1.68, 10.6, 3.24, 0.202, 0.041),
    _p("澳門公用同業 C", "MO", "utility", 13.4, 1.24, 8.2, 2.48, 0.168, 0.026),
    _p("澳門公用同業 D", "MO", "utility", 16.5, 1.55, 9.9, 3.02, 0.194, 0.037),
    _p("澳門公用同業 E", "MO", "utility", 14.6, 1.35, 8.8, 2.66, 0.176, 0.029),
    # ---- 港股・公用 ----
    _p("港股公用同業 A", "HK", "utility", 14.8, 1.31, 9.2, 3.10, 0.192, 0.028),
    _p("港股公用同業 B", "HK", "utility", 16.2, 1.48, 10.1, 3.42, 0.205, 0.035),
    _p("港股公用同業 C", "HK", "utility", 12.9, 1.15, 8.1, 2.72, 0.174, 0.022),
    _p("港股公用同業 D", "HK", "utility", 15.6, 1.39, 9.6, 3.26, 0.198, 0.031),
    _p("港股公用同業 E", "HK", "utility", 17.4, 1.62, 10.8, 3.65, 0.212, 0.039),
    # ---- 港股・地產 ----
    _p("港股地產同業 A", "HK", "property", 8.6, 0.42, 9.8, 2.15, 0.224, 0.018),
    _p("港股地產同業 B", "HK", "property", 11.2, 0.58, 11.6, 2.68, 0.238, 0.026),
    _p("港股地產同業 C", "HK", "property", 6.9, 0.35, 8.4, 1.82, 0.206, 0.012),
    _p("港股地產同業 D", "HK", "property", 9.8, 0.49, 10.4, 2.34, 0.231, 0.021),
    _p("港股地產同業 E", "HK", "property", 12.5, 0.64, 12.2, 2.86, 0.245, 0.029),
    # ---- A 股・電子 ----
    _p("A 股電子同業 A", "A", "electronics", 24.6, 3.15, 14.2, 1.86, 0.092, 0.142),
    _p("A 股電子同業 B", "A", "electronics", 31.2, 4.02, 17.8, 2.28, 0.108, 0.186),
    _p("A 股電子同業 C", "A", "electronics", 19.8, 2.64, 12.1, 1.58, 0.081, 0.116),
    _p("A 股電子同業 D", "A", "electronics", 28.4, 3.68, 16.2, 2.06, 0.099, 0.164),
    _p("A 股電子同業 E", "A", "electronics", 22.1, 2.86, 13.4, 1.72, 0.086, 0.128),
    _p("A 股電子同業 F", "A", "electronics", 35.6, 4.58, 20.4, 2.62, 0.124, 0.205),
    # ---- A 股・餐飲 ----
    _p("A 股餐飲同業 A", "A", "catering", 28.5, 3.42, 15.6, 2.05, 0.112, 0.098),
    _p("A 股餐飲同業 B", "A", "catering", 36.2, 4.36, 19.8, 2.58, 0.128, 0.132),
    _p("A 股餐飲同業 C", "A", "catering", 21.4, 2.68, 12.4, 1.68, 0.094, 0.074),
    _p("A 股餐飲同業 D", "A", "catering", 31.8, 3.85, 17.2, 2.28, 0.118, 0.115),
    _p("A 股餐飲同業 E", "A", "catering", 25.6, 3.10, 14.1, 1.88, 0.104, 0.087),
    # ---- 港股・航空／設備租賃（示範數值，特性：P/B 接近 1、EV/EBITDA 低於製造業） ----
    _p("港股租賃同業 A", "HK", "leasing", 9.4, 0.98, 10.6, 5.10, 0.312, 0.041),
    _p("港股租賃同業 B", "HK", "leasing", 8.1, 0.86, 9.4, 4.62, 0.295, 0.028),
    _p("港股租賃同業 C", "HK", "leasing", 11.2, 1.12, 12.1, 5.84, 0.338, 0.055),
    _p("港股租賃同業 D", "HK", "leasing", 10.3, 1.05, 11.2, 5.35, 0.321, 0.047),
    _p("港股租賃同業 E", "HK", "leasing", 7.6, 0.79, 8.8, 4.28, 0.286, 0.022),
    _p("港股租賃同業 F", "HK", "leasing", 12.4, 1.26, 13.3, 6.42, 0.352, 0.063),
]


def bucket_of(industry: str) -> Optional[str]:
    text = str(industry or "").lower()      # Yahoo 行業名多為英文首字母大寫
    for kw, bucket in INDUSTRY_BUCKETS.items():
        if kw.lower() in text:
            return bucket
    return None


def select(industry: str, markets: List[str], bucket_fallback: bool = True) -> List[Dict[str, Any]]:
    """挑選可比樣本：同市場 ＋ 同行業；樣本不足時退回同市場全部。"""
    bucket = bucket_of(industry)
    pool = [p for p in PEERS if p["market"] in markets]
    if bucket:
        same = [p for p in pool if p["industry"] == bucket]
        if len(same) >= 3 or not bucket_fallback:
            return same
    return pool


def industry_label(industry: str) -> str:
    return _INDUSTRY_LABELS.get(bucket_of(industry) or "", industry or "未分類")


def stats(values: List[Optional[float]]) -> Dict[str, Optional[float]]:
    """均值／中位數／上下四分位（自動剔除 None 與非正數）。"""
    xs = sorted(v for v in values if v is not None and v > 0)
    n = len(xs)
    if n == 0:
        return {"n": 0, "mean": None, "median": None, "q1": None, "q3": None,
                "min": None, "max": None}

    def q(p: float) -> float:
        pos = (n - 1) * p
        lo, hi = int(pos), min(int(pos) + 1, n - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)

    return {"n": n, "mean": sum(xs) / n, "median": q(0.5), "q1": q(0.25),
            "q3": q(0.75), "min": xs[0], "max": xs[-1]}
