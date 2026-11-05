"""
mpl_charts.py — DCF 股價估算器圖表（matplotlib，供 Tkinter 內嵌）
================================================================
四張輔助圖，色彩語義與 FCFF 工具一致（theme.PALETTE）：
  1. sensitivity_heatmap  WACC × 永續增長率 熱圖（核心輔助圖）
  2. contribution_chart   逐年折現現金流貢獻 ＋ 累計線
  3. scenario_chart       樂觀／基準／保守 三情景股價
  4. bridge_chart         EV → 股權價值 瀑布橋接

每個函式「畫在呼叫方給的 Figure 上」，方便 Tkinter 直接內嵌。
"""

from __future__ import annotations

import warnings
from typing import List, Optional

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure  # noqa: E402

C_OPT = "#0EA5A4"      # 樂觀
C_BASE = "#3B5BDB"     # 基準
C_CONS = "#E8590C"     # 保守
C_TV = "#E8A317"       # 終值
C_BAR = "#3F6E7A"      # 明確期
C_TOTAL = "#1F5C5C"    # 小計
C_INK = "#343A40"
C_MUTED = "#868E96"
C_GRID = "#DEE2E6"
C_NEG = "#B7C0C7"      # 淨負債

plt_rc = {
    "font.sans-serif": ["Microsoft YaHei UI", "Microsoft JhengHei", "SimHei", "sans-serif"],
    "axes.unicode_minus": False,
    "font.size": 9,
}
matplotlib.rcParams.update(plt_rc)


def _tight(fig) -> None:
    """安全版 tight_layout：極端資料（如全部數值為 0）下會發警告，這裡靜默處理。"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            _tight(fig)
        except Exception:  # 佈局失敗也不該讓整張圖空白
            pass


def _ceil(lo: float, hi: float, pad: float = 1.0) -> tuple:
    """回傳不會退化的 (bottom, top)，避免 ylim 上下限相同。"""
    if not (hi > lo):
        hi = lo + pad
    return lo, hi


def _style_axes(ax) -> None:
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(C_MUTED)
    ax.tick_params(colors=C_INK, labelsize=8.5)


# ---------------------------------------------------------------- 1 熱圖
def sensitivity_heatmap(fig: Figure, res) -> None:
    """WACC（列）× g（欄）內在股價熱圖；WACC ≤ g 的格標 n/a。"""
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    sens = res.sensitivity
    waccs, gs = sens["wacc_axis"], sens["g_axis"]
    cells = sens["cells"]
    n_r, n_c = len(waccs), len(gs)

    import numpy as np
    arr = np.full((n_r, n_c), np.nan)
    for i in range(n_r):
        for j in range(n_c):
            if cells[i][j] is not None:
                arr[i][j] = cells[i][j]
    valid = arr[~np.isnan(arr)]
    lo, hi = (valid.min(), valid.max()) if valid.size else (0.0, 1.0)
    if hi - lo < 1e-9:
        hi = lo + 1e-9

    im = ax.imshow(arr, cmap="YlGnBu", aspect="auto",
                   vmin=lo, vmax=hi, origin="upper")
    fig.colorbar(im, ax=ax, shrink=0.85, pad=0.015)

    fmt = lambda v: f"{v:,.2f}" if v is not None else "n/a"
    for i in range(n_r):
        for j in range(n_c):
            v = cells[i][j]
            none_case = v is None
            # 深底格用白字
            rel = None if none_case else (v - lo) / (hi - lo)
            color = "#C92A2A" if none_case else ("white" if rel is not None and rel > 0.62 else C_INK)
            ax.text(j, i, "n/a" if none_case else fmt(v),
                    ha="center", va="center", fontsize=7.6, color=color)
    # 基準位置框記
    bi = sens.get("base_index")
    if bi and 0 <= bi[0] < n_r and 0 <= bi[1] < n_c:
        from matplotlib.patches import Rectangle
        ax.add_patch(Rectangle((bi[1] - 0.5, bi[0] - 0.5), 1, 1, fill=False,
                               edgecolor="#E8590C", lw=2.2))

    ax.set_xticks(range(n_c), [f"{g * 100:.1f}%" for g in gs], fontsize=8)
    ax.set_yticks(range(n_r), [f"{w * 100:.1f}%" for w in waccs], fontsize=8)
    ax.set_xlabel("永續增長率 g", fontsize=9, color=C_INK)
    ax.set_ylabel("WACC", fontsize=9, color=C_INK)
    ax.set_title(f"{sens['metric_label']} 敏感性熱圖（{sens['currency']}）— "
                 "n/a = WACC ≤ g，不成立", fontsize=10, color=C_TOTAL, pad=8)
    _tight(fig)


# ---------------------------------------------------------------- 2 貢獻
def contribution_chart(fig: Figure, res) -> None:
    """逐年折現後 FCFF 長條 ＋ 累計現值虛線（至 EV）。"""
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    rows = res.rows
    s = res.summary
    xs = [r["fiscal_year"] for r in rows]
    pvs = [max(r["pv"], 0.0) for r in rows]
    cum = [r["cum_pv"] for r in rows]
    cur = C_BAR

    bars = ax.bar(xs, pvs, width=0.55, color=cur, alpha=0.88, label="逐年折現 FCFF")
    for b, v in zip(bars, pvs):
        ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.0f}",
                ha="center", va="bottom", fontsize=7.8, color=C_INK)
    ax.plot(xs, cum, "--", color="#E8590C", lw=1.6, marker="o", ms=3.5,
            label="累計現值")
    ax.axhline(s["ev"], color=C_TOTAL, lw=1.2, ls=":")
    ax.text(xs[-1], s["ev"], f"  EV {s['ev']:,.0f}", va="bottom", ha="right",
            fontsize=8.5, color=C_TOTAL, fontweight="bold")
    ax.set_ylabel(f"折現後現金流（{res.inp.currency} {res.inp.unit}）", fontsize=9)
    ax.set_xlabel("年度", fontsize=9)
    ax.set_title("折現現金流逐年貢獻曲線", fontsize=10, color=C_TOTAL, pad=8)
    _style_axes(ax)
    ax.legend(fontsize=8, frameon=False, loc="upper left")
    ax.margins(y=0.16)
    _tight(fig)


# ---------------------------------------------------------------- 3 情景
def scenario_chart(fig: Figure, res) -> None:
    """三情景內在股價 ＋ 現價虛線。"""
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    colors = {"optimistic": C_OPT, "base": C_BASE, "conservative": C_CONS}
    labels, prices, cols = [], [], []
    for sc in res.scenarios:
        labels.append(sc["label"] + ("" if sc["valid"] else "（n/a）"))
        prices.append(sc["price"] if sc["valid"] else 0.0)
        cols.append(colors.get(sc["key"], C_BAR))

    bars = ax.bar(labels, prices, width=0.5, color=cols, alpha=0.9)
    for b, v, sc in zip(bars, prices, res.scenarios):
        if sc["valid"]:
            ax.text(b.get_x() + b.get_width() / 2, v, f"{v:,.2f}",
                    ha="center", va="bottom", fontsize=9, fontweight="bold", color=C_INK)
            up = sc.get("upside")
            if up is not None:
                ax.text(b.get_x() + b.get_width() / 2, v * 0.5,
                        f"{up * 100:+.1f}%", ha="center", fontsize=8, color="white")
        else:
            ax.text(b.get_x() + b.get_width() / 2, 0.02, "WACC ≤ g",
                    ha="center", fontsize=8, color="#C92A2A")
    mp = res.inp.price
    if mp:
        ax.axhline(mp, color=C_MUTED, lw=1.3, ls="--")
        ax.text(len(labels) - 0.42, mp, f" 現價 {mp:,.2f}", va="bottom",
                ha="left", fontsize=8.5, color=C_MUTED)
    ax.set_ylabel(f"內在股價（{res.inp.currency}）", fontsize=9)
    ax.set_title("三情景預計股價（輔助）", fontsize=10, color=C_TOTAL, pad=8)
    _style_axes(ax)
    ax.margins(y=0.18)
    _tight(fig)


# ---------------------------------------------------------------- 4 橋接
def bridge_chart(fig: Figure, res) -> None:
    """瀑布圖：明確期 PV ＋ 終值 PV → EV → 減淨負債 → 股權價值。"""
    fig.patch.set_facecolor("white")
    ax = fig.subplots(1, 1)
    s = res.summary
    ev, eq = s["ev"], max(s["equity"], 0.0)
    steps = [
        ("高速增長期\n現值", 0.0, s["pv_explicit"], C_BAR, "sum"),
        ("永續終值\n現值", s["pv_explicit"], ev, C_TV, "float"),
        ("企業價值 EV", 0.0, ev, C_TOTAL, "sum"),
        ("減：淨負債", min(eq, ev), max(eq, ev), C_NEG, "float"),
        ("股權價值", 0.0, eq, "#3B5BDB", "sum"),
    ]
    tops = max(max(ev, eq) * 1.16, 1.0)   # 全為 0 時避免 ylim 退化
    for i, (label, y0, y1, color, kind) in enumerate(steps):
        bottom, height = y0, y1 - y0
        alpha = 0.62 if kind == "sum" and color in (C_BAR, "#3B5BDB") else 0.92
        ax.bar(i, height, bottom=bottom, width=0.56, color=color, alpha=alpha)
        val = abs(y1 - y0)
        prefix = "−" if label.startswith("減") else ""
        ax.text(i, y1 + tops * 0.015, f"{prefix}{val:,.0f}", ha="center",
                fontsize=8.6, fontweight="bold", color=C_INK)
        if i < len(steps) - 1 and kind == "float" or (i == 0):
            nxt_y = steps[i + 1][1] if steps[i + 1][4] == "float" else steps[i + 1][2]
            ax.plot([i + 0.28, i + 1 - 0.28], [y1, y1], ls=":", color=C_MUTED, lw=1)
    ax.set_xticks(range(len(steps)), [s[0] for s in steps], fontsize=8.4)
    ax.set_ylim(0, tops)
    ax.set_ylabel(f"金額（{res.inp.currency} {res.inp.unit}）", fontsize=9)
    ax.set_title("企業價值 → 股權價值 橋接", fontsize=10, color=C_TOTAL, pad=8)
    _style_axes(ax)
    _tight(fig)


BUILDERS = {
    "heatmap": sensitivity_heatmap,
    "contribution": contribution_chart,
    "scenario": scenario_chart,
    "bridge": bridge_chart,
}


def draw(fig: Figure, kind: str, res) -> bool:
    """在 fig 上畫指定圖種；資料不足時留白並標示原因。"""
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
