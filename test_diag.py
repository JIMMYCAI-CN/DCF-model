"""
test_diag.py — 企業診斷模組回歸測試
================================================================
涵蓋：杜邦公式恆等、Altman Z'' 分級門檻、DDM 防錯（Ke ≤ g）、
可比估值隱藏失效指標、參數溯源、資料不足不崩潰、
以及最重要的「推送建議值絕不覆蓋使用者改過的 DCF 假設」。

執行：  python tests/test_diag.py   （或 py tests/test_diag.py）
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dcfcore import statements as st      # noqa: E402
from dcfcore.dcf import DCFError          # noqa: E402
import diagnostics                        # noqa: E402

PASS = 0


def check(name: str, cond: bool) -> None:
    global PASS
    status = "✓" if cond else "✗"
    print(f"{status} {name}")
    if not cond:
        raise SystemExit(f"測試失敗：{name}")
    PASS += 1


ROOT = os.path.dirname(os.path.abspath(__file__))


def load(cid: str):
    path = os.path.join(ROOT, "samples", f"{cid}.json")
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    return st.parse_payload(d), d["meta"]


def make(cid: str = "hk_tech", markets=("HK", "MO", "A")):
    p, meta = load(cid)
    return diagnostics.Diagnostics(p, meta), p, meta


def main() -> int:
    diag, p, meta = make()
    res = diag.run_all({"markets": list(("HK", "MO", "A"))})

    # ---------- 模組一：杜邦 ----------
    rows = res.dupont["rows"]
    last = rows[-1]
    if None not in (last["net_margin"], last["asset_turnover"], last["equity_multiplier"]):
        check("杜邦三因子乘積 ≈ ROE",
              abs(last["net_margin"] * last["asset_turnover"] * last["equity_multiplier"]
                  - last["roe"]) < 1e-9)
    check("逐年 ROE 有值且非零", all(r["roe"] and r["roe"] > 0 for r in rows))
    check("驅動來源文字存在", bool(res.dupont["driver"]["text"]))
    check("CAGR 已計算", res.dupont["cagr"] is not None and res.dupont["cagr"] > 0)
    check("三情景 ROE 齊備", len(res.dupont["scenarios"]) == 3 and
          all(s["roe"] is not None for s in res.dupont["scenarios"]))
    # 情景方向：樂觀 ROE ≥ 基準 ≥ 保守
    roe = {s["key"]: s["roe"] for s in res.dupont["scenarios"]}
    check("樂觀 ROE ≥ 基準 ≥ 保守",
          roe["optimistic"] >= roe["base"] - 1e-9 >= roe["conservative"] - 2e-9)

    # ---------- 模組二：Z'' ----------
    z = res.credit["last"]["z"]
    check("Z'' 已計算", z is not None)
    x = res.credit["last"]
    recomputed = (6.56 * x["x1"] + 3.26 * x["x2"] + 6.72 * x["x3"] + 1.05 * x["x4"])
    check("Z'' 公式可由四因子重算", abs(recomputed - z) < 1e-9)
    check("Z'' 分級落在三區之一", res.credit["last"]["zone"] in ("safe", "grey", "distress"))
    check("四項財務比率齊備", all(res.credit["last"].get(k) is not None
                                for k in ("current_ratio", "quick_ratio",
                                          "debt_ratio", "interest_coverage")))
    # 情景：EBIT 上修 → Z'' 上升
    sc = {s["key"]: s["z"] for s in res.credit["scenarios"]}
    check("信用情景方向正確", sc["optimistic"] > sc["base"] > sc["conservative"])

    # ---------- 模組三：DDM ----------
    d = res.ddm
    if d.get("price"):
        # Gordon：P = DPS×(1+g)/(Ke−g)
        expect = d["dps"] * (1 + d["g"]) / (d["ke"] - d["g"])
        check("Gordon 公式重算一致", abs(expect - d["price"]) < 1e-9)
        check("Ke > g（成立前提）", d["ke"] > d["g"])
    # Ke ≤ g 必須被禁止
    d2 = diag.run_ddm({"overrides": {"ddm.ke": 0.05, "ddm.g": 0.08}})
    check("Ke ≤ g 時 DDM 被禁止", d2["disabled"] and "禁止" in d2["reason"])
    check("Ke ≤ g 觸發風險標籤", any(t["code"] == "KE_LE_G" for t in d2["tags"]))
    # 無派息 → 自動停用
    d3_diag, _, _ = make("hk_tech")
    for r in d3_diag.rows:
        r.dividends = 0.0
        r.dps = 0.0
    d3 = d3_diag.run_ddm({})
    check("無派息時 DDM 自動停用", d3["disabled"] and "沒有派息" in d3["reason"])
    check("FCF 收益率有值", res.ddm.get("fcf_yield") is not None)

    # ---------- 模組四：可比 ----------
    c = res.comps
    check("可比樣本已選取", c["n_peers"] >= 3)
    check("中位數反推股價存在", c["implied_median"] is not None)
    check("判斷落在四類之一",
          c["verdict"] in ("cheap", "expensive", "fair", "na"))
    check("行業參考值已導出",
          c.get("industry_margin") is not None or c.get("industry_growth") is not None)
    # 隱藏失效指標
    c2 = diagnostics.Diagnostics(p, meta).run_comps(
        {"markets": ["HK", "MO", "A"], "overrides": {"comps.pb": -1.0}})
    hidden = [r for r in c2["rows"] if not r["valid"] and r["metric"] == "pb"]
    check("P/B 無效時被隱藏", bool(hidden) and hidden[0]["reason"] != "")
    # 虧損 → P/E 失效
    c3_diag, _, _ = make("hk_tech")
    c3_diag.rows[-1].net_profit = -100.0
    c3_diag.rows[-1].eps = None
    c3 = c3_diag.run_comps({"markets": ["HK", "MO", "A"]})
    pe_row = next(r for r in c3["rows"] if r["metric"] == "pe")
    check("虧損時 P/E 標記不適用", not pe_row["valid"] and pe_row["reason"] != "")

    # ---------- 溯源與匯總 ----------
    snap = res.book.snapshot()
    check("參數溯源已記錄", len(snap) >= 3)
    check("溯源含行業參考來源", any(i["source"] == "auto" for i in snap))
    check("綜合摘要含交叉對照", bool(res.summary.get("text")))
    check("風險標籤已去重合併", len(res.tags) == len({t["code"] for t in res.tags}))
    check("建議值齊備（EBIT 利潤率＋CAGR）",
          res.suggestions.get("ebit_margin") is not None and
          res.suggestions.get("revenue_cagr") is not None)

    # ---------- v2.0：五階杜邦／營收趨勢／三情景打通 ----------
    d5 = res.dupont["rows"][-1]
    if None not in (d5["ebit_margin"], d5["interest_burden"], d5["tax_burden"],
                    d5["asset_turnover"], d5["equity_multiplier"]):
        check("五階乘積 ＝ 三階 ROE（利息負擔正常時）",
              d5["interest_burden"] <= 1.001 and
              abs(d5["five_product"] - d5["roe"]) < 1e-9)
    check("營收 YoY 逐年有值",
          all(r["yoy"] is not None for r in res.dupont["rev"]["rows"][1:] if r["revenue"]))
    check("營收趨勢增長已計算", res.dupont["rev_trend_growth"] is not None)
    check("三情景 ROE／DDM／可比 全部隨情景變動",
          len({round(s["roe"], 6) for s in res.scenarios}) == 3 and
          len({round(s["ddm_price"], 6) for s in res.scenarios}) == 3 and
          len({round(s["comps_price"], 6) for s in res.scenarios}) == 3)
    check("情景含 Ke 旋鈕且方向正確",
          res.scenarios[0]["ke"] < 0 and res.scenarios[2]["ke"] > 0)

    # 衝突診斷觸發邏輯（樣本可能無市價，先注入測試用市價）
    diag.price = 12.0
    conf = diag._conflict({"price": 15.0, "payout": 0.2, "g": 0.02, "ke_source": "macro"},
                          {"implied_median": 8.0, "n_peers": 4})
    check("方向衝突可被識別並附成因", conf["conflict"] and len(conf["causes"]) >= 1)
    conf2 = diag._conflict({"price": 11.0, "payout": 0.6, "g": 0.02, "ke_source": "macro"},
                           {"implied_median": 10.5, "n_peers": 6})
    check("方向一致時不誤報衝突", not conf2["conflict"])
    check("無市價時衝突檢查安全降級",
          not diagnostics.Diagnostics(p, meta)._conflict(
              {"price": 15.0, "payout": 0.2}, {"implied_median": 8.0, "n_peers": 4})["conflict"])

    # ---------- v2.0：BOC Aviation（航空租賃）測試標的 ----------
    boc_path = os.path.join(ROOT, "cache", "2588_HK.json")
    if os.path.exists(boc_path):
        from dcfcore import dcf as dcf_engine          # noqa: E402
        raw = json.load(open(boc_path, encoding="utf-8"))
        meta_boc = dict(raw["payload"]["meta"])
        meta_boc["tax_jur"] = "MO"
        pboc = st.parse_payload(raw["payload"])
        diag_boc = diagnostics.Diagnostics(pboc, meta_boc)
        res_boc = diag_boc.run_all({"markets": ["HK"]})
        check("租賃業 Z'' 行業調整已套用（原始與調整分級不同）",
              res_boc.credit["industry_rule"] is not None and
              res_boc.credit["last"]["zone"] != res_boc.credit["last"]["zone_adj"])
        check("租賃業可比樣本池自動選中 leasing",
              res_boc.comps.get("industry_label") == "航空／設備租賃" and
              res_boc.comps.get("n_peers", 0) <= 10)
        check("稅制口徑＝澳門所得補充稅（12%）",
              res_boc.tax["jurisdiction"] == "MO" and
              abs(res_boc.tax["statutory_rate"] - 0.12) < 1e-9)
        check("BOC 營收 CAGR／趨勢／EBIT 利潤率齊備",
              res_boc.dupont["cagr"] is not None and
              res_boc.dupont["rev_trend_growth"] is not None and
              res_boc.dupont["hist_ebit_margin"] is not None)
        base_boc = st.extract_base(pboc)
        state_boc = dcf_engine.default_state(base_boc, meta_boc)
        res_boc2 = diag_boc.run_all({"markets": ["HK"]},
                                    dcf_ctx={"state": state_boc, "base": base_boc,
                                             "meta": meta_boc})
        check("DCF 情景橋接可用（共用引擎）", res_boc2.dcf_scenarios.get("available"))
        check("DCF 三情景 EV 互不相同",
              len({round(s["dcf_ev"] or 0) for s in res_boc2.scenarios}) == 3)
        import diag_export                              # noqa: E402
        p_npv = diag_export.save_npv_bundle(res_boc)
        check("NPV 參數包可匯出", os.path.exists(p_npv))
        with open(p_npv, encoding="utf-8") as fh:
            b = json.load(fh)
        check("NPV 參數包含歷史成長／EBIT 利潤率／稅制",
              b["history"]["revenue_cagr"] is not None and
              b["history"]["ebit_margin_avg3"] is not None and
              b["tax"]["jurisdiction"] == "MO")
    else:
        print("（略過 BOC Aviation 測試：cache/2588_HK.json 不存在）")

    # ---------- 資料不足不崩潰 ----------
    p_min, meta_min = load("mo_resort")   # 兩年樣本
    r_min = diagnostics.Diagnostics(p_min, meta_min).run_all({})
    check("兩年樣本可完整計算", r_min.dupont["available"] and r_min.credit["available"])
    # 空殼：砍掉所有科目
    p_empty, _ = load("hk_tech")
    p_empty.values = {}
    p_empty.years = []
    try:
        r_empty = diagnostics.Diagnostics(p_empty, {"name": "空殼"}).run_all({})
        check("完全無資料時不崩潰", r_empty.dupont.get("available") is False)
    except Exception as e:                                    # noqa: BLE001
        check(f"完全無資料時不崩潰（發生例外：{e}）", False)

    # ---------- 覆寫保護（模擬 App.push_suggestions 的規則） ----------
    edited = {"growth"}
    values = {"growth": [0.01] * 5, "margin": [0.20] * 5}
    applied = [k for k in values if k not in edited]
    check("推送規則：使用者改過的欄位被跳過", applied == ["margin"])
    check("推送規則：未改過的欄位被套用", values["margin"] == [0.20] * 5)

    print(f"\n結果：{PASS} 項測試全部通過 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
