"""
exporter.py — DCF 股價估算器匯出（JSON ＋ 可列印 HTML 報告）
================================================================
只匯出企業 DCF（FCFF）結果；JSON 攜帶全部參數溯源標籤，
HTML 報告可直接列印成 PDF。與專案 NPV 完全分離，不存在合併口徑問題。
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

ISOLATION_NOTE = (
    "本報告為「企業整體 DCF（FCFF）」口徑：現金流為企業自由現金流，"
    "反映全公司的營運現金產生能力。與「專案增量 NPV」口徑不同——"
    "企業 DCF 的現金流不得直接拿去覆蓋任何專案型資本預算的增量現金流，兩者互相獨立。"
)


def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _n(v: Optional[float], nd: int = 0) -> str:
    return "—" if v is None else f"{v:,.{nd}f}"


# ---------------------------------------------------------------- helpers
def dcf_engine_version(res) -> str:
    return res.to_dict().get("engine", "DCF-FCFF")


def provenance_items(res) -> List[Dict[str, Any]]:
    """DCFResult.book（ParamBook）→ 溯源清單。"""
    book = getattr(res, "book", None)
    if book is None:
        return []
    if hasattr(book, "to_list"):
        return book.to_list()
    return book.get("items", []) if isinstance(book, dict) else []


def result_to_dict(res) -> Dict[str, Any]:
    d = res.to_dict() if hasattr(res, "to_dict") else res.__dict__.copy()
    return d


def save_json(res, path: str) -> str:
    payload = {
        "schema": "dcf-stock-estimator/1.0",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "engine": dcf_engine_version(res),
        "company": res.inp.company_name,
        "jurisdiction": res.inp.jurisdiction,
        "summary": res.summary,
        "forecast_rows": res.rows,
        "scenarios": res.scenarios,
        "sensitivity": {k: v for k, v in res.sensitivity.items()},
        "risk_tags": res.tags,
        "provenance": provenance_items(res),
        "isolation_note": ISOLATION_NOTE,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


# ---------------------------------------------------------------- HTML
def result_to_html(res) -> str:
    """DCF 估值報告（可列印）。"""
    s = res.summary
    inp = res.inp
    cur, unit = inp.currency, inp.unit

    def kpi(label, value, sub="", cls=""):
        return (f'<div class="kpi {cls}"><div class="k">{_e(label)}</div>'
                f'<div class="v">{_e(value)}</div>'
                + (f'<div class="s">{_e(sub)}</div>' if sub else "") + "</div>")

    up = s.get("upside")
    up_txt = f"{'▲' if up and up > 0 else '▼'} {abs(up) * 100:.1f}%" if up is not None else "—"

    kpis = "".join([
        kpi("預計股價（內在價值）", f"{cur} {s['intrinsic_price']:,.2f}",
            f"現價 {cur} {s['market_price']:,.2f}　價差 {up_txt}", "main"),
        kpi("企業價值 EV", _n(s["ev"]), f"{cur} {unit}"),
        kpi("股權價值", _n(s["equity"]), f"EV − 淨負債 {_n(s['net_debt'])}"),
        kpi("終值佔 EV", f"{s['tv_share'] * 100:.1f}%",
            "WACC " + f"{s['wacc'] * 100:.1f}%｜g {s['g'] * 100:.1f}%"),
    ])

    tags = "".join(
        f'<div class="tag t{t["level"]}"><b>{_e(t["title"])}</b>'
        f'<span>{_e(t.get("detail", ""))}</span></div>'
        for t in res.tags) or '<div class="tag tok">未觸發自動風險標籤</div>'

    rows = "".join(
        f"<tr><td>{r['fiscal_year']}</td>"
        f"<td class='r'>{r['growth'] * 100:.1f}%</td>"
        f"<td class='r'>{r['margin'] * 100:.1f}%</td>"
        f"<td class='r'>{_n(r['revenue'])}</td>"
        f"<td class='r'>{_n(r['ebit'])}</td>"
        f"<td class='r'>{_n(r['fcff'])}</td>"
        f"<td class='r'>{r['discount_factor']:.4f}</td>"
        f"<td class='r'>{_n(r['pv'])}</td>"
        f"<td class='r'>{_n(r['cum_pv'])}</td></tr>"
        for r in res.rows)
    table_fcff = f"""<table><thead><tr>
        <th>年度</th><th>成長率</th><th>EBIT率</th><th>營收</th><th>EBIT</th>
        <th>FCFF</th><th>折現因子</th><th>折現值</th><th>累計</th>
        </tr></thead><tbody>{rows}</tbody></table>
        <p class="hint">單位：{cur} {unit}；FCFF = EBIT × (1−有效稅率) + 折舊攤銷 − 資本開支 − 營運資金增量</p>"""

    sc_rows = "".join(
        f"<tr><td>{_e(sc['label'])}</td>"
        f"<td class='r'>{sc['wacc'] * 100:.2f}%</td>"
        f"<td class='r'>{sc['g'] * 100:.2f}%</td>"
        f"<td class='r'>{sc['growth_shift'] * 100:+.1f}pp</td>"
        f"<td class='r'>{sc['margin_shift'] * 100:+.1f}pp</td>"
        f"<td class='r'>{_n(sc['ev'])}</td>"
        f"<td class='r'><b>{_n(sc['price'], 2) if sc['valid'] else 'n/a'}</b></td></tr>"
        for sc in res.scenarios)
    table_scen = f"""<table><thead><tr><th>情景</th><th>WACC</th><th>g</th>
        <th>成長偏移</th><th>利潤偏移</th><th>EV</th><th>預計股價</th>
        </tr></thead><tbody>{sc_rows}</tbody></table>"""

    pv_items = provenance_items(res)
    prov_rows = "".join(
        f"<tr><td>{_e(it.get('name') or it.get('key'))}</td>"
        f"<td class='r'>{_e(it.get('value'))}</td>"
        f"<td>{_e(it.get('source'))}</td>"
        f"<td>{'🔒 已鎖定' if it.get('locked') else ''}</td></tr>"
        for it in pv_items[:60])
    table_prov = f"""<table><thead><tr><th>參數</th><th>值</th><th>來源</th><th>鎖定</th>
        </tr></thead><tbody>{prov_rows}</tbody></table>"""

    return f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<title>DCF 股價估算報告 — {_e(inp.company_name)}</title>
<style>
body{{font-family:"Microsoft YaHei UI","Microsoft JhengHei",sans-serif;margin:0;
     color:#343A40;background:#F4F6F7}}
.page{{max-width:900px;margin:24px auto;background:#fff;padding:36px 44px;
      box-shadow:0 2px 10px rgba(0,0,0,.08)}}
h1{{color:#1F5C5C;font-size:22px;margin:0 0 2px}}
h2{{color:#1F5C5C;font-size:15px;border-bottom:2px solid #1F5C5C;
   padding-bottom:4px;margin:28px 0 10px}}
.meta{{color:#868E96;font-size:12px;margin-bottom:18px}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}
.kpi{{border:1px solid #DEE2E6;border-radius:6px;padding:10px 12px;background:#F8F9FA}}
.kpi.main{{background:#1F5C5C;color:#fff;border-color:#1F5C5C}}
.kpi .k{{font-size:11px;opacity:.85}} .kpi .v{{font-size:19px;font-weight:700;margin:2px 0}}
.kpi .s{{font-size:10.5px;opacity:.8}}
table{{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}}
th{{background:#1F5C5C;color:#fff;padding:6px 8px;text-align:left;font-weight:600}}
td{{padding:5px 8px;border-bottom:1px solid #E9ECEF}} tr:nth-child(even){{background:#F8F9FA}}
td.r{{text-align:right;font-variant-numeric:tabular-nums}}
.hint{{color:#868E96;font-size:11px}}
.tag{{border-radius:5px;padding:8px 12px;margin:6px 0;font-size:12.5px}}
.tag b{{display:block}} .tag span{{opacity:.85}}
.thigh{{background:#FFF0F0;border-left:4px solid #E03131}}
.tmed{{background:#FFF8E6;border-left:4px solid #F08C00}}
.tok{{background:#E6FCF5;border-left:4px solid #0CA678}}
.isolation{{background:#EDF2F7;border-left:4px solid #3B5BDB;padding:10px 14px;
           font-size:12px;color:#343A40;margin-top:26px}}
@media print{{body{{background:#fff}} .page{{box-shadow:none;margin:0}}}}
</style></head><body><div class="page">
<h1>DCF 股價估算報告（FCFF）— {_e(inp.company_name)}</h1>
<div class="meta">產生時間 {_e(datetime.now().strftime('%Y-%m-%d %H:%M'))}｜
引擎 {_e(dcf_engine_version(res))}｜司法管轄 {('香港' if inp.jurisdiction == 'HK' else '澳門')}｜
基期 {inp.base_year} 年｜歷史樣本 {inp.history_years} 年</div>
<div class="kpis">{kpis}</div>

<h2>自動風險標籤</h2>{tags}

<h2>逐年 FCFF 與折現</h2>{table_fcff}

<h2>三情景分析（輔助）</h2>{table_scen}

<h2>口徑隔離聲明（重要）</h2>
<div class="isolation">{_e(ISOLATION_NOTE)}</div>

<h2>附錄：參數溯源（全部輸入欄位）</h2>
{table_prov}
<p class="hint">🔒＝使用者人工覆寫後鎖定，重新抓取財報不會覆蓋；未鎖定欄位由財報自動帶入或依預設值。</p>
</div></body></html>"""


def save_files(res, out_dir: Optional[str] = None) -> Dict[str, str]:
    """輸出 JSON ＋ HTML；回傳兩個路徑。"""
    out_dir = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in res.inp.company_name)
    jpath = os.path.join(out_dir, f"DCF估值_{safe}_{stamp}.json")
    hpath = os.path.join(out_dir, f"DCF估值_{safe}_{stamp}.html")
    save_json(res, jpath)
    with open(hpath, "w", encoding="utf-8") as f:
        f.write(result_to_html(res))
    return {"json": jpath, "html": hpath}
