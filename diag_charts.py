"""
diag_charts.py — 企業診斷圖表（matplotlib，供 Tkinter 内嵌）
================================================================
  * dupont_trend      杜邦三因子與 ROE 趨勢（雙軸）
  * dupont_scenario   三情景 ROE 對照
  * credit_z          Altman Z'' 趨勢（安全／灰色／危險區帶）
  * credit_ratios     流動／速動／負債率／利息覆蓋 四小圖
  * ddm_price         DDM 內在價 vs 市價 vs 可比反推
  * ddm_yield         股息率／FCF 收益率 vs 無風險利率
  * comps_box         可比倍數箱線圖（P/E、P/B、EV/EBITDA、EV/Sales）

視覺風格與 NPV／DCF 一致（同一組色盤、同樣的軸線處理）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import matplotlib
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator

import mpl_charts as mc

C_OPT, C_BASE, C_CONS = mc.C_OPT, mc.C_BASE, mc.C_CONS
C_INK, C_MUTED, C_GRID, C_TOTAL = mc.C_INK, mc.C_MUTED, mc.C_GRID, mc.C_TOTAL
C_TV, C_BAR = mc.C_TV, mc.C_BAR

ZONE_BANDS = [(0.0, 1.1, "#FFE3E3", "危險區"), (1.1, 2.6, "#FFF4E0", "灰色區"),
              (2.6, 7.0, "#E6FCF5", "安全區")]


def _tight(fig) -> None:
    """安全版 tight_layout：資料極端時會發警告，靜默處理以免洗版。"""
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            fig.tight_layout(pad=0.5)
        except Exception:
            pass


def _axes(ax) -> None:
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(C_MUTED)
    ax.tick_params(colors=C_INK, labelsize=8)
    ax.grid(axis="y", color=C_GRID, lw=0.7, ls="-")
    ax.set_axisbelow(True)


def _years(ax) -> None:
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))


def _pct_fmt(ax, axis="y") -> None:
    f = FuncFormatter(lambda v, _p: f"{v * 100:.0f}%")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(f)


# ---------------------------------------------------------------- 杜邦
def dupont_trend(fig: Figure, res) -> None:
    fig.patch.set_facecolor("white")
    d = res.dupont
    rows = d.get("rows") or []
    years = [r["year"] for r in rows]
    ax = fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [1.35, 1]})

    roe = [r["roe"] for r in rows]
    ax[0].plot(years, roe, "-o", color=C_BASE, lw=2, ms=4, label="ROE")
    ax[0].fill_between(years, 0, [v or 0 for v in roe], color=C_BASE, alpha=0.10)
    for x, v in zip(years, roe):
        if v is not None:
            ax[0].annotate(f"{v * 100:.1f}%", (x, v), textcoords="offset points",
                           xytext=(0, 7), ha="center", fontsize=7.6, color=C_BASE)
    ax[0].axhline(d["avg"].get("roe") or 0, color=C_MUTED, ls=":", lw=1)
    ax[0].set_ylabel("ROE", fontsize=8.6)
    _pct_fmt(ax[0])
    _axes(ax[0])
    ax[0].legend(fontsize=7.6, frameon=False, loc="upper left")
    _years(ax[0])
    ax[0].set_title("杜邦分析：ROE 與三因子趨勢", fontsize=9.6, color=C_TOTAL, pad=6)

    ax2 = ax[1]
    ax2.bar([y - 0.18 for y in years], [r["net_margin"] or 0 for r in rows],
            width=0.34, color=C_BAR, alpha=0.85, label="淨利率")
    ax2.set_ylabel("淨利率", fontsize=8.2)
    _pct_fmt(ax2)
    _axes(ax2)
    ax3 = ax2.twinx()
    ax3.plot(years, [r["asset_turnover"] or 0 for r in rows], "-s", color=C_TV,
             ms=3.4, lw=1.5, label="資產週轉（次）")
    ax3.plot(years, [r["equity_multiplier"] or 0 for r in rows], "-^", color=C_CONS,
             ms=3.4, lw=1.5, label="權益乘數（倍）")
    ax3.set_ylabel("次／倍", fontsize=8.2)
    ax3.spines["top"].set_visible(False)
    ax3.tick_params(labelsize=7.6)
    _years(ax2)
    h1, l1 = ax2.get_legend_handles_labels()
    h2, l2 = ax3.get_legend_handles_labels()
    ax2.legend(h1 + h2, l1 + l2, fontsize=7.2, frameon=False, ncol=3,
               loc="upper left", bbox_to_anchor=(0, 1.22))
    _tight(fig)


def dupont_scenario(fig: Figure, res) -> None:
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    sc = res.dupont.get("scenarios") or []
    if not sc:
        ax.text(0.5, 0.5, "資料不足，無法模擬情景", ha="center", va="center",
                transform=ax.transAxes, color=C_MUTED, fontsize=10)
        ax.set_axis_off()
        return
    colors = {"optimistic": C_OPT, "base": C_BASE, "conservative": C_CONS}
    bars = ax.bar([s["label"] for s in sc], [s["roe"] for s in sc], width=0.46,
                  color=[colors.get(s["key"], C_BAR) for s in sc], alpha=0.9)
    for b, s in zip(bars, sc):
        ax.text(b.get_x() + b.get_width() / 2, s["roe"], f"{s['roe'] * 100:.1f}%",
                ha="center", va="bottom", fontsize=9, fontweight="bold", color=C_INK)
    ax.set_ylim(0, max(s["roe"] for s in sc) * 1.25)
    _pct_fmt(ax)
    _axes(ax)
    ax.set_title("ROE 三情景模擬（淨利率／週轉／乘數偏移）", fontsize=9.6,
                 color=C_TOTAL, pad=6)
    _tight(fig)


# ---------------------------------------------------------------- 信用
def credit_z(fig: Figure, res) -> None:
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    rows = res.credit.get("rows") or []
    years = [r["year"] for r in rows]
    zs = [r["z"] for r in rows]
    rule = res.credit.get("industry_rule") or {}
    safe = float(rule.get("z_safe", 2.6))
    grey = float(rule.get("z_grey", 1.1))
    top = max([safe] + [z for z in zs if z] + [3.2]) * 1.12
    bands = [(0.0, grey, "#FFE3E3", "危險區"), (grey, safe, "#FFF4E0", "灰色區"),
             (safe, top, "#E6FCF5", "安全區")]
    for lo, hi, color, label in bands:
        ax.axhspan(lo, min(hi, top), color=color, zorder=0)
        if hi <= top or lo == 0:
            ax.text(years[0] if years else 0, (lo + min(hi, top)) / 2, label,
                    fontsize=7.4, color="#868E96", va="center")
    ax.plot(years, zs, "-o", color=C_TOTAL, lw=2, ms=4.5)
    for x, z in zip(years, zs):
        if z is not None:
            ax.annotate(f"{z:.2f}", (x, z), textcoords="offset points", xytext=(0, 8),
                        ha="center", fontsize=7.4, color=C_TOTAL)
    ax.axhline(safe, color="#0CA678", lw=1, ls="--",
               label=f"安全線 {safe:.2f}" + ("（行業調整）" if rule else ""))
    ax.axhline(grey, color="#E03131", lw=1, ls="--",
               label=f"危險線 {grey:.2f}" + ("（行業調整）" if rule else ""))
    if rule:
        ax.axhline(2.6, color=C_MUTED, lw=0.9, ls=":")
        ax.axhline(1.1, color=C_MUTED, lw=0.9, ls=":")
        ax.text(years[-1] if years else 0, 2.6, " 原始 2.6", fontsize=6.8,
                color=C_MUTED, va="bottom", ha="right")
        ax.text(years[-1] if years else 0, 1.1, " 原始 1.1", fontsize=6.8,
                color=C_MUTED, va="bottom", ha="right")
    ax.set_ylim(0, top)
    ax.set_ylabel("Z'' 分數", fontsize=8.6)
    _axes(ax)
    _years(ax)
    ax.legend(fontsize=7, frameon=False, loc="lower right")
    title = "Altman Z''-Score 趨勢（非製造業版）"
    if rule:
        title += f"｜行業調整：{rule.get('label')}"
    ax.set_title(title, fontsize=9.4, color=C_TOTAL, pad=6)
    _tight(fig)


def credit_ratios(fig: Figure, res) -> None:
    fig.patch.set_facecolor("white")
    rows = res.credit.get("rows") or []
    years = [r["year"] for r in rows]
    specs = [("current_ratio", "流動比率", 1.0, None),
             ("quick_ratio", "速動比率", 0.7, None),
             ("debt_ratio", "資產負債率", 0.6, True),
             ("interest_coverage", "利息覆蓋率（倍）", 2.0, None)]
    axes = fig.subplots(2, 2, sharex=True)
    fig.suptitle("核心財務比率", fontsize=9.6, color=C_TOTAL, y=0.97)
    for ax, (key, label, ref, pct) in zip(axes.ravel(), specs):
        vals = [r.get(key) for r in rows]
        ax.plot(years, vals, "-o", color=C_BASE, lw=1.7, ms=3.4)
        if ref is not None:
            ax.axhline(ref, color="#E03131", ls=":", lw=1)
        if pct:
            _pct_fmt(ax)
        ax.set_title(label, fontsize=8.4, color=C_INK, pad=3)
        _axes(ax)
        ax.tick_params(labelsize=7.2)
        _years(ax)
    _tight(fig)


# ---------------------------------------------------------------- DDM
def ddm_price(fig: Figure, res) -> None:
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    items = []
    if res.ddm.get("price"):
        items.append(("DDM 內在價", res.ddm["price"], C_OPT))
    if res.comps.get("implied_median"):
        items.append(("可比反推", res.comps["implied_median"], C_TV))
    if res.price:
        items.append(("現價", res.price, C_MUTED))
    if not items:
        ax.text(0.5, 0.5, "缺少價格資料", ha="center", va="center",
                transform=ax.transAxes, color=C_MUTED, fontsize=10)
        ax.set_axis_off()
        return
    bars = ax.bar([i[0] for i in items], [i[1] for i in items], width=0.42,
                  color=[i[2] for i in items], alpha=0.9)
    for b, (_, v, _c) in zip(bars, items):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.2f}", ha="center",
                va="bottom", fontsize=9, fontweight="bold", color=C_INK)
    if res.price:
        for i, (label, v, _c) in enumerate(items):
            if label != "現價":
                gap = v / res.price - 1
                ax.text(i, v * 0.45, f"{gap * 100:+.1f}%", ha="center",
                        fontsize=8.2, color="white")
    ax.set_ylabel(f"股價（{res.currency}）", fontsize=8.6)
    _axes(ax)
    ax.set_ylim(0, max(i[1] for i in items) * 1.22)
    ax.set_title("絕對估值對照（僅供參考）", fontsize=9.6, color=C_TOTAL, pad=6)
    _tight(fig)


def ddm_yield(fig: Figure, res) -> None:
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    d = res.ddm
    items = [("股息率", d.get("div_yield"), C_BASE),
             ("FCF 收益率", d.get("fcf_yield"), C_BAR)]
    if d.get("risk_free"):
        items.append(("無風險利率", d["risk_free"], "#E03131"))
        ax.axhline(d["risk_free"], color="#E03131", ls="--", lw=1)
    vals = [(n, v, c) for n, v, c in items if v is not None]
    if not vals:
        ax.text(0.5, 0.5, "缺少收益率資料", ha="center", va="center",
                transform=ax.transAxes, color=C_MUTED, fontsize=10)
        ax.set_axis_off()
        return
    bars = ax.bar([v[0] for v in vals], [v[1] for v in vals], width=0.4,
                  color=[v[2] for v in vals], alpha=0.9)
    for b, (_, v, _c) in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v * 100:.2f}%", ha="center",
                va="bottom", fontsize=9, color=C_INK, fontweight="bold")
    ax.set_ylim(0, max(v[1] for v in vals) * 1.3)
    _pct_fmt(ax)
    _axes(ax)
    ax.set_title("現金回報 vs 無風險利率", fontsize=9.6, color=C_TOTAL, pad=6)
    _tight(fig)


# ---------------------------------------------------------------- 可比
def comps_box(fig: Figure, res) -> None:
    fig.patch.set_facecolor("white")
    rows = res.comps.get("rows") or []
    pool = res.comps.get("peers") or []
    keys = [("pe", "P/E"), ("pb", "P/B"), ("ev_ebitda", "EV/EBITDA"),
            ("ev_sales", "EV/Sales")]
    axes = fig.subplots(1, 4)
    fig.suptitle("可比公司倍數分布（箱線）與目標公司位置（紅點）", fontsize=9.4,
                 color=C_TOTAL, y=0.98)
    for ax, (key, label) in zip(axes, keys):
        vals = [p.get(key) for p in pool if p.get(key) and p.get(key) > 0]
        if vals:
            bp = ax.boxplot([vals], widths=0.5, patch_artist=True, showfliers=False)
            for box in bp["boxes"]:
                box.set(facecolor="#E7F0F7", edgecolor=C_TOTAL, lw=1)
            for med in bp["medians"]:
                med.set(color=C_TV, lw=2)
        target = next((r["target"] for r in rows if r["metric"] == key), None)
        if target and target > 0:
            ax.plot([1], [target], "ro", ms=5)
            ax.annotate(f"{target:.1f}", (1, target), textcoords="offset points",
                        xytext=(9, 0), fontsize=7, color="#E03131", va="center")
        ax.set_title(label, fontsize=8.2, color=C_INK, pad=3)
        ax.set_xticks([])
        ax.tick_params(labelsize=6.8)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.grid(axis="y", color=C_GRID, lw=0.6)
    _tight(fig)


# ---------------------------------------------------------------- 營收
def revenue_trend(fig: Figure, res) -> None:
    """營收柱狀 ＋ 逐年成長率折線 ＋ 對數趨勢線。"""
    fig.patch.set_facecolor("white")
    rev = res.dupont.get("rev") or {}
    rows = [r for r in (rev.get("rows") or []) if r.get("revenue")]
    ax = fig.subplots(1, 1)
    if not rows:
        ax.text(0.5, 0.5, "缺少營收資料", ha="center", va="center",
                transform=ax.transAxes, color=C_MUTED, fontsize=10)
        ax.set_axis_off()
        return
    years = [r["year"] for r in rows]
    vals = [r["revenue"] for r in rows]
    bars = ax.bar(years, vals, width=0.52, color=C_BAR, alpha=0.85, label="營業收入")
    top = max(vals) * 1.22
    for b, r in zip(bars, rows):
        ax.text(b.get_x() + b.get_width() / 2, r["revenue"], f"{r['revenue']:,.0f}",
                ha="center", va="bottom", fontsize=7.2, color=C_INK)
        if r.get("yoy") is not None:
            ax.text(b.get_x() + b.get_width() / 2, r["revenue"] * 0.06,
                    f"{r['yoy'] * 100:+.1f}%", ha="center", va="bottom", fontsize=7.2,
                    color="white", fontweight="bold")

    ax2 = ax.twinx()
    yoy_years = [r["year"] for r in rows if r.get("yoy") is not None]
    yoy_vals = [r["yoy"] * 100 for r in rows if r.get("yoy") is not None]
    if yoy_vals:
        ax2.plot(yoy_years, yoy_vals, "-o", color=C_CONS, ms=4, lw=1.8, label="營收成長率")
        for x, v in zip(yoy_years, yoy_vals):
            ax2.annotate(f"{v:+.1f}%", (x, v), textcoords="offset points",
                         xytext=(0, 7), ha="center", fontsize=7, color=C_CONS)
        ax2.axhline(0, color=C_MUTED, lw=0.8, ls=":")
        ax2.set_ylabel("成長率（%）", fontsize=8.4, color=C_CONS)
        ax2.tick_params(labelsize=7.6, colors=C_CONS)
        ax2.spines["top"].set_visible(False)
        lo = min(yoy_vals); hi = max(yoy_vals)
        span = max(hi - lo, 4.0)
        ax2.set_ylim(lo - span * 0.55, hi + span * 0.75)

    tg = rev.get("trend_growth")
    if tg is not None:
        # 對數趨勢線：以首年營收為錨，按趨勢增長率外推（與實際營收同軸）
        first = vals[0]
        fitted = [first * (1 + tg) ** i for i in range(len(years))]
        ax.plot(years, fitted, "--", color=C_TV, lw=1.6,
                label=f"對數趨勢線（年增 {tg * 100:.1f}%）")

    ax.set_ylim(0, top)
    ax.set_ylabel(f"營收（{res.unit}）", fontsize=8.4)
    ax.set_title("營收與成長率趨勢", fontsize=9.6, color=C_TOTAL, pad=6)
    ax.margins(x=0.08)
    _axes(ax)
    _years(ax)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, fontsize=7.2, frameon=False, ncol=3, loc="upper left")
    _tight(fig)


def ebit_margin_trend(fig: Figure, res) -> None:
    """EBIT 利潤率（獨立輸出）vs 行業中位數。"""
    fig.patch.set_facecolor("white")
    e = res.dupont.get("ebit") or {}
    rows = [r for r in (e.get("rows") or []) if r.get("margin") is not None]
    ax = fig.subplots(1, 1)
    if not rows:
        ax.text(0.5, 0.5, "缺少 EBIT 或營收資料", ha="center", va="center",
                transform=ax.transAxes, color=C_MUTED, fontsize=10)
        ax.set_axis_off()
        return
    years = [r["year"] for r in rows]
    vals = [r["margin"] for r in rows]
    ax.bar(years, vals, width=0.5, color=C_BASE, alpha=0.22)
    ax.plot(years, vals, "-o", color=C_BASE, lw=2, ms=4.5, label="EBIT 利潤率")
    for x, v in zip(years, vals):
        ax.annotate(f"{v * 100:.1f}%", (x, v), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=7.6, color=C_BASE,
                    fontweight="bold")
    ind = next((r.get("industry_margin") for r in rows if r.get("industry_margin")), None)
    if ind is not None:
        ax.axhline(ind, color=C_CONS, ls="--", lw=1.2,
                   label=f"行業中位數 {ind * 100:.1f}%")
    avg3 = e.get("avg3")
    if avg3 is not None:
        ax.axhline(avg3, color=C_MUTED, ls=":", lw=1.1,
                   label=f"近三年平均 {avg3 * 100:.1f}%")
    hi = max(vals + [ind or 0, avg3 or 0])
    ax.set_ylim(0, hi * 1.3)
    _pct_fmt(ax)
    _axes(ax)
    _years(ax)
    ax.legend(fontsize=7.2, frameon=False, loc="upper left", ncol=2)
    ax.set_title("EBIT 利潤率（五階杜邦・第一因子）", fontsize=9.6, color=C_TOTAL, pad=6)
    _tight(fig)


def dupont5_factors(fig: Figure, res) -> None:
    """五階杜邦五因子小圖（＋ROE 合成）。"""
    fig.patch.set_facecolor("white")
    rows = res.dupont.get("rows") or []
    years = [r["year"] for r in rows]
    specs = [("ebit_margin", "EBIT 利潤率", True),
             ("interest_burden", "利息負擔（EBT/EBIT）", False),
             ("tax_burden", "稅負擔（淨利/EBT）", False),
             ("asset_turnover", "資產週轉（次）", False),
             ("equity_multiplier", "權益乘數（倍）", False),
             ("roe", "ROE（五因子乘積）", True)]
    axes = fig.subplots(2, 3, sharex=True)
    fig.suptitle("五階杜邦因子拆解", fontsize=9.6, color=C_TOTAL, y=0.99)
    for ax, (key, label, pct) in zip(axes.ravel(), specs):
        vals = [r.get(key) for r in rows]
        ax.plot(years, vals, "-o", color=C_BASE if key != "roe" else C_OPT,
                lw=1.6, ms=3.2)
        if pct:
            _pct_fmt(ax)
        ax.set_title(label, fontsize=7.8, color=C_INK, pad=2)
        ax.tick_params(labelsize=6.8)
        _axes(ax)
        _years(ax)
        ax.margins(y=0.25)
    _tight(fig)


# ---------------------------------------------------------------- 情景橋接
def scenario_bridge(fig: Figure, res) -> None:
    """三情景 × 三種方法（DDM／可比／DCF）股價對照。"""
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    sc = res.scenarios or []
    if not sc:
        ax.text(0.5, 0.5, "情景資料不足", ha="center", va="center",
                transform=ax.transAxes, color=C_MUTED, fontsize=10)
        ax.set_axis_off()
        return
    labels = [s["label"] for s in sc]
    xs = range(len(sc))
    series = [("DDM", "ddm_price", C_OPT), ("可比反推", "comps_price", C_TV),
              ("DCF", "dcf_price", C_BASE)]
    width = 0.24
    drew = False
    for i, (name, key, color) in enumerate(series):
        vals = [s.get(key) or 0.0 for s in sc]
        if not any(vals):
            continue
        pos = [x + (i - 1) * width for x in xs]
        bars = ax.bar(pos, vals, width=width, color=color, alpha=0.9, label=name)
        for b, v, s in zip(bars, vals, sc):
            if v:
                ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.1f}", ha="center",
                        va="bottom", fontsize=7, color=C_INK)
        drew = True
    if not drew:
        ax.text(0.5, 0.5, "三種方法皆無法給出股價", ha="center", va="center",
                transform=ax.transAxes, color=C_MUTED, fontsize=10)
        ax.set_axis_off()
        return
    if res.price:
        ax.axhline(res.price, color=C_MUTED, ls="--", lw=1.1,
                   label=f"現價 {res.price:,.2f}")
    ax.set_xticks(list(xs), labels, fontsize=9)
    ax.set_ylabel(f"股價（{res.currency}）", fontsize=8.6)
    top = max([s.get(k) or 0 for s in sc
               for k in ("ddm_price", "comps_price", "dcf_price")] + [res.price or 0])
    ax.set_ylim(0, top * 1.22)
    _axes(ax)
    ax.legend(fontsize=7.4, frameon=False, ncol=4, loc="upper left")
    ax.set_title("三情景 × 三方法（同一組偏移：成長／EBIT 利潤率／Ke）",
                 fontsize=9.4, color=C_TOTAL, pad=6)
    if not any(s.get("dcf_price") for s in sc):
        ax.text(0.5, -0.24, "DCF 股價不適用（企業價值低於淨負債），EV 對照見表格",
                transform=ax.transAxes, ha="center", fontsize=7.4, color=C_MUTED)
    _tight(fig)


BUILDERS = {
    "dupont_trend": dupont_trend,
    "dupont_scenario": dupont_scenario,
    "revenue_trend": revenue_trend,
    "ebit_margin_trend": ebit_margin_trend,
    "dupont5_factors": dupont5_factors,
    "scenario_bridge": scenario_bridge,
    "credit_z": credit_z,
    "credit_ratios": credit_ratios,
    "ddm_price": ddm_price,
    "ddm_yield": ddm_yield,
    "comps_box": comps_box,
}


def draw(fig: Figure, kind: str, res) -> bool:
    fig.clear()
    fig.patch.set_facecolor("white")
    try:
        BUILDERS[kind](fig, res)
        return True
    except Exception:
        ax = fig.subplots(1, 1)
        ax.text(0.5, 0.5, "資料不足，無法繪製此圖", ha="center", va="center",
                fontsize=10, color=C_MUTED, transform=ax.transAxes)
        ax.set_axis_off()
        return False
