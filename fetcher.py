"""
fetcher.py — 自動資料抓取（免匯入檔案）
================================================================
用 Yahoo Finance 的公開端點，直接抓「年度財報 + 現價 + 股數 + 估值倍數」，
轉成本程式既有的財報 payload 格式，交給 dcfcore.statements 解析。

設計原則
  * 不改變任何既有演算：抓回來的資料一律走原解析器 → 基期萃取 → DCF。
  * 抓不到不能死：先試快取；快取也沒有就回傳明確錯誤，UI 保留手動輸入。
  * 全部落地快取（cache/），同一家公司 12 小時內不重複打網路。

對外介面
  feed = YahooFeed()
  feed.search("0700")                 -> [ {symbol, name, exchange, market, ...} ]
  payload, info = feed.payload("0700.HK")   -> 可直接丟給 st.parse_payload()
  feed.multiples(["0005.HK", ...])     -> 可比公司倍數（供診斷頁籤用）
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

Q2 = "https://query2.finance.yahoo.com"
Q1 = "https://query1.finance.yahoo.com"

CURRENCY_LABEL = {"HKD": "港元", "MOP": "澳門元", "CNY": "人民幣",
                  "USD": "美元", "EUR": "歐元", "SGD": "新加坡元",
                  "TWD": "新台幣", "GBP": "英鎊", "JPY": "日圓"}

MARKET_OF_SUFFIX = {
    "HK": ("HK", "港股"), "SS": ("A", "A 股（滬）"), "SZ": ("A", "A 股（深）"),
    "BJ": ("A", "A 股（北）"), "TW": ("TW", "台灣"), "T": ("JP", "日本"),
    "L": ("UK", "英國"), "SI": ("SG", "新加坡"), "AX": ("AU", "澳洲"),
}

#: 常用快速鍵（UI 下拉用）
PRESETS: Tuple[Tuple[str, str], ...] = (
    ("0700.HK  騰訊控股", "0700.HK"),
    ("0005.HK  滙豐控股", "0005.HK"),
    ("0939.HK  建設銀行", "0939.HK"),
    ("1299.HK  友邦保險", "1299.HK"),
    ("1928.HK  金沙中國（澳門）", "1928.HK"),
    ("0027.HK  銀河娛樂（澳門）", "0027.HK"),
    ("2282.HK  美高梅中國（澳門）", "2282.HK"),
    ("600519.SS 貴州茅台", "600519.SS"),
    ("000001.SZ 平安銀行", "000001.SZ"),
)


class FetchError(RuntimeError):
    """抓取失敗（帶機讀代碼，方便 UI 分級提示）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------ 工具
def _num(v: Any) -> Optional[float]:
    """Yahoo 的數字可能是 {"raw": 1.2} 或直接 1.2。"""
    if v is None:
        return None
    if isinstance(v, dict):
        for k in ("raw", "value"):
            if k in v and v[k] is not None:
                try:
                    return float(v[k])
                except (TypeError, ValueError):
                    return None
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None          # 排除 NaN


def _year_of(label: Any) -> Optional[int]:
    m = re.search(r"(19|20)\d{2}", str(label or ""))
    return int(m.group(0)) if m else None


def normalize_symbol(raw: str) -> str:
    """把使用者輸入整理成 Yahoo 代號。

    '700' / '0700' / '0700.hk' → '0700.HK'；'600519' → '600519.SS'。
    """
    s = (raw or "").strip().upper().replace(" ", "")
    if not s:
        return ""
    if "." in s:
        return s
    if re.fullmatch(r"\d{1,5}", s):
        return s.zfill(4) + ".HK"
    if re.fullmatch(r"\d{6}", s):
        return s + (".SS" if s[0] in "56" else ".SZ")
    return s


def market_of(symbol: str) -> Tuple[str, str]:
    suf = symbol.split(".")[-1].upper() if "." in symbol else ""
    return MARKET_OF_SUFFIX.get(suf, ("US", "其他"))


# ------------------------------------------------------------------ 抓取器
class YahooFeed:
    """Yahoo Finance 抓取器（標準庫實作，含 cookie+crumb 與磁碟快取）。"""

    def __init__(self, cache_dir: Optional[str] = None, ttl_hours: float = 12.0,
                 timeout: float = 12.0):
        base = os.path.dirname(os.path.abspath(__file__))
        self.cache_dir = cache_dir or os.path.join(base, "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)
        self.timeout = timeout
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj))
        self._crumb: Optional[str] = None
        self.last_source = ""            # network / cache / stale

    # ---------------------------------------------------------- HTTP
    def _get(self, url: str, retries: int = 2) -> str:
        last: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": UA,
                    "Accept": "application/json,text/plain,*/*",
                    "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
                })
                with self._opener.open(req, timeout=self.timeout) as r:
                    return r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (401, 403) and attempt == 0:
                    self._crumb = None      # crumb 過期 → 重新取
                    continue
                if e.code == 404:
                    break
            except Exception as e:          # 網路／逾時／DNS
                last = e
            time.sleep(0.6 * (attempt + 1))
        raise FetchError("network", f"連線失敗：{last}")

    def _ensure_crumb(self) -> str:
        if self._crumb:
            return self._crumb
        try:
            self._get("https://fc.yahoo.com", retries=0)      # 取 cookie
        except FetchError:
            pass
        txt = self._get(f"{Q1}/v1/test/getcrumb", retries=1).strip()
        if not txt or "<" in txt or len(txt) > 32:
            raise FetchError("crumb", "無法取得 Yahoo 授權碼（網路受限或端點變更）")
        self._crumb = txt
        return txt

    # ---------------------------------------------------------- 搜尋
    def search(self, query: str, limit: int = 12) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        if not q:
            return []
        url = (f"{Q2}/v1/finance/search?q={urllib.parse.quote(q)}"
               f"&quotesCount={limit}&newsCount=0&enableFuzzyQuery=true")
        data = json.loads(self._get(url))
        out: List[Dict[str, Any]] = []
        for it in data.get("quotes") or []:
            sym = str(it.get("symbol") or "")
            if not sym or it.get("quoteType") not in ("EQUITY", "ETF"):
                continue
            mk, mk_label = market_of(sym)
            out.append({
                "symbol": sym,
                "name": it.get("longname") or it.get("shortname") or sym,
                "exchange": it.get("exchDisp") or it.get("exchange") or "",
                "market": mk,
                "market_label": mk_label,
                "sector": it.get("sectorDisp") or it.get("sector") or "",
                "industry": it.get("industryDisp") or it.get("industry") or "",
            })

        # 排序：完全相符 → 港股／A 股 → 有名稱的 → 其他
        want = normalize_symbol(q).upper()
        q_up = q.upper()

        def rank(r: Dict[str, Any]) -> Tuple[int, int, int, int]:
            sym = r["symbol"].upper()
            exact = 0 if sym in (want, q_up) else 1
            near = 0 if sym.startswith(q_up.replace(".HK", "").replace(".SS", "")) else 1
            home = 0 if r["market"] in ("HK", "A", "MO") else 1
            named = 0 if r["name"].upper() != sym else 1
            return (exact, home, named, near)

        out.sort(key=rank)
        return out[:limit]

    # ---------------------------------------------------------- 報表
    TS_TYPES = (
        # 損益表
        "annualTotalRevenue", "annualOperatingRevenue", "annualCostOfRevenue",
        "annualGrossProfit", "annualSellingGeneralAdministration",
        "annualOperatingIncome", "annualEBIT", "annualEBITDA",
        "annualDepreciationAndAmortization", "annualDepreciationAmortizationDepletion",
        "annualInterestExpense", "annualPretaxIncome", "annualTaxProvision",
        "annualNetIncome", "annualBasicEPS",
        # 資產負債表
        "annualAccountsReceivable", "annualInventory", "annualAccountsPayable",
        "annualCurrentAssets", "annualCurrentLiabilities",
        "annualOtherCurrentAssets", "annualOtherCurrentLiabilities",
        "annualCashAndCashEquivalents", "annualNetPPE", "annualTotalAssets",
        "annualTotalLiabilitiesNetMinorityInterest", "annualStockholdersEquity",
        "annualTotalDebt", "annualLongTermDebt", "annualCurrentDebt",
        "annualMinorityInterest", "annualRetainedEarnings",
        # 現金流量表
        "annualCapitalExpenditure", "annualOperatingCashFlow",
        "annualCashDividendsPaid",
    )

    def _timeseries(self, symbol: str) -> Dict[str, Dict[int, float]]:
        p1 = int((datetime.now() - timedelta(days=365 * 12)).timestamp())
        p2 = int(time.time()) + 86400
        url = (f"{Q2}/ws/fundamentals-timeseries/v1/finance/timeseries/"
               f"{urllib.parse.quote(symbol)}?symbol={urllib.parse.quote(symbol)}"
               f"&type={','.join(self.TS_TYPES)}&period1={p1}&period2={p2}")
        raw = json.loads(self._get(url))
        res = (raw.get("timeseries") or {}).get("result") or []
        out: Dict[str, Dict[int, float]] = {}
        for block in res:
            for typ in block:
                if typ in ("meta", "timestamp"):
                    continue
                series = block[typ]
                if not isinstance(series, list):
                    continue
                bucket: Dict[int, float] = {}
                for cell in series:
                    if not isinstance(cell, dict):
                        continue
                    y = _year_of(cell.get("asOfDate"))
                    v = _num(cell.get("reportedValue"))
                    if y and v is not None:
                        bucket[y] = v
                if bucket:
                    out[typ] = bucket
        if not out:
            raise FetchError("empty", "Yahoo 沒有回傳年度財報（可能是 ETF、停牌或非上市公司）")
        return out

    def _quote_summary(self, symbol: str) -> Dict[str, Any]:
        crumb = self._ensure_crumb()
        modules = ("price,summaryDetail,defaultKeyStatistics,financialData,"
                   "assetProfile,summaryProfile")
        url = (f"{Q1}/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}"
               f"?modules={modules}&crumb={urllib.parse.quote(crumb)}")
        raw = json.loads(self._get(url))
        res = (raw.get("quoteSummary") or {}).get("result")
        if not res:
            err = (raw.get("quoteSummary") or {}).get("error") or {}
            raise FetchError("quote", err.get("description") or "查無此代號的行情資料")
        return res[0] or {}

    def _fx(self, src: str, dst: str) -> Optional[float]:
        """1 單位 src 兌 dst 的匯率（抓不到回 None）。"""
        if not src or not dst or src == dst:
            return 1.0
        pair = f"{src}{dst}=X"
        cache = os.path.join(self.cache_dir, f"fx_{pair}.json")
        try:
            if os.path.exists(cache):
                d = json.load(open(cache, encoding="utf-8"))
                if datetime.fromisoformat(d["at"]) > datetime.now() - timedelta(hours=6):
                    return float(d["rate"])
        except Exception:
            pass
        try:
            raw = json.loads(self._get(f"{Q1}/v8/finance/chart/{pair}?range=5d&interval=1d"))
            rate = _num(((raw.get("chart") or {}).get("result") or [{}])[0]
                        .get("meta", {}).get("regularMarketPrice"))
            if rate:
                json.dump({"rate": rate, "at": datetime.now().isoformat()},
                          open(cache, "w", encoding="utf-8"))
                return rate
        except Exception:
            return None
        return None

    # ---------------------------------------------------------- 組 payload
    #: (Yahoo 類型清單（依優先序）, 標準科目, 報表區, 顯示名稱)
    ITEM_MAP: Tuple[Tuple[Tuple[str, ...], str, str, str], ...] = (
        (("annualTotalRevenue", "annualOperatingRevenue"), "revenue", "income",
         "Revenue 營業收入"),
        (("annualCostOfRevenue",), "cost_of_sales", "income", "Cost of sales 銷貨成本"),
        (("annualGrossProfit",), "gross_profit", "income", "Gross profit 毛利"),
        (("annualSellingGeneralAdministration",), "admin_expenses", "income",
         "Operating expenses 營業費用"),
        (("annualDepreciationAndAmortization", "annualDepreciationAmortizationDepletion"),
         "depreciation_amortisation", "income", "Depreciation and amortisation 折舊及攤銷"),
        (("annualEBIT", "annualOperatingIncome"), "ebit", "income",
         "EBIT 息稅前營業利益"),
        (("annualEBITDA",), "ebitda", "income", "EBITDA"),
        (("annualInterestExpense",), "finance_costs", "income", "Finance costs 財務費用"),
        (("annualPretaxIncome",), "pbt", "income", "Profit before tax 除稅前溢利"),
        (("annualTaxProvision",), "tax_expense", "income", "Income tax expense 所得稅費用"),
        (("annualNetIncome",), "net_profit", "income", "Net profit 年度溢利"),

        (("annualAccountsReceivable",), "receivables", "balance",
         "Trade receivables 應收帳款"),
        (("annualInventory",), "inventory", "balance", "Inventories 存貨"),
        (("annualAccountsPayable",), "payables", "balance", "Trade payables 應付帳款"),
        (("annualOtherCurrentAssets",), "other_current_assets", "balance",
         "Other current assets 其他流動資產"),
        (("annualOtherCurrentLiabilities",), "other_current_liabilities", "balance",
         "Other current liabilities 其他流動負債"),
        (("annualCurrentAssets",), "current_assets", "balance",
         "Total current assets 流動資產總額"),
        (("annualCurrentLiabilities",), "current_liabilities", "balance",
         "Total current liabilities 流動負債總額"),
        (("annualCashAndCashEquivalents",), "cash", "balance",
         "Cash and cash equivalents 現金及現金等價物"),
        (("annualNetPPE",), "ppe", "balance", "Property, plant and equipment 物業廠房及設備"),
        (("annualTotalAssets",), "total_assets", "balance", "Total assets 資產總額"),
        (("annualTotalLiabilitiesNetMinorityInterest",), "total_liabilities", "balance",
         "Total liabilities 負債總額"),
        (("annualStockholdersEquity",), "equity", "balance", "Total equity 權益總額"),
        (("annualTotalDebt", "annualLongTermDebt"), "debt", "balance",
         "Bank borrowings 計息負債"),
        (("annualMinorityInterest",), "minority", "balance", "Non-controlling interests 非控股權益"),
        (("annualRetainedEarnings",), "retained_earnings", "balance",
         "Retained earnings 保留溢利"),

        (("annualCapitalExpenditure",), "capex", "cashflow", "Capital expenditure 資本開支"),
        (("annualOperatingCashFlow",), "operating_cash_flow", "cashflow",
         "Operating cash flow 經營活動現金流"),
        (("annualCashDividendsPaid",), "dividends_paid", "cashflow", "Dividends paid 已付股息"),
    )

    def payload(self, symbol: str, allow_stale: bool = True, force: bool = False
                ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """抓一家公司 → (payload, info)。payload 可直接給 statements.parse_payload。"""
        sym = normalize_symbol(symbol)
        if not sym:
            raise FetchError("symbol", "請輸入股票代號或公司名稱")
        cache = os.path.join(self.cache_dir, f"{sym.replace('.', '_')}.json")
        info: Dict[str, Any] = {"symbol": sym, "warnings": []}

        # 1) 快取（有效期內直接回；force=True 略過）
        if os.path.exists(cache) and not force:
            try:
                cd = json.load(open(cache, encoding="utf-8"))
                at = datetime.fromisoformat(cd["at"])
                if datetime.now() - at < self.ttl:
                    self.last_source = "cache"
                    info.update(cd.get("info") or {})
                    info["source"] = "cache"
                    info["as_of"] = cd["at"]
                    return cd["payload"], info
            except Exception:
                pass

        # 2) 抓網路
        try:
            ts = self._timeseries(sym)
            qs = self._quote_summary(sym)
        except FetchError as e:
            if allow_stale and os.path.exists(cache):
                cd = json.load(open(cache, encoding="utf-8"))
                self.last_source = "stale"
                info.update(cd.get("info") or {})
                info["source"] = "stale"
                info["as_of"] = cd.get("at")
                info["warnings"].append(f"即時抓取失敗（{e.message}），已改用 {cd.get('at', '')[:16]} 的快取資料。")
                return cd["payload"], info
            raise

        payload, meta = self._build(sym, ts, qs, info)
        try:
            json.dump({"at": datetime.now().isoformat(), "payload": payload,
                       "info": {k: v for k, v in info.items() if k != "warnings"}},
                      open(cache, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
        self.last_source = "network"
        info["source"] = "network"
        return payload, info

    def _build(self, sym: str, ts: Dict[str, Dict[int, float]], qs: Dict[str, Any],
               info: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        price_mod = qs.get("price") or {}
        dks = qs.get("defaultKeyStatistics") or {}
        fin = qs.get("financialData") or {}
        prof = qs.get("assetProfile") or qs.get("summaryProfile") or {}

        name = (price_mod.get("longName") or price_mod.get("shortName")
                or prof.get("longBusinessSummary", "")[:0] or sym)
        trade_ccy = (price_mod.get("currency") or "").upper()
        fin_ccy = (fin.get("financialCurrency") or trade_ccy or "").upper()
        market_price = _num(price_mod.get("regularMarketPrice")) or _num(fin.get("currentPrice"))

        # 報表幣別 vs 交易幣別不同（例如港股以人民幣記帳）→ 換算現價
        fx_note = ""
        price = market_price
        if market_price and trade_ccy and fin_ccy and trade_ccy != fin_ccy:
            rate = self._fx(trade_ccy, fin_ccy)
            if rate:
                price = market_price * rate
                fx_note = (f"現價 {market_price:,.2f} {trade_ccy} 已按 1 {trade_ccy}"
                           f" = {rate:.4f} {fin_ccy} 換算")
            else:
                info["warnings"].append(
                    f"交易幣別 {trade_ccy} 與報表幣別 {fin_ccy} 不同，且匯率抓取失敗；"
                    f"現價仍為 {trade_ccy}，請自行換算或手動覆寫。")

        ccy = fin_ccy or "HKD"
        label = CURRENCY_LABEL.get(ccy, ccy)
        unit = f"百萬{label}"
        scale = 1e6

        years: List[int] = sorted({y for s in ts.values() for y in s})
        statements: Dict[str, Dict[str, Dict[str, float]]] = {
            "income": {}, "balance": {}, "cashflow": {}}

        for types, key, section, label_txt in self.ITEM_MAP:
            merged: Dict[int, float] = {}
            for t in types:
                for y, v in (ts.get(t) or {}).items():
                    merged.setdefault(y, v)
            if not merged:
                continue
            statements[section].setdefault(label_txt, {}).update(
                {str(y): round(v / scale, 4) for y, v in sorted(merged.items())})

        # 每股指標（不隨單位縮放，歸在「備忘指標」區避免分流警告）
        metric: Dict[str, Dict[str, float]] = {}
        if "annualBasicEPS" in ts:
            metric["Basic EPS 每股基本盈利"] = {
                str(y): round(v, 4) for y, v in sorted(ts["annualBasicEPS"].items())}

        shares = _num(dks.get("sharesOutstanding")) or _num(dks.get("impliedSharesOutstanding"))
        if shares:
            metric["Shares outstanding 已發行股數"] = {str(years[-1]): round(shares / 1e6, 4)}
            if price:
                metric["Share price 股價"] = {str(years[-1]): round(price, 4)}
        if metric:
            statements["metric"] = metric

        mk, mk_label = market_of(sym)
        jur = {"HK": "HK", "A": "HK", "MO": "MO"}.get(mk, "HK")
        if mk == "A":
            jur = "HK"          # 稅制模組只支援港澳；A 股沿用香港口徑並在備註提示
            info["warnings"].append("A 股企業的企業所得稅為 25%，稅制頁籤請自行調整稅率。")

        sector = prof.get("sector") or ""
        industry = prof.get("industry") or ""
        if sector == "Financial Services" or "Bank" in industry or "Insurance" in industry:
            info["warnings"].append(
                "此公司屬金融業（銀行／保險）：FCFF 口徑不適用，建議改用 DDM 或 P/B 相對估值參考。")

        meta: Dict[str, Any] = {
            "id": sym.replace(".", "_").lower(),
            "name": name,
            "ticker": sym,
            "jurisdiction": jur,
            "currency": ccy,
            "unit": unit,
            "industry": prof.get("industry") or "",
            "sector": prof.get("sector") or "",
            "fiscal_years": years,
            "price": round(price, 4) if price else None,
            "shares": round(shares / 1e6, 4) if shares else None,
            "reporting_standard": "Yahoo Finance 標準化年度科目（港股多為 IFRS）",
            "source": f"Yahoo Finance 自動抓取（{datetime.now():%Y-%m-%d %H:%M}）",
            "market": mk_label,
            "note": "；".join(filter(None, [
                f"共 {len(years)} 個年度（{years[0]}–{years[-1]}）" if years else "",
                fx_note,
                ("金融業：FCFF 口徑不適用，請以 DDM／P/B 交叉參考"
                 if (sector == "Financial Services" or "Bank" in industry)
                 else ""),
                "科目為標準化後的年度數值，個別明細（如其他收益）可能與年報原文有差異，建議抽查覆寫。",
            ])),
        }
        payload = {"meta": meta, "statements": statements}
        info.update({"years": years, "currency": ccy, "unit": unit,
                     "name": name, "price": meta["price"], "shares": meta["shares"],
                     "market": mk_label,
                     "as_of": datetime.now().isoformat()})
        if len(years) < 3:
            info["warnings"].append(f"只抓到 {len(years)} 個年度，歷史樣本偏少。")
        return payload, meta

    # ---------------------------------------------------------- 可比公司
    def similar(self, symbol: str, limit: int = 8) -> List[str]:
        """Yahoo 的「相似公司」清單（同業推薦）。"""
        sym = normalize_symbol(symbol)
        if not sym:
            return []
        try:
            raw = json.loads(self._get(f"{Q2}/v6/finance/recommendationsbysymbol/"
                                       f"{urllib.parse.quote(sym)}"))
            res = (raw.get("finance") or {}).get("result") or []
            recs = (res[0] or {}).get("recommendedSymbols") or [] if res else []
            return [str(r["symbol"]) for r in recs[:limit] if r.get("symbol")]
        except Exception:
            return []

    def peer_rows(self, symbol: str, markets: Optional[List[str]] = None,
                  extra: Optional[List[str]] = None, limit: int = 8) -> List[Dict[str, Any]]:
        """自動組出可比樣本池：相似公司 ＋ 使用者自訂代號 → 抓倍數。"""
        sym = normalize_symbol(symbol)
        syms: List[str] = []
        for s in list(extra or []) + self.similar(sym, limit):
            s = normalize_symbol(s)
            if s and s != sym and s not in syms:
                syms.append(s)
        rows = self.multiples(syms)
        if markets:
            # 澳門企業多在港股上市，因此 MO 與 HK 共用港股樣本
            allow = set()
            if "HK" in markets or "MO" in markets:
                allow.add("HK")
            if "A" in markets:
                allow.add("A")
            if allow:
                rows = [r for r in rows if r.get("market") in allow]
        for r in rows:
            r["price"] = None          # 由本模組以倍數反推，不需要市價
        return rows

    def multiples(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """抓一組同業的估值倍數（給診斷頁籤的可比估值用）。"""
        out: List[Dict[str, Any]] = []
        for raw in symbols:
            sym = normalize_symbol(raw)
            if not sym:
                continue
            try:
                qs = self._quote_summary(sym)
            except FetchError:
                continue
            p = qs.get("price") or {}
            d = qs.get("defaultKeyStatistics") or {}
            f = qs.get("financialData") or {}
            s = qs.get("summaryDetail") or {}
            prof = qs.get("assetProfile") or qs.get("summaryProfile") or {}
            pe = _num(s.get("trailingPE")) or _num(d.get("forwardPE"))
            if pe is not None and (pe <= 0 or pe > 400):
                pe = None                    # 虧損或極端值 → 視為失效
            row = {
                "name": p.get("longName") or p.get("shortName") or sym,
                "market": market_of(sym)[0],
                "industry": prof.get("industry") or "",
                "pe": pe,
                "pb": _num(d.get("priceToBook")),
                "ev_ebitda": _num(d.get("enterpriseToEbitda")),
                "ev_sales": _num(d.get("enterpriseToRevenue")),
                "ebit_margin": _num(f.get("operatingMargins")),
                "growth": _num(f.get("revenueGrowth")),
                "symbol": sym,
                "source": "Yahoo Finance",
            }
            for k in ("pb", "ev_ebitda", "ev_sales"):
                v = row[k]
                if v is not None and (v <= 0 or v > 200):
                    row[k] = None
            out.append(row)
        return out
