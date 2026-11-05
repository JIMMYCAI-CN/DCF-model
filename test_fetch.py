"""
test_fetch.py — 自動抓取模組回歸測試（離線部分必測，網路部分可跳過）
================================================================
用法：py test_fetch.py [--net]
  不帶 --net 時只跑離線測試；帶 --net 時額外實測 Yahoo 端點（需要連網）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetcher  # noqa: E402

_PASS = 0
_FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}" + (f"　— {detail}" if detail and not ok else ""))
    _PASS += 1 if ok else 0
    _FAIL += 0 if ok else 1


def main() -> int:
    # ---- 離線：代號整理
    check("四位數補 0 → .HK", fetcher.normalize_symbol("700") == "0700.HK")
    check("五碼港股", fetcher.normalize_symbol("0700") == "0700.HK")
    check("A 股滬市", fetcher.normalize_symbol("600519") == "600519.SS")
    check("A 股深市", fetcher.normalize_symbol("000001") == "000001.SZ")
    check("已帶尾綴不動", fetcher.normalize_symbol("1928.hk") == "1928.HK")
    check("空白代號", fetcher.normalize_symbol("  ") == "")

    # ---- 離線：數值與年度解析
    check("_num raw dict", fetcher._num({"raw": 1.5}) == 1.5)
    check("_num None", fetcher._num(None) is None)
    check("_num NaN", fetcher._num(float("nan")) is None)
    check("_num 字串", fetcher._num("3.2") == 3.2)
    check("_year_of ISO 日期", fetcher._year_of("2025-12-31") == 2025)
    check("_year_of 空值", fetcher._year_of(None) is None)

    # ---- 離線：市場判斷
    check("港股市場", fetcher.market_of("0700.HK")[0] == "HK")
    check("A 股市場", fetcher.market_of("600519.SS")[0] == "A")
    check("美股市場", fetcher.market_of("AAPL")[0] == "US")

    # ---- 離線：ITEM_MAP 完整性（每個 canonical key 只出現一次）
    keys = [k for _, k, _, _ in fetcher.YahooFeed.ITEM_MAP]
    check("科目對映無重複", len(keys) == len(set(keys)),
          f"重複：{[k for k in keys if keys.count(k) > 1]}")

    # ---- 離線：TS_TYPES 覆蓋所有對映類型
    need = {t for types, _, _, _ in fetcher.YahooFeed.ITEM_MAP for t in types}
    missing = need - set(fetcher.YahooFeed.TS_TYPES)
    check("TS_TYPES 覆蓋 ITEM_MAP", not missing, f"缺：{missing}")

    if "--net" in sys.argv:
        f = fetcher.YahooFeed()
        try:
            rows = f.search("0700")
            check("搜尋回傳結果", bool(rows) and rows[0]["symbol"] == "0700.HK",
                  str(rows[:1]))
            payload, info = f.payload("0700.HK", force=True)
            check("抓到年度財報", len(info.get("years") or []) >= 3)
            check("報表幣別存在", bool(info.get("currency")))
            from dcfcore import statements as st
            parsed = st.parse_payload(payload)
            check("解析無未對映科目", not parsed.unmatched, str(parsed.unmatched[:5]))
            base = st.extract_base(parsed)
            check("基期營收 > 0", base.values.get("revenue", 0) > 0)
            check("EBIT > 0", base.values.get("ebit", 0) > 0)
        except fetcher.FetchError as e:
            check("網路測試（可跳過）", False, e.message)

    print(f"\n通過 {_PASS}／{_PASS + _FAIL}")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
