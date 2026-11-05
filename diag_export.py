"""
diag_export.py — 企業診斷匯出（JSON ＋ HTML 報告）
================================================================
  * save_json   全部參數＋計算結果＋風險標籤＋溯源（一次到位）
  * save_html   全套四合一報告；可附「DCF 驗證附錄」（傳入 dcf_result 與 dcf_html_path）
  * 單一模組報告：save_html(module="dupont"|"credit"|"ddm"|"comps")
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

MODULE_TITLES = {
    "dupont": "① 企業基本面｜杜邦分析（五階）",
    "credit": "② 信用風險｜Altman Z'' 與財務比率",
    "ddm": "③ 絕對估值｜DDM 與 FCF Yield",
    "comps": "④ 相對估值｜可比公司",
}

ZONE_SAFE_OLD, ZONE_GREY_OLD = 2.6, 1.1     # Altman 原始門檻（行業調整前的對照值）
ZONE_LABELS = {"safe": "安全區", "grey": "灰色區", "distress": "危險區"}

CALIBRATION_NOTE = (
    "本報告為「參考校準工具」輸出：所有估值與比率僅供交叉比對，"
    "不會、亦不得自動覆寫 DCF／NPV 的任何使用者自訂假設。"
    "唯有在使用者從未手動修改過對應欄位時，才會把建議值推送為 DCF 的參考預設。"
)


def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _n(v: Optional[float], nd: int = 2) -> str:
    return "—" if v is None else f"{v:,.{nd}f}"


def _p(v: Optional[float], nd: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{nd}f}%"


def _tags_html(tags) -> str:
    if not tags:
        return '<div class="tag tok">未觸發風險標籤</div>'
    out = []
    for t in tags:
        cls = {"high": "thigh", "med": "tmed", "ok": "tok", "info": "tinfo"}.get(t.get("level"), "tinfo")
        out.append(f'<div class="tag {cls}"><b>{_e(t.get("title"))}</b>'
                   f'<span>{_e(t.get("detail", ""))}</span></div>')
    return "".join(out)


# ---------------------------------------------------------------- JSON
def save_json(res, path: Optional[str] = None) -> str:
    path = path or os.path.join(OUTPUT_DIR, f"企業診斷_{_safe(res.company)}_{_stamp()}.json")
    payload = res.to_dict()
    payload["generated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["calibration_note"] = CALIBRATION_NOTE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "company"


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


NPV_BUNDLE_NOTE = (
    "本參數包由企業診斷模組匯出，供 NPV／其他估值工具匯入作為「預設值」；"
    "匯入方不得自動覆寫使用者已自訂的假設。歷史營收成長率與 EBIT 利潤率來自"
    "共用財報解析引擎（dcfcore.statements），稅制為澳門所得補充稅（dcfcore.tax），"
    "折現率來自 macro_analyzer 宏觀模型。"
)


def save_npv_bundle(res, dcf_state: Optional[Dict[str, Any]] = None,
                    path: Optional[str] = None) -> str:
    """匯出給 NPV／外部工具的預設參數包（JSON）。"""
    d = res.dupont
    rev = d.get("rev") or {}
    last = res.rows[-1] if res.rows else {}
    sug = res.suggestions or {}
    scen = {s["key"]: {"growth": s["growth"], "ebit_margin": s.get("ebit_margin"),
                       "ke": s["ke"]} for s in (res.scenarios or [])}
    stt = dcf_state or {}
    bundle = {
        "schema": "npv-params/1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": {"name": res.company, "ticker": res.ticker,
                    "currency": res.currency, "unit": res.unit,
                    "industry": res.industry},
        "history": {
            "revenue_by_year": {str(r["year"]): r["revenue"]
                                for r in (rev.get("rows") or []) if r.get("revenue")},
            "revenue_cagr": rev.get("cagr"),
            "revenue_trend_growth": rev.get("trend_growth"),
            "ebit_margin_by_year": {str(r["year"]): r["margin"]
                                    for r in (d.get("ebit") or {}).get("rows") or []},
            "ebit_margin_avg3": d.get("hist_ebit_margin"),
            "roe_avg3": (d.get("avg") or {}).get("roe"),
        },
        "defaults": {
            "revenue_growth": rev.get("cagr"),
            "ebit_margin": d.get("hist_ebit_margin"),
            "ke": sug.get("ke"), "ke_source": sug.get("ke_source"),
            "payout": sug.get("payout"),
        },
        "scenarios": scen,
        "tax": res.tax,
        "dcf_assumptions": {k: stt.get(k) for k in
                            ("years", "wacc", "g", "growth", "margin", "mo_regime")},
        "note": NPV_BUNDLE_NOTE,
        "provenance": res.book.snapshot() if res.book else [],
    }
    path = path or os.path.join(OUTPUT_DIR, f"NPV參數包_{_safe(res.company)}_{_stamp()}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, indent=2, default=str)
    return path


# ---------------------------------------------------------------- HTML
def _css() -> str:
    return """body{font-family:"Microsoft YaHei UI","Microsoft JhengHei",sans-serif;margin:0;
 color:#343A40;background:#F4F6F7}
.page{max-width:960px;margin:24px auto;background:#fff;padding:34px 44px;
 box-shadow:0 2px 10px rgba(0,0,0,.08)}
h1{color:#1F5C5C;font-size:21px;margin:0 0 2px}
h2{color:#1F5C5C;font-size:14.5px;border-bottom:2px solid #1F5C5C;padding-bottom:4px;margin:26px 0 10px}
.meta{color:#868E96;font-size:12px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}
th{background:#1F5C5C;color:#fff;padding:6px 8px;text-align:left;font-weight:600}
td{padding:5px 8px;border-bottom:1px solid #E9ECEF}tr:nth-child(even){background:#F8F9FA}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums}
.hint{color:#868E96;font-size:11px}
.tag{border-radius:5px;padding:8px 12px;margin:6px 0;font-size:12.5px}
.tag b{display:block}.tag span{opacity:.85}
.thigh{background:#FFF0F0;border-left:4px solid #E03131}
.tmed{background:#FFF8E6;border-left:4px solid #F08C00}
.tok{background:#E6FCF5;border-left:4px solid #0CA678}
.tinfo{background:#EDF2F7;border-left:4px solid #3B5BDB}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:6px}
.kpi{border:1px solid #DEE2E6;border-radius:6px;padding:10px 12px;background:#F8F9FA}
.kpi .k{font-size:11px;opacity:.85}.kpi .v{font-size:17px;font-weight:700;margin:2px 0}
.kpi .s{font-size:10.5px;opacity:.8}
.callout{background:#EDF2F7;border-left:4px solid #3B5BDB;padding:10px 14px;font-size:12px;margin-top:8px}
.explain{font-size:12.5px;line-height:1.75;margin:6px 0}
@media print{body{background:#fff}.page{box-shadow:none;margin:0}}"""


def _dupont_html(res) -> str:
    d = res.dupont
    if not d.get("available"):
        return f"<p class='hint'>{_e(d.get('note') or '資料不足')}</p>"
    avg = d["avg"]
    rev = d.get("rev") or {}
    rows5 = "".join(
        f"<tr><td>{r['year']}</td>"
        f"<td class='r'>{_p(r['ebit_margin'])}</td>"
        f"<td class='r'>{_n(r['interest_burden'])}{'†' if not r.get('interest_clean', True) else ''}</td>"
        f"<td class='r'>{_n(r['tax_burden'])}</td>"
        f"<td class='r'>{_p(r['net_margin'])}</td>"
        f"<td class='r'>{_n(r['asset_turnover'])}</td>"
        f"<td class='r'>{_n(r['equity_multiplier'])}</td>"
        f"<td class='r'><b>{_p(r['roe'])}</b></td></tr>"
        for r in d["rows"])
    revrows = "".join(
        f"<tr><td>{r['year']}</td><td class='r'>{_n(r['revenue'], 0)}</td>"
        f"<td class='r'>{_p(r['yoy'], 1) if r['yoy'] is not None else '—'}</td>"
        f"<td class='r'>{_n(r['index'], 1) if r['index'] is not None else '—'}</td></tr>"
        for r in rev.get("rows") or [])
    ebitrows = "".join(
        f"<tr><td>{r['year']}</td><td class='r'>{_n(r['ebit'], 0)}</td>"
        f"<td class='r'>{_p(r['margin'])}</td>"
        f"<td class='r'>{_p(r['industry_margin'])}</td>"
        f"<td class='r'>{_p(r['gap'], 1) if r['gap'] is not None else '—'}</td></tr>"
        for r in (d.get("ebit") or {}).get("rows") or [])
    scen = "".join(
        f"<tr><td>{s['label']}</td>"
        f"<td class='r'>{_p(s.get('ebit_margin'))}</td>"
        f"<td class='r'>{_n(s.get('asset_turnover'))}</td>"
        f"<td class='r'>{_n(s.get('equity_multiplier'))}</td>"
        f"<td class='r'>{_p(s['roe'])}</td></tr>"
        for s in d.get("scenarios") or [])
    footnote = ("† 該年度稅前溢利高於 EBIT（報表 EBIT 不含非營業項目），「利息負擔」>1 應理解為"
                "EBIT→稅前轉換係數。" if any(not r.get("interest_clean", True) for r in d["rows"]) else "")
    return f"""
<h2>{MODULE_TITLES['dupont']}（五階）</h2>
<div class="kpis">
<div class="kpi"><div class="k">營收 CAGR</div><div class="v">{_p(rev.get('cagr'))}</div>
<div class="s">趨勢增長 {_p(rev.get('trend_growth'))}</div></div>
<div class="kpi"><div class="k">EBIT 利潤率（3年平均）</div><div class="v">{_p(d.get('hist_ebit_margin'))}</div>
<div class="s">最新年度 {_p((d.get('ebit') or {}).get('latest'))}</div></div>
<div class="kpi"><div class="k">ROE（3年平均）</div><div class="v">{_p(avg.get('roe'))}</div>
<div class="s">淨利率 {_p(avg.get('net_margin'))}</div></div>
<div class="kpi"><div class="k">利息負擔（平均）</div><div class="v">{_n(avg.get('interest_burden'))}</div>
<div class="s">稅負擔 {_n(avg.get('tax_burden'))}</div></div>
</div>
<h3 style="color:#1F5C5C;font-size:13px">歷年營收與成長率</h3>
<table><thead><tr><th>年度</th><th class='r'>營收（{_e(res.unit)}）</th><th class='r'>成長率</th>
<th class='r'>指數</th></tr></thead><tbody>{revrows}</tbody></table>
<p class="hint">對數趨勢增長 {_p(rev.get('trend_growth'))}（R² {rev.get('trend_r2')}）｜
最近三年平均成長 {_p(rev.get('avg_yoy'))}｜指數以首個有營收年度 = 100。</p>
<h3 style="color:#1F5C5C;font-size:13px">EBIT 利潤率（獨立輸出）</h3>
<table><thead><tr><th>年度</th><th class='r'>EBIT</th><th class='r'>EBIT 利潤率</th>
<th class='r'>行業中位數</th><th class='r'>差距</th></tr></thead><tbody>{ebitrows}</tbody></table>
<h3 style="color:#1F5C5C;font-size:13px">五階杜邦逐年拆解</h3>
<table><thead><tr><th>年度</th><th class='r'>① EBIT 利潤率</th><th class='r'>② 利息負擔</th>
<th class='r'>③ 稅負擔</th><th class='r'>＝淨利率</th><th class='r'>④ 週轉</th>
<th class='r'>⑤ 乘數</th><th class='r'>ROE</th></tr></thead><tbody>{rows5}</tbody></table>
<p class="hint">{_e(footnote)}</p>
<div class="explain"><b>ROE 驅動來源：{_e(d['driver']['title'])}</b><br>{_e(d['driver']['text'])}</div>
<h3 style="color:#1F5C5C;font-size:13px">情景模擬（營收成長／EBIT 利潤率／Ke 同一組偏移）</h3>
<table><thead><tr><th>情景</th><th class='r'>EBIT 利潤率</th><th class='r'>週轉</th>
<th class='r'>乘數</th><th class='r'>ROE</th></tr></thead><tbody>{scen}</tbody></table>
{_tags_html(d.get('tags'))}"""


def _credit_html(res) -> str:
    c = res.credit
    if not c.get("available"):
        return f"<p class='hint'>{_e(c.get('note') or '資料不足')}</p>"
    rule = c.get("industry_rule")
    zl = ZONE_LABELS
    rows = "".join(
        f"<tr><td>{r['year']}</td><td class='r'>{_n(r['z'])}</td>"
        f"<td>{_e(zl.get(r['zone'], '—'))}</td>"
        f"<td>{_e(zl.get(r.get('zone_adj', ''), '—'))}</td>"
        f"<td class='r'>{_n(r['current_ratio'])}</td><td class='r'>{_n(r['quick_ratio'])}</td>"
        f"<td class='r'>{_p(r['debt_ratio'])}</td><td class='r'>{_n(r['interest_coverage'])}</td></tr>"
        for r in c["rows"])
    scen = "".join(
        f"<tr><td>{s['label']}</td><td class='r'>{_n(s['z'])}</td>"
        f"<td>{_e(zl.get(s['zone'], '—'))}</td>"
        f"<td>{_e(zl.get(s.get('zone_adj', ''), '—'))}</td></tr>"
        for s in c.get("scenarios") or [])
    ind = ""
    if rule:
        ind = (f"<div class='callout' style='border-color:#F08C00'>"
               f"<b>行業備註：{rule['label']}（高槓桿常態）</b><br>"
               f"分級門檻已行業調整為 安全 &gt;{_n(rule['z_safe'])}／灰色 ≥{_n(rule['z_grey'])}（原始 {_n(ZONE_SAFE_OLD)}／{_n(ZONE_GREY_OLD)}）。"
               f"{rule['note']}<br><b>重點追蹤：</b>{rule['watch']}</div>")
    return f"""
<h2>{MODULE_TITLES['credit']}</h2>
{ind}
<table><thead><tr><th>年度</th><th class='r'>Z''</th><th>原始分級</th><th>行業調整分級</th>
<th class='r'>流動比率</th><th class='r'>速動比率</th><th class='r'>資產負債率</th>
<th class='r'>利息覆蓋</th></tr></thead><tbody>{rows}</tbody></table>
<h3 style="color:#1F5C5C;font-size:13px">情景模擬（EBIT 利潤率偏移 → X3 → Z''）</h3>
<table><thead><tr><th>情景</th><th class='r'>Z''</th><th>原始分級</th><th>行業調整分級</th></tr></thead>
<tbody>{scen}</tbody></table>
{_tags_html(c.get('tags'))}"""


def _ddm_html(res) -> str:
    d = res.ddm
    if d.get("disabled"):
        return (f"<h2>{MODULE_TITLES['ddm']}</h2>"
                f"<div class='callout'>{_e(d.get('reason'))}</div>{_tags_html(d.get('tags'))}")
    scen = "".join(
        f"<tr><td>{s['label']}</td><td class='r'>{_p(s['ke'])}</td>"
        f"<td class='r'>{_p(s['g'])}</td>"
        f"<td class='r'>{_n(s['price']) if s['valid'] else 'n/a'}</td></tr>"
        for s in d.get("scenarios") or [])
    ke_src = {"macro": "宏觀 CAPM（macro_analyzer）", "manual": "手動輸入（已鎖定）",
              "fallback": "宏觀不可用 → 10% 保守替代"}.get(d.get("ke_source"), "—")
    g_src = {"manual": "手動輸入", "derived": "自動：min(可持續增長、名義 GDP、歷史營收成長)"}.get(
        d.get("g_source"), "—")
    return f"""
<h2>{MODULE_TITLES['ddm']}</h2>
<div class="kpis">
<div class="kpi"><div class="k">DDM 內在股價</div><div class="v">{res.currency} {_n(d.get('price'))}</div>
<div class="s">Ke {_p(d.get('ke'))}｜g {_p(d.get('g'))}</div></div>
<div class="kpi"><div class="k">股息收益率</div><div class="v">{_p(d.get('div_yield'))}</div>
<div class="s">DPS {_n(d.get('dps'))}｜派息率 {_p(d.get('payout'))}</div></div>
<div class="kpi"><div class="k">FCF 收益率</div><div class="v">{_p(d.get('fcf_yield'))}</div>
<div class="s">FCF = 經營現金流 − 資本開支</div></div>
<div class="kpi"><div class="k">無風險利率</div><div class="v">{_p(d.get('risk_free'))}</div>
<div class="s">宏觀模型（10 年期公債）</div></div>
</div>
<p class="hint">Ke 來源：{_e(ke_src)}｜g 來源：{_e(g_src)}。
DDM 適合穩定派息企業；若公司處於高增長低派息階段，DDM 只能給出下限參考，請以 DCF 為主。
可持續增長率 =（1−派息率）× ROE。</p>
<table><thead><tr><th>情景</th><th class='r'>Ke</th><th class='r'>g</th><th class='r'>內在價</th></tr></thead>
<tbody>{scen}</tbody></table>
{_tags_html(d.get('tags'))}"""


def _comps_html(res) -> str:
    c = res.comps
    if not c.get("available"):
        return f"<p class='hint'>{_e(c.get('note') or '資料不足，相對估值不可用')}</p>"
    rows = "".join(
        f"<tr><td>{_e(r['label'])}</td>"
        f"<td class='r'>{_n(r['target']) if r['target'] else '—'}</td>"
        f"<td class='r'>{_n(r['median'])}</td><td class='r'>{_n(r['mean'])}</td>"
        f"<td class='r'>{_n(r['q1'])}–{_n(r['q3'])}</td>"
        f"<td class='r'>{'隱藏' if not r['valid'] else _n(r['implied_price'])}</td>"
        f"<td class='r'>{_p(r['premium'], 1) if r['premium'] is not None else '—'}</td></tr>"
        for r in c["rows"])
    scen = "".join(
        f"<tr><td>{s['label']}</td><td class='r'>{_n(s['price'])}</td>"
        f"<td class='r'>{_p(s.get('upside'), 1)}</td></tr>"
        for s in c.get("scenarios") or [])
    return f"""
<h2>{MODULE_TITLES['comps']}</h2>
<p class="hint">行業：{_e(c.get('industry_label'))}｜可比樣本 {c.get('n_peers')} 家
（內建示範樣本池，可在介面編輯或匯入 CSV）｜行業 EBIT 利潤率中位數 {_p(c.get('industry_margin'))}｜
行業成長預期中位數 {_p(c.get('industry_growth'))}</p>
<table><thead><tr><th>指標</th><th class='r'>目標</th><th class='r'>中位數</th>
<th class='r'>均值</th><th class='r'>Q1–Q3</th><th class='r'>反推股價</th>
<th class='r'>溢價/折價</th></tr></thead><tbody>{rows}</tbody></table>
<p class="hint">綜合判斷：{_e(c.get('verdict_text'))}</p>
<table><thead><tr><th>情景</th><th class='r'>反推股價</th><th class='r'>較市價</th></tr></thead>
<tbody>{scen}</tbody></table>
{_tags_html(c.get('tags'))}"""


def _scen_bridge_html(res) -> str:
    sc = res.scenarios or []
    if not sc:
        return ""
    rows = "".join(
        f"<tr><td>{s['label']}</td>"
        f"<td class='r'>{_p(s['growth'], 1)}</td>"
        f"<td class='r'>{_p(s.get('ebit_margin'))}</td>"
        f"<td class='r'>{_p(s['ke'], 1)}</td>"
        f"<td class='r'>{_p(s.get('roe'))}</td>"
        f"<td class='r'>{_n(s.get('z'))}</td>"
        f"<td class='r'>{_n(s['ddm_price']) if s.get('ddm_price') else 'n/a'}</td>"
        f"<td class='r'>{_n(s['comps_price']) if s.get('comps_price') else '—'}</td>"
        f"<td class='r'>{_n(s['dcf_price']) if s.get('dcf_price') else (_n(s.get('dcf_ev'), 0) + ' EV' if s.get('dcf_ev') else 'n/a')}</td>"
        f"</tr>"
        for s in sc)
    extra = ""
    dsc = res.dcf_scenarios or {}
    if dsc.get("normalised"):
        extra = f"<p class='hint'>DCF 情景：{_e(dsc.get('normalise_note'))}</p>"
    elif dsc.get("reason"):
        extra = f"<p class='hint'>DCF 情景：{_e(dsc['reason'])}</p>"
    return f"""
<h2>三情景橋接（營收成長／EBIT 利潤率／Ke 同一組偏移）</h2>
<table><thead><tr><th>情景</th><th class='r'>營收成長</th><th class='r'>EBIT 利潤率</th>
<th class='r'>Ke</th><th class='r'>ROE</th><th class='r'>Z''</th>
<th class='r'>DDM 價</th><th class='r'>可比價</th><th class='r'>DCF 價</th></tr></thead>
<tbody>{rows}</tbody></table>{extra}"""


def _conflict_html(res) -> str:
    c = res.conflict or {}
    if not c.get("text"):
        return ""
    items = "".join(f"<li>{_e(x)}</li>" for x in c.get("causes") or [])
    advice = f"<p><b>建議：</b>{_e(c.get('advice'))}</p>" if c.get("advice") else ""
    return (f"<div class='callout' style='border-color:{'#E03131' if c.get('conflict') else '#0CA678'}'>"
            f"<b>{_e(c['text'])}</b><ul>{items}</ul>{advice}</div>")


def _summary_html(res) -> str:
    s = res.summary
    kpis = ""
    cross = s.get("cross") or {}
    if cross.get("dcf_price"):
        kpis = f"""
<div class="kpis">
<div class="kpi"><div class="k">DCF 內在價（對照）</div><div class="v">{_n(cross['dcf_price'])}</div>
<div class="s">較可比反推 {_p(cross.get('vs_comps'), 1)}</div></div>
<div class="kpi"><div class="k">可比反推中位數</div><div class="v">{_n(res.comps.get('implied_median'))}</div>
<div class="s">{_e(res.comps.get('industry_label'))}</div></div>
<div class="kpi"><div class="k">DDM 內在價</div><div class="v">{_n(res.ddm.get('price'))}</div>
<div class="s">Ke {_p(res.ddm.get('ke'))}｜g {_p(res.ddm.get('g'))}</div></div>
<div class="kpi"><div class="k">近年平均 ROE</div><div class="v">{_p(res.dupont.get('avg', {}).get('roe'))}</div>
<div class="s">{_e(res.dupont.get('driver', {}).get('title'))}</div></div>
</div>"""
    tax = res.tax or {}
    tax_line = (f"<p class='hint'>🧾 稅制：{_e(tax.get('label'))}｜法定邊際稅率 "
                f"{_p(tax.get('statutory_rate'))}｜報表隱含有效稅率 {_p(tax.get('effective_rate_reported'))}"
                f"（僅對照）｜所有稅額呼叫 dcfcore.tax。</p>")
    return f"""
<h2>⑤ 綜合摘要與 DCF 校準建議</h2>
{kpis}
<div class="explain">{_e(s.get('text'))}</div>
{_conflict_html(res)}
{_scen_bridge_html(res)}
{tax_line}
<div class="callout">{_e(CALIBRATION_NOTE)}</div>"""


def _prov_html(res) -> str:
    items = res.book.snapshot() if res.book else []
    rows = "".join(
        f"<tr><td>{_e(it.get('label'))}</td><td class='r'>{_e(it.get('value'))}</td>"
        f"<td>{_e(it.get('source_label') or it.get('source'))}</td>"
        f"<td>{'🔒' if it.get('locked') else ''}</td></tr>"
        for it in items)
    return f"""
<h2>附錄：參數溯源</h2>
<table><thead><tr><th>參數</th><th>值</th><th>來源</th><th>鎖定</th></tr></thead>
<tbody>{rows}</tbody></table>"""


def result_to_html(res, modules: Optional[List[str]] = None,
                   dcf_html_path: Optional[str] = None) -> str:
    mods = modules or ["dupont", "credit", "ddm", "comps"]
    builders = {"dupont": _dupont_html, "credit": _credit_html,
                "ddm": _ddm_html, "comps": _comps_html}
    body = "".join(builders[m](res) for m in mods if m in builders)
    appendix = ""
    if dcf_html_path and os.path.exists(dcf_html_path):
        appendix = ("<h2>附錄 B：DCF 估值報告（驗證附錄）</h2>"
                    "<p class='hint'>完整 DCF 報告另存於：" + _e(dcf_html_path) + "</p>")
    jur = {"HK": "香港", "MO": "澳門"}.get(res.jurisdiction, res.jurisdiction)
    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>企業診斷報告 — {_e(res.company)}</title><style>{_css()}</style></head><body>
<div class="page">
<h1>企業基本面與估值配套報告 — {_e(res.company)}</h1>
<div class="meta">產生時間 {_e(datetime.now().strftime('%Y-%m-%d %H:%M'))}｜
引擎 {_e(res.engine_version)}｜{jur}｜{_e(res.currency)} {_e(res.unit)}｜
行業 {_e(res.industry)}｜歷史年度 {', '.join(str(y) for y in res.years) or '—'}</div>
{_summary_html(res)}
{body}
{_prov_html(res)}
{appendix}
</div></body></html>"""


def save_html(res, modules: Optional[List[str]] = None,
              path: Optional[str] = None,
              dcf_html_path: Optional[str] = None) -> str:
    tag = "全套" if (modules is None or len(modules) == 4) else "_".join(modules)
    path = path or os.path.join(OUTPUT_DIR, f"企業診斷_{tag}_{_safe(res.company)}_{_stamp()}.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(result_to_html(res, modules, dcf_html_path))
    return path


def save_files(res, modules: Optional[List[str]] = None,
               dcf_html_path: Optional[str] = None) -> Dict[str, str]:
    return {"json": save_json(res),
            "html": save_html(res, modules, dcf_html_path=dcf_html_path)}
