"""
diag_tab.py — 企業診斷｜四合一配套模組（Tkinter 頁籤）
================================================================
掛在主視窗 Notebook 的第二個頁籤。定位＝**參考校準工具**：
  * 只輸出參考數據、交叉比對、風險警示；
  * 「推送建議值到 DCF」僅寫入使用者從未手動修改的欄位；
  * 可讀取 DCF 的成長／利潤率假設做「偏高／偏低」對照，但不修改 DCF。

五張可收合卡片：
  ① 企業基本面｜杜邦分析　② 信用風險｜Altman Z'' ＋ 財務比率
  ③ 絕對估值｜DDM ＋ FCF Yield　④ 相對估值｜可比公司
  ⑤ 綜合摘要（對 DCF 的校準建議）
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

import threading

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import diagnostics
import diag_charts
import diag_export
import peers as peer_lib

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

C_HEADER = "#1F5C5C"
C_BG = "#F4F6F7"
C_ACCENT = "#3B5BDB"
C_OK = "#0CA678"
C_ERR = "#E03131"
C_LOCK = "#E8A317"

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")

MARKETS = [("HK", "港股"), ("MO", "澳門上市"), ("A", "A 股")]

DDM_FIELDS = [("dps", "每股股息 DPS"), ("ke", "折現率 Ke（%）"), ("g", "股息永續增長率 g（%）")]


class CollapsibleCard(tk.Frame):
    """可收合卡片（點標題列切換）。"""

    def __init__(self, parent: tk.Frame, title: str, subtitle: str = "",
                 expanded: bool = True):
        super().__init__(parent, bg=C_BG, bd=1, relief="groove")
        self._expanded = expanded
        head = tk.Frame(self, bg="#E3EDED", cursor="hand2")
        head.pack(fill="x")
        self.btn = tk.Label(head, text="▾" if expanded else "▸", font=FONT_BOLD,
                            bg="#E3EDED", width=2, cursor="hand2")
        self.btn.pack(side="left", padx=(6, 0), pady=3)
        tk.Label(head, text=title, font=FONT_BOLD, bg="#E3EDED",
                 fg=C_HEADER).pack(side="left")
        if subtitle:
            tk.Label(head, text=subtitle, font=FONT_SMALL, bg="#E3EDED",
                     fg="#5F6C73").pack(side="left", padx=10)
        for w in (head, self.btn):
            w.bind("<Button-1>", lambda _e: self.toggle())
        self.body = tk.Frame(self, bg=C_BG)
        if expanded:
            self.body.pack(fill="x", padx=8, pady=6)

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self.btn.config(text="▾" if self._expanded else "▸")
        if self._expanded:
            self.body.pack(fill="x", padx=8, pady=6)
        else:
            self.body.pack_forget()

    def section(self, title: str) -> tk.Frame:
        sec = tk.Frame(self.body, bg=C_BG)
        sec.pack(fill="x", pady=(6, 2))
        tk.Label(sec, text=title, font=FONT_SMALL, bg=C_BG, fg=C_HEADER,
                 ).pack(anchor="w")
        return sec


class DiagnosticsTab:
    """企業診斷頁籤控制器。"""

    def __init__(self, app, nb: ttk.Notebook):
        self.app = app
        self.frame = tk.Frame(nb, bg=C_BG)
        nb.add(self.frame, text=" ② 企業診斷｜四合一（參考校準） ")

        self.state: Dict[str, Any] = {
            "overrides": {},          # 使用者覆寫（ddm.ke / ddm.g / ddm.dps / comps.pe …）
            "markets": ["HK", "MO"],
            # 統一三情景偏移（營收成長／EBIT 利潤率／Ke），四模組＋DCF 同步套用
            "offsets": {"scenario": {k: dict(v) for k, v in
                                      diagnostics.DEFAULT_SCENARIO_OFFSETS.items()}},
        }
        self.result = None
        self.figures: Dict[str, Figure] = {}
        self.canvases: Dict[str, FigureCanvasTkAgg] = {}
        self.peer_rows: Optional[List[Dict[str, Any]]] = None   # 手動編輯後的樣本池

        self._build_topbar()
        self._build_body()
        self._build_cards()

    # ============================================================ 版面
    def _build_topbar(self) -> None:
        bar = tk.Frame(self.frame, bg="#E3EDED")
        bar.pack(fill="x")
        self.lbl_company = tk.Label(
            bar, text="尚未載入財報——請先在「① DCF 股價估值」頁籤載入（兩頁共用同一份財報）",
            font=FONT_BOLD, bg="#E3EDED", fg="#5F6C73")
        self.lbl_company.pack(side="left", padx=12, pady=6)
        tk.Button(bar, text="▶ 執行診斷", font=FONT_BOLD, command=self.refresh,
                  bg="#0E7C7B", fg="white", relief="flat", padx=16).pack(
            side="right", padx=10, pady=4)
        self._mk = {}
        mkf = tk.Frame(bar, bg="#E3EDED")
        mkf.pack(side="right", padx=6)
        tk.Label(mkf, text="可比市場：", font=FONT_SMALL, bg="#E3EDED").pack(side="left")
        for code, label in MARKETS:
            v = tk.BooleanVar(value=code in self.state["markets"])
            cb = tk.Checkbutton(mkf, text=label, variable=v, font=FONT_SMALL,
                                bg="#E3EDED", command=lambda c=code, vv=v: self._on_market(c, vv))
            cb.pack(side="left")
            self._mk[code] = v

    def _on_market(self, code: str, var: Optional[tk.BooleanVar] = None) -> None:
        if var is not None:
            on = var.get()
            if on and code not in self.state["markets"]:
                self.state["markets"].append(code)
            elif not on and code in self.state["markets"]:
                self.state["markets"].remove(code)
        self.peer_rows = None   # 市場變更 → 回用自動樣本池

    def _scrollable(self, parent: tk.Frame) -> tk.Frame:
        canvas = tk.Canvas(parent, bg=C_BG, highlightthickness=0)
        vs = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=C_BG)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vs.set)
        canvas.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        def _wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _build_body(self) -> None:
        self.body_wrap = tk.Frame(self.frame, bg=C_BG)
        self.body_wrap.pack(fill="both", expand=True)
        self.inner = self._scrollable(self.body_wrap)

    def _build_cards(self) -> None:
        self.card1 = CollapsibleCard(self.inner, "① 企業基本面｜杜邦分析",
                                     "ROE = 淨利率 × 資產週轉 × 權益乘數")
        self.card1.pack(fill="x", pady=4)
        self.card2 = CollapsibleCard(self.inner, "② 信用風險｜Altman Z'' 與財務比率",
                                     "非製造業版；缺股價仍可計算")
        self.card2.pack(fill="x", pady=4)
        self.card3 = CollapsibleCard(self.inner, "③ 絕對估值｜DDM 與 FCF Yield",
                                     "適合穩定派息企業；無派息自動停用")
        self.card3.pack(fill="x", pady=4)
        self.card4 = CollapsibleCard(self.inner, "④ 相對估值｜可比公司",
                                     "P/E・P/B・EV/EBITDA・EV/Sales")
        self.card4.pack(fill="x", pady=4)
        self.card5 = CollapsibleCard(self.inner, "⑤ 綜合摘要｜對 DCF 的校準建議",
                                     "整合四模組結論（不覆寫 DCF）")
        self.card5.pack(fill="x", pady=4)

        self._build_card1()
        self._build_card2()
        self._build_card3()
        self._build_card4()
        self._build_card5()

    # ------------------------------------------------------------ 共用小件
    def fig(self, kind: str, parent: tk.Frame, w=6.4, h=3.6) -> None:
        cell = tk.Frame(parent, bg="white", bd=1, relief="solid")
        cell.pack(fill="x", pady=4)
        fig = Figure(figsize=(w, h), dpi=96)
        canvas = FigureCanvasTkAgg(fig, master=cell)
        canvas.get_tk_widget().pack(fill="both", expand=True)
        self.figures[kind], self.canvases[kind] = fig, canvas

    def _tags(self, parent: tk.Frame, tags: List[Dict[str, str]]) -> None:
        for w in parent.winfo_children():
            w.destroy()
        for t in tags or []:
            color = {"high": "#FFF0F0", "med": "#FFF8E6", "ok": "#E6FCF5"}.get(
                t.get("level"), "#EDF2F7")
            fg = {"high": "#E03131", "med": "#F08C00", "ok": C_OK}.get(t.get("level"), "#3B5BDB")
            box = tk.Frame(parent, bg=color)
            box.pack(fill="x", pady=2)
            tk.Label(box, text=("⚠ " if t.get("level") in ("high", "med") else "• ")
                     + t.get("title", ""), font=FONT_BOLD, bg=color, fg=fg
                     ).pack(anchor="w", padx=8, pady=(3, 0))
            tk.Label(box, text=t.get("detail", ""), font=FONT_SMALL, bg=color,
                     fg="#495057", wraplength=900, justify="left"
                     ).pack(anchor="w", padx=8, pady=(0, 3))

    def _table(self, parent: tk.Frame, cols: Tuple[Tuple[str, int, str], ...],
               height: int = 6) -> ttk.Treeview:
        tv = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings",
                          height=height)
        for key, w, title in cols:
            anchor = "e" if w < 0 else "w"
            tv.heading(key, text=title)
            tv.column(key, width=abs(w), anchor=anchor)
        tv.pack(fill="x", pady=3)
        return tv

    def _push_button(self, parent: tk.Frame, values_fn) -> None:
        tk.Button(parent, text="→ 推送建議值到 DCF（僅套用未手動修改的欄位）",
                  font=FONT_BOLD, command=lambda: self._push(values_fn),
                  bg="#3B5BDB", fg="white", relief="flat", padx=10
                  ).pack(anchor="w", pady=(6, 0))

    def _push(self, values_fn) -> None:
        if self.result is None:
            messagebox.showinfo("尚未執行", "請先按「▶ 執行診斷」。", parent=self.frame)
            return
        values = values_fn()
        if not values:
            messagebox.showinfo("沒有可推送的建議值", "本次診斷未產生建議值。",
                                parent=self.frame)
            return
        applied, skipped = self.app.push_suggestions(values)
        msg = []
        if applied:
            msg.append("✓ 已寫入 DCF 欄位：" + "、".join(applied))
        if skipped:
            msg.append("🔒 已跳過（你曾手動修改，永遠不覆蓋）：" + "、".join(skipped))
        if not applied and not skipped:
            msg.append("沒有可套用的欄位。")
        messagebox.showinfo("推送結果", "\n".join(msg), parent=self.frame)

    # ------------------------------------------------------------ 卡片 1
    def _build_card1(self) -> None:
        c = self.card1
        # KPI 摘要列
        kpi = tk.Frame(c.body, bg="#EDF2F7")
        kpi.pack(fill="x", pady=(0, 4))
        self.dupont_kpis = {}
        for key, title in (("rev_cagr", "營收 CAGR"), ("rev_trend", "營收趨勢增長"),
                           ("ebit_margin", "EBIT 利潤率（3年平均）"),
                           ("roe", "ROE（3年平均）"), ("rev_yoy", "最新年度營收成長")):
            box = tk.Frame(kpi, bg="#EDF2F7")
            box.pack(side="left", expand=True, fill="both", padx=6, pady=6)
            tk.Label(box, text=title, font=FONT_SMALL, bg="#EDF2F7", fg="#7A868F").pack()
            lb = tk.Label(box, text="—", font=("Microsoft YaHei UI", 13, "bold"),
                          bg="#EDF2F7", fg=C_HEADER)
            lb.pack()
            self.dupont_kpis[key] = lb

        # 圖：營收趨勢 ＋ EBIT 利潤率
        row = tk.Frame(c.body, bg=C_BG)
        row.pack(fill="x")
        left = tk.Frame(row, bg=C_BG); left.pack(side="left", fill="both", expand=True)
        right = tk.Frame(row, bg=C_BG); right.pack(side="left", fill="both")
        self.fig("revenue_trend", left)
        self.fig("ebit_margin_trend", right, w=3.4, h=3.6)
        self.fig("dupont5_factors", c.body, h=4.0)
        self.fig("dupont_trend", c.body)
        self.fig("dupont_scenario", c.body, w=6.4, h=2.8)

        # 表：營收與成長率
        sec = c.section("歷年營收與成長率（自動計算 YoY ＋ CAGR ＋ 對數趨勢線）")
        row2 = tk.Frame(sec, bg=C_BG); row2.pack(fill="x")
        lft = tk.Frame(row2, bg=C_BG); lft.pack(side="left", fill="both", expand=True)
        rgt = tk.Frame(row2, bg=C_BG); rgt.pack(side="left", fill="both")
        self.rev_table = self._table(lft, (
            ("year", 70, "年度"), ("revenue", -120, f"營收"),
            ("yoy", -100, "成長率"), ("index", -90, "指數")), height=6)
        self.ebit_table = self._table(rgt, (
            ("year", 70, "年度"), ("ebit", -100, "EBIT"),
            ("margin", -100, "EBIT 利潤率"), ("ind", -100, "行業中位數"),
            ("gap", -90, "差距")), height=6)

        # 表：五階杜邦
        sec3 = c.section("五階杜邦：ROE = EBIT 利潤率 × 利息負擔 × 稅負擔 × 資產週轉 × 權益乘數")
        self.dupont_table = self._table(c.body, (
            ("year", 60, "年度"), ("ebit_margin", -105, "① EBIT 利潤率"),
            ("interest", -100, "② 利息負擔"), ("tax", -95, "③ 稅負擔"),
            ("net_margin", -95, "＝淨利率"), ("turnover", -95, "④ 週轉（次）"),
            ("em", -90, "⑤ 乘數（倍）"), ("roe", -95, "ROE")))
        self.dupont_footnote = tk.Label(c.body, text="", font=FONT_SMALL, bg=C_BG,
                                        fg="#7A868F", wraplength=920, justify="left")
        self.dupont_footnote.pack(anchor="w", fill="x")

        # 統一三情景偏移（三個旋鈕）
        sec4 = c.section("三情景偏移（統一旋鈕：同時套用到杜邦／Z''／DDM／可比／DCF）")
        srow = tk.Frame(sec4, bg=C_BG); srow.pack(anchor="w")
        self.scen_vars: Dict[str, Dict[str, tk.StringVar]] = {}
        tk.Label(srow, text="情景", font=FONT_BOLD, bg=C_BG, width=8).grid(row=0, column=0)
        for ci, (knob, label) in enumerate(
                (("growth", "營收成長 ±pp"), ("margin", "EBIT 利潤率 ±pp"), ("ke", "Ke ±pp"))):
            tk.Label(srow, text=label, font=FONT_BOLD, bg=C_BG, width=14).grid(row=0, column=ci + 1)
        for ri, (key, label) in enumerate((("optimistic", "樂觀"), ("conservative", "保守"))):
            tk.Label(srow, text=label, font=FONT, bg=C_BG, width=8).grid(row=ri + 1, column=0, sticky="e")
            self.scen_vars[key] = {}
            for ci, knob in enumerate(("growth", "margin", "ke")):
                init = diagnostics.DEFAULT_SCENARIO_OFFSETS[key][knob] * 100
                e = tk.Entry(srow, width=8, font=FONT, justify="right")
                e.insert(0, f"{init:+.1f}")
                e.grid(row=ri + 1, column=ci + 1, padx=3)
                e.bind("<KeyRelease>", lambda _e: self._collect_scen_offsets())
                self.scen_vars[key][knob] = e

        sec5 = c.section("三情景橋接（每個模型都對同一組偏移作出回應）")
        self.scen_table = self._table(c.body, (
            ("scen", 62, "情景"), ("growth", -95, "營收成長"), ("margin", -100, "EBIT 利潤率"),
            ("ke", -85, "Ke"), ("roe", -90, "ROE"), ("z", -80, "Z''"),
            ("ddm", -95, "DDM 價"), ("comps", -95, "可比價"), ("dcf", -95, "DCF 價")))
        self.scen_note = tk.Label(c.body, text="", font=FONT_SMALL, bg=C_BG,
                                  fg="#7A868F", wraplength=920, justify="left")
        self.scen_note.pack(anchor="w", fill="x")

        self.driver_lbl = tk.Label(c.body, text="", font=FONT, bg=C_BG, fg="#343A40",
                                   wraplength=920, justify="left")
        self.driver_lbl.pack(anchor="w", pady=(4, 0))
        self.tags1 = tk.Frame(c.body, bg=C_BG); self.tags1.pack(fill="x")
        self._push_button(c.body, self._dupont_push)
        self.auto_note = tk.Label(c.body, text="", font=FONT_SMALL, bg=C_BG,
                                  fg="#0CA678", wraplength=920, justify="left")
        self.auto_note.pack(anchor="w", fill="x")

    def _collect_scen_offsets(self) -> None:
        scen = {"base": {"growth": 0.0, "margin": 0.0, "ke": 0.0}}
        for key in ("optimistic", "conservative"):
            d = {}
            for knob in ("growth", "margin", "ke"):
                try:
                    d[knob] = float(self.scen_vars[key][knob].get().strip() or 0.0) / 100.0
                except ValueError:
                    d[knob] = diagnostics.DEFAULT_SCENARIO_OFFSETS[key][knob]
            scen[key] = d
        self.state["offsets"] = {"scenario": scen}

    def _dupont_push(self) -> Dict[str, Any]:
        """建議值：營收 CAGR → 逐年成長率；歷史 EBIT 利潤率 → 逐年利潤率。"""
        d = self.result.dupont
        vals = {}
        years = len(self.result.years)
        if d.get("cagr") is not None:
            vals["growth"] = [d["cagr"]] * years
        if d.get("hist_ebit_margin") is not None:
            vals["margin"] = [d["hist_ebit_margin"]] * years
        return vals

    # ------------------------------------------------------------ 卡片 2
    def _build_card2(self) -> None:
        c = self.card2
        self.fig("credit_z", c.body)
        self.fig("credit_ratios", c.body, h=4.4)
        self.credit_table = self._table(c.body, (
            ("year", 70, "年度"), ("z", -90, "Z'' 分數"), ("zone", -90, "分級"),
            ("zone_adj", -110, "行業調整分級"),
            ("cr", -100, "流動比率"), ("qr", -100, "速動比率"),
            ("dr", -110, "資產負債率"), ("icr", -110, "利息覆蓋（倍）")))
        self.tags2 = tk.Frame(c.body, bg=C_BG); self.tags2.pack(fill="x")
        self.ind_note = tk.Label(c.body, text="", font=FONT_SMALL, bg="#FFF8E6",
                                 fg="#8A5A00", wraplength=920, justify="left")
        self.ind_note.pack(anchor="w", fill="x", pady=(4, 0))

    # ------------------------------------------------------------ 卡片 3
    def _build_card3(self) -> None:
        c = self.card3
        ctl = tk.Frame(c.body, bg=C_BG)
        ctl.pack(fill="x")
        tk.Label(ctl, text="Gordon 增長模型：", font=FONT, bg=C_BG).pack(side="left")
        self.ddm_enabled = tk.BooleanVar(value=True)
        tk.Checkbutton(ctl, text="啟用 DDM", variable=self.ddm_enabled,
                       font=FONT, bg=C_BG).pack(side="left", padx=(4, 16))
        self.sim_fail = tk.BooleanVar(value=False)
        tk.Checkbutton(ctl, text="（測試）模擬宏觀失敗", variable=self.sim_fail,
                       font=FONT_SMALL, bg=C_BG).pack(side="left")
        tk.Label(ctl, text="　Ke 來源：", font=FONT_SMALL, bg=C_BG, fg="#7A868F"
                 ).pack(side="left")
        self.ke_source = tk.Label(ctl, text="宏觀模型／預設", font=FONT_SMALL,
                                  bg=C_BG, fg="#7A868F")
        self.ke_source.pack(side="left")

        fields = tk.Frame(c.body, bg=C_BG)
        fields.pack(fill="x", pady=4)
        self.ddm_vars: Dict[str, tk.StringVar] = {}
        for key, label in DDM_FIELDS:
            tk.Label(fields, text=label + "：", font=FONT, bg=C_BG).pack(side="left",
                                                                        padx=(10, 2))
            e = tk.Entry(fields, width=9, font=FONT, justify="right")
            e.pack(side="left")
            e.bind("<KeyRelease>", lambda _e, k=key: self._on_ddm_edit(k))
            self.ddm_vars[key] = e
        tk.Button(fields, text="⚡ 由宏觀模型取得權益成本", font=FONT_BOLD,
                  command=self._fetch_ke, bg="#0E7C7B", fg="white", relief="flat",
                  padx=10).pack(side="left", padx=(16, 0))

        kpi = tk.Frame(c.body, bg="#EDF2F7")
        kpi.pack(fill="x", pady=4)
        self.ddm_kpis = {}
        for key, title in (("price", "DDM 內在股價"), ("div_yield", "股息收益率"),
                           ("fcf_yield", "FCF 收益率"), ("risk_free", "無風險利率")):
            box = tk.Frame(kpi, bg="#EDF2F7")
            box.pack(side="left", expand=True, fill="both", padx=6, pady=6)
            tk.Label(box, text=title, font=FONT_SMALL, bg="#EDF2F7", fg="#7A868F"
                     ).pack()
            lb = tk.Label(box, text="—", font=("Microsoft YaHei UI", 15, "bold"),
                          bg="#EDF2F7", fg=C_HEADER)
            lb.pack()
            self.ddm_kpis[key] = lb

        self.ddm_note = tk.Label(c.body, text="", font=FONT_SMALL, bg=C_BG,
                                 fg="#F08C00", wraplength=920, justify="left")
        self.ddm_note.pack(anchor="w", fill="x")
        row = tk.Frame(c.body, bg=C_BG)
        row.pack(fill="x")
        self.fig("ddm_price", row)
        self.fig("ddm_yield", row, w=3.4, h=3.6)
        self.ddm_table = self._table(c.body, (
            ("scen", 80, "情景"), ("ke", -110, "Ke"), ("g", -110, "g"),
            ("price", -140, "DDM 內在價"), ("upside", -110, "較市價")))
        self.tags3 = tk.Frame(c.body, bg=C_BG); self.tags3.pack(fill="x")

    def _on_ddm_edit(self, key: str) -> None:
        raw = self.ddm_vars[key].get().strip().replace(",", "").replace("%", "")
        try:
            v = float(raw)
        except ValueError:
            return
        if key in ("ke", "g"):
            v /= 100.0
        self.state["overrides"][f"ddm.{key}"] = v
        self.ke_source.config(text="手動輸入（已鎖定，宏觀刷新不覆蓋）", fg=C_LOCK)

    def _fetch_ke(self) -> None:
        diag = self._make_diag()
        snap = diag.fetch_macro(simulate_failure=self.sim_fail.get())
        if snap is None:
            self.ke_source.config(text="宏觀抓取失敗——已降級，保留手動 Ke，不阻斷計算",
                                  fg="#F08C00")
            messagebox.showwarning("宏觀抓取失敗",
                                   "宏觀模型無回應（可勾選「模擬宏觀失敗」測試降級流程）。\n"
                                   "已保留手動 Ke／預設值，可直接繼續計算。",
                                   parent=self.frame)
            return
        if "ddm.ke" not in self.state["overrides"]:
            self.ddm_vars["ke"].delete(0, "end")
            self.ddm_vars["ke"].insert(0, f"{snap.cost_of_equity * 100:.2f}")
        self.ke_source.config(
            text=f"宏觀 CAPM：Rf {snap.risk_free * 100:.2f}% + β {snap.beta:.2f} × "
                 f"ERP {snap.equity_risk_premium * 100:.2f}% = Ke {snap.cost_of_equity * 100:.2f}%",
            fg=C_OK)
        self.refresh()

    # ------------------------------------------------------------ 卡片 4
    def _build_card4(self) -> None:
        c = self.card4
        row0 = tk.Frame(c.body, bg=C_BG)
        row0.pack(fill="x")
        tk.Button(row0, text="🌐 自動抓取同業（Yahoo）", font=FONT_BOLD,
                  command=self._auto_peers, bg=C_ACCENT, fg="white",
                  relief="flat", padx=10).pack(side="left", padx=(0, 6))
        tk.Button(row0, text="編輯可比樣本池", font=FONT, command=self._edit_peers,
                  bg="#E8EDF2", relief="groove", padx=8).pack(side="left")
        tk.Button(row0, text="匯入樣本 CSV", font=FONT, command=self._import_peers,
                  bg="#E8EDF2", relief="groove", padx=8).pack(side="left", padx=(6, 0))
        self.peer_lbl = tk.Label(row0, text="", font=FONT_SMALL, bg=C_BG, fg="#7A868F")
        self.peer_lbl.pack(side="left", padx=10)
        self.comps_table = self._table(c.body, (
            ("metric", -110, "指標"), ("target", -100, "目標公司"),
            ("median", -100, "中位數"), ("mean", -100, "均值"),
            ("iqr", -130, "Q1–Q3"), ("implied", -110, "反推股價"),
            ("prem", -110, "溢價/折價")))
        self.verdict_lbl = tk.Label(c.body, text="", font=FONT_BOLD, bg=C_BG,
                                    fg=C_HEADER, wraplength=920, justify="left")
        self.verdict_lbl.pack(anchor="w", pady=(4, 0))
        self.fig("comps_box", c.body)
        self.comps_table2 = self._table(c.body, (
            ("scen", 80, "情景"), ("price", -140, "反推股價"),
            ("upside", -110, "較市價")), height=3)
        self.tags4 = tk.Frame(c.body, bg=C_BG); self.tags4.pack(fill="x")
        self._push_button(c.body, self._comps_push)

    def _comps_push(self) -> Dict[str, Any]:
        c = self.result.comps
        vals = {}
        if c.get("industry_margin") is not None:
            vals["margin"] = [c["industry_margin"]] * len(self.result.years)
        if c.get("industry_growth") is not None:
            vals["growth"] = [c["industry_growth"]] * len(self.result.years)
        return vals

    def _auto_peers(self) -> None:
        """用 Yahoo 自動抓同業倍數（依同業推薦 ＋ 目前選取的市場）。"""
        app = self.app
        sym = str((app.meta or {}).get("ticker") or "").strip()
        suffix = sym.split(".")[-1].upper() if "." in sym else ""
        if not sym or suffix not in ("HK", "SS", "SZ"):
            messagebox.showinfo(
                "無法自動抓取同業",
                "目前載入的財報沒有對應的上市代號（示範資料）。\n\n"
                "請先到「① DCF 股價估值」第 1 頁用自動查詢載入一家真實上市公司，"
                "再回來按此按鈕。",
                parent=app.root)
            return
        markets = list(self.state.get("markets") or ["HK"])
        self.peer_lbl.config(text=f"正在抓取 {sym} 的同業倍數…", fg="#7A868F")

        def work() -> None:
            try:
                rows = app.feed.peer_rows(sym, markets)
                err = None
            except Exception as exc:
                rows, err = None, exc
            app._q.put((self._on_auto_peers, (sym, rows, err)))

        threading.Thread(target=work, daemon=True).start()

    def _on_auto_peers(self, sym: str, rows: Optional[List[Dict[str, Any]]], err: Any) -> None:
        if err is not None or not rows:
            msg = getattr(err, "message", str(err)) if err else "找不到可用的同業資料"
            self.peer_lbl.config(text=f"✗ 同業抓取失敗：{msg}", fg=C_ERR)
            return
        self.peer_rows = rows
        self.peer_lbl.config(
            text=f"🌐 Yahoo 即時同業（{sym} 相似公司）：{len(rows)} 家，可再手動編輯",
            fg=C_OK)
        self.refresh()

    def _edit_peers(self) -> None:
        """簡易樣本池編輯器：文字區每行一筆「名稱,市場,PE,PB,EV_EBITDA,EV_Sales」。"""
        win = tk.Toplevel(self.frame)
        win.title("編輯可比樣本池（每行：名稱,市場(HK/MO/A),PE,PB,EV_EBITDA,EV_Sales）")
        win.geometry("760x460")
        txt = tk.Text(win, font=("Consolas", 10))
        txt.pack(fill="both", expand=True, padx=10, pady=8)
        rows = self.peer_rows if self.peer_rows is not None else peer_lib.select(
            (self.app.meta or {}).get("industry") or "", self.state["markets"])
        for p in rows:
            vals = [p["name"], p["market"], p.get("pe"), p.get("pb"),
                    p.get("ev_ebitda"), p.get("ev_sales")]
            txt.insert("end", ",".join("" if v is None else str(v) for v in vals) + "\n")

        def ok():
            out = []
            for line in txt.get("1.0", "end").splitlines():
                parts = [x.strip() for x in line.split(",")]
                if len(parts) < 6 or not parts[0]:
                    continue
                def num(i):
                    try:
                        return float(parts[i])
                    except (ValueError, IndexError):
                        return None
                out.append({"name": parts[0], "market": parts[1] or "HK",
                            "industry": "", "pe": num(2), "pb": num(3),
                            "ev_ebitda": num(4), "ev_sales": num(5),
                            "ebit_margin": None, "growth": None})
            if len(out) < 3:
                messagebox.showerror("樣本不足", "至少需要 3 筆有效樣本。",
                                     parent=win)
                return
            self.peer_rows = out
            self.peer_lbl.config(text=f"已使用手動樣本池（{len(out)} 家）", fg=C_LOCK)
            win.destroy()
            self.refresh()

        tk.Button(win, text="套用", font=FONT_BOLD, command=ok, bg=C_ACCENT,
                  fg="white", relief="flat", padx=14).pack(pady=6)

    def _import_peers(self) -> None:
        p = filedialog.askopenfilename(title="選擇可比樣本 CSV",
                                       filetypes=[("CSV", "*.csv"), ("所有檔案", "*.*")])
        if not p:
            return
        try:
            import csv as _csv
            out = []
            with open(p, encoding="utf-8-sig") as fh:
                for row in _csv.DictReader(fh):
                    def num(key):
                        try:
                            return float(row.get(key) or "")
                        except ValueError:
                            return None
                    out.append({"name": row.get("名稱") or row.get("name") or "",
                                "market": (row.get("市場") or row.get("market") or "HK"),
                                "industry": row.get("行業") or "",
                                "pe": num("PE"), "pb": num("PB"),
                                "ev_ebitda": num("EV_EBITDA"),
                                "ev_sales": num("EV_Sales"),
                                "ebit_margin": num("EBIT_MARGIN"),
                                "growth": num("GROWTH")})
            if len(out) < 3:
                raise ValueError("樣本少於 3 筆")
            self.peer_rows = out
            self.peer_lbl.config(text=f"已匯入手動樣本池（{len(out)} 家）", fg=C_LOCK)
            self.refresh()
        except Exception as e:
            messagebox.showerror("匯入失敗", str(e), parent=self.frame)

    # ------------------------------------------------------------ 卡片 5
    def _build_card5(self) -> None:
        c = self.card5
        self.conflict_lbl = tk.Label(c.body, text="", font=FONT, bg="#FFF8E6",
                                     fg="#8A5A00", wraplength=920, justify="left")
        self.conflict_lbl.pack(anchor="w", fill="x", pady=(0, 4))
        self.conflict_detail = tk.Label(c.body, text="", font=FONT_SMALL, bg=C_BG,
                                        fg="#495057", wraplength=920, justify="left")
        self.conflict_detail.pack(anchor="w", fill="x")
        self.tax_lbl = tk.Label(c.body, text="", font=FONT_SMALL, bg="#E6FCF5",
                                fg="#0B7285", wraplength=920, justify="left")
        self.tax_lbl.pack(anchor="w", fill="x", pady=(6, 0))
        self.summary_text = tk.Text(c.body, height=10, font=FONT, wrap="word",
                                    bg="white", relief="flat")
        self.summary_text.pack(fill="x", pady=4)
        self.calib_lbl = tk.Label(c.body, text="", font=FONT_SMALL, bg=C_BG,
                                  fg="#5F6C73", wraplength=920, justify="left")
        self.calib_lbl.pack(anchor="w", fill="x", pady=(0, 4))
        exp = tk.Frame(c.body, bg=C_BG)
        exp.pack(fill="x", pady=4)
        tk.Button(exp, text="💾 匯出 JSON（全部參數＋溯源＋標籤）", font=FONT,
                  command=lambda: self._export("json"), bg="#E8EDF2",
                  relief="groove", padx=10).pack(side="left")
        tk.Button(exp, text="📄 匯出全套四合一報告（HTML）", font=FONT,
                  command=lambda: self._export("all"), bg="#E8EDF2",
                  relief="groove", padx=10).pack(side="left", padx=(8, 0))
        self.mod_choice = ttk.Combobox(exp, width=16, state="readonly", values=(
            "僅①杜邦", "僅②信用風險", "僅③DDM", "僅④可比估值"))
        self.mod_choice.current(0)
        self.mod_choice.pack(side="left", padx=(16, 4))
        tk.Button(exp, text="匯出單一模組報告", font=FONT,
                  command=lambda: self._export("module"), bg="#E8EDF2",
                  relief="groove", padx=10).pack(side="left")
        self.attach_dcf = tk.BooleanVar(value=True)
        tk.Checkbutton(exp, text="附 DCF 報告作驗證附錄", variable=self.attach_dcf,
                       font=FONT_SMALL, bg=C_BG).pack(side="left", padx=(10, 0))
        exp2 = tk.Frame(c.body, bg=C_BG)
        exp2.pack(fill="x", pady=(0, 4))
        tk.Button(exp2, text="📤 匯出 NPV 參數包（歷史成長／EBIT 利潤率／稅制／折現率 → JSON）",
                  font=FONT_BOLD, command=self._export_npv_bundle, bg="#3B5BDB",
                  fg="white", relief="flat", padx=10).pack(side="left")
        tk.Label(exp2, text="供「NPV 快速估算」等工具匯入的預設參數；不會自動覆寫任何工具。",
                 font=FONT_SMALL, bg=C_BG, fg="#7A868F").pack(side="left", padx=(10, 0))

    def _export(self, what: str) -> None:
        if self.result is None:
            messagebox.showinfo("尚無結果", "請先按「▶ 執行診斷」。", parent=self.frame)
            return
        dcf_html = self._latest_dcf_html() if self.attach_dcf.get() else None
        if what == "json":
            p = diag_export.save_json(self.result)
        elif what == "all":
            p = diag_export.save_html(self.result, dcf_html_path=dcf_html)
        else:
            mods = {"僅①杜邦": ["dupont"], "僅②信用風險": ["credit"],
                    "僅③DDM": ["ddm"], "僅④可比估值": ["comps"]}[self.mod_choice.get()]
            p = diag_export.save_html(self.result, modules=mods, dcf_html_path=dcf_html)
        if messagebox.askyesno("匯出完成", f"已輸出：\n{p}\n\n要立即開啟嗎？",
                               parent=self.frame):
            os.startfile(p)

    def _export_npv_bundle(self) -> None:
        if self.result is None:
            messagebox.showinfo("尚無結果", "請先按「▶ 執行診斷」。", parent=self.frame)
            return
        try:
            stt = (self.app.dcf_snapshot() or {}).get("state") or {}
            p = diag_export.save_npv_bundle(self.result, dcf_state=stt)
        except Exception as e:
            messagebox.showerror("匯出失敗", str(e), parent=self.frame)
            return
        if messagebox.askyesno("匯出完成", f"已輸出 NPV 參數包：\n{p}\n\n要立即開啟嗎？",
                               parent=self.frame):
            os.startfile(p)

    def _latest_dcf_html(self) -> Optional[str]:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
        if not os.path.isdir(out_dir):
            return None
        cands = sorted((f for f in os.listdir(out_dir)
                        if f.startswith("DCF估值") and f.endswith(".html")),
                       key=lambda f: os.path.getmtime(os.path.join(out_dir, f)))
        return os.path.join(out_dir, cands[-1]) if cands else None

    def _check_company_switch(self) -> None:
        key = (self.app.meta or {}).get("id") or (self.app.meta or {}).get("name") or ""
        if key != getattr(self, "_company_key", None):
            self._company_key = key
            self.state["overrides"] = {}
            self.peer_rows = None
            self.ke_source.config(text="宏觀模型／預設", fg="#7A868F")
            self.peer_lbl.config(text="")
            for e in self.ddm_vars.values():
                e.delete(0, "end")

    # ============================================================ 計算與渲染
    def _make_diag(self) -> diagnostics.Diagnostics:
        app = self.app
        diag = diagnostics.Diagnostics(app.parsed, app.meta)
        if self.sim_fail.get():
            diag.fetch_macro(simulate_failure=True)   # 觸發降級路徑
        return diag

    def _dcf_param(self) -> Optional[Dict[str, Any]]:
        snap = self.app.dcf_snapshot()
        r = snap.get("result")
        if r is None:
            return None
        s = r.summary
        stt = snap.get("state") or {}
        return {"intrinsic_price": s.get("intrinsic_price"),
                "growth": stt.get("growth"), "margin": stt.get("margin"),
                "wacc": stt.get("wacc"), "g": stt.get("g")}

    def refresh(self) -> None:
        app = self.app
        self._check_company_switch()
        if app.base is None or app.parsed is None:
            self.lbl_company.config(
                text="尚未載入財報——請先在「① DCF 股價估值」頁籤載入（兩頁共用同一份財報）",
                fg="#5F6C73")
            self.summary_text.config(state="normal")
            self.summary_text.delete("1.0", "end")
            self.summary_text.insert("end", "（載入財報後自動執行診斷）")
            return
        name = (app.meta or {}).get("name") or app.var("company_name").get()
        self.lbl_company.config(
            text=f"目前公司：{name}　（與 DCF 頁籤共用同一份財報；稅制預設澳門所得補充稅）",
            fg=C_HEADER)
        try:
            diag = self._make_diag()
            dcf_ctx = None
            if getattr(app, "base", None) is not None and getattr(app, "state", None):
                dcf_ctx = {"state": app.state, "base": app.base, "meta": app.meta}
            self.result = diag.run_all(self.state, self._dcf_param(),
                                       peers=self.peer_rows, dcf_ctx=dcf_ctx)
        except Exception as e:
            messagebox.showerror("診斷失敗", str(e), parent=self.frame)
            return
        self._auto_apply_defaults()
        self._render()

    def _auto_apply_defaults(self) -> None:
        """自動把歷史營收成長／EBIT 利潤率傳入 DCF（僅使用者未手動修改的欄位）。

        DDM 的 g 已在引擎內直接以歷史營收成長為上限參考；DCF 透過 push_suggestions
        寫入未鎖定欄位；NPV 走「匯出 NPV 參數包」。每家公司只自動套用一次。
        """
        app = self.app
        sug = self.result.suggestions or {}
        key = (app.meta or {}).get("id") or (app.meta or {}).get("name") or ""
        if getattr(self, "_auto_applied_for", None) == key:
            return
        self._auto_applied_for = key
        years = int(app.state.get("years") or 5)
        values = {}
        if sug.get("revenue_cagr") is not None:
            values["growth"] = [float(sug["revenue_cagr"])] * years
        if sug.get("ebit_margin") is not None:
            values["margin"] = [float(sug["ebit_margin"])] * years
        if not values:
            self.auto_note.config(text="")
            return
        applied, skipped = app.push_suggestions(values)
        parts = []
        if "growth" in applied:
            parts.append(f"營收成長 {sug['revenue_cagr'] * 100:.1f}%")
        if "margin" in applied:
            parts.append(f"EBIT 利潤率 {sug['ebit_margin'] * 100:.1f}%")
        note = "✓ 已自動把歷史參數寫入 DCF 預設：" + "、".join(parts) + \
               "（DDM 的 g 亦以歷史營收成長為上限；NPV 請用下方「匯出 NPV 參數包」）。"
        if skipped:
            note += "　🔒 跳過你手動修改過的欄位：" + "、".join(skipped)
        self.auto_note.config(text=note)

    def _render(self) -> None:
        r = self.result
        # --- 卡片 1
        d = r.dupont
        rev = d.get("rev") or {}
        self.dupont_kpis["rev_cagr"].config(text=diag_export._p(rev.get("cagr")))
        self.dupont_kpis["rev_trend"].config(text=diag_export._p(rev.get("trend_growth")))
        self.dupont_kpis["ebit_margin"].config(text=diag_export._p(d.get("hist_ebit_margin")))
        self.dupont_kpis["roe"].config(text=diag_export._p((d.get("avg") or {}).get("roe")))
        self.dupont_kpis["rev_yoy"].config(text=diag_export._p(rev.get("avg_yoy")))
        for iid in self.rev_table.get_children():
            self.rev_table.delete(*self.rev_table.get_children())
        for row in rev.get("rows") or []:
            self.rev_table.insert("", "end", values=(
                row["year"],
                f"{row['revenue']:,.0f}" if row["revenue"] is not None else "—",
                f"{row['yoy'] * 100:+.1f}%" if row["yoy"] is not None else "—",
                f"{row['index']:.1f}" if row["index"] is not None else "—"))
        for iid in self.ebit_table.get_children():
            self.ebit_table.delete(*self.ebit_table.get_children())
        for row in d.get("ebit", {}).get("rows") or []:
            self.ebit_table.insert("", "end", values=(
                row["year"],
                f"{row['ebit']:,.0f}" if row["ebit"] is not None else "—",
                f"{row['margin'] * 100:.1f}%" if row["margin"] is not None else "—",
                f"{row['industry_margin'] * 100:.1f}%" if row["industry_margin"] is not None else "—",
                f"{row['gap'] * 100:+.1f}pp" if row["gap"] is not None else "—"))
        for iid in self.dupont_table.get_children():
            self.dupont_table.delete(*self.dupont_table.get_children())
        for row in d["rows"]:
            def f(x, fmt="{:.1f}%", plus=False):
                if x is None:
                    return "—"
                s = fmt.format(x * 100) if "%" in fmt else fmt.format(x)
                return ("+" + s) if (plus and x > 0) else s
            self.dupont_table.insert("", "end", values=(
                row["year"],
                f(row["ebit_margin"]),
                f"{row['interest_burden']:.2f}" + ("†" if not row.get("interest_clean", True)
                                                   else "") if row["interest_burden"] is not None else "—",
                f"{row['tax_burden']:.2f}" if row["tax_burden"] is not None else "—",
                f(row["net_margin"]),
                f"{row['asset_turnover']:.2f}" if row["asset_turnover"] is not None else "—",
                f"{row['equity_multiplier']:.2f}" if row["equity_multiplier"] is not None else "—",
                f"{row['roe'] * 100:.2f}%" if row["roe"] is not None else "—"))
        fn = []
        if any(not x.get("interest_clean", True) for x in d["rows"]):
            fn.append("† 該年度稅前溢利高於 EBIT（報表 EBIT 不含處置收益等非營業項目），"
                      "「利息負擔」>1 應理解為 EBIT→稅前的轉換係數，非利息侵蝕程度。")
        if rev.get("trend_r2") is not None:
            fn.append(f"營收對數趨勢線 R² {rev['trend_r2']:.2f}；指數以首個有營收年度 = 100。")
        self.dupont_footnote.config(text="　".join(fn))
        # 三情景橋接表
        for iid in self.scen_table.get_children():
            self.scen_table.delete(*self.scen_table.get_children())
        for s in r.scenarios:
            self.scen_table.insert("", "end", values=(
                s["label"],
                f"{s['growth'] * 100:+.1f}pp",
                f"{s['ebit_margin'] * 100:.1f}%" if s.get("ebit_margin") is not None else "—",
                f"{s['ke'] * 100:+.1f}pp" if s["ke"] else "基準",
                f"{s['roe'] * 100:.2f}%" if s.get("roe") is not None else "—",
                f"{s['z']:.2f}" if s.get("z") is not None else "—",
                f"{s['ddm_price']:,.2f}" if s.get("ddm_price") else
                ("n/a" if not s.get("ddm_valid") else "—"),
                f"{s['comps_price']:,.2f}" if s.get("comps_price") else "—",
                f"{s['dcf_price']:,.2f}" if s.get("dcf_price") else
                (f"EV {s['dcf_ev']:,.0f}" if s.get("dcf_ev") else "n/a")))
        note = []
        if r.dcf_scenarios.get("normalised"):
            note.append("DCF 情景：" + r.dcf_scenarios.get("normalise_note", ""))
        if r.dcf_scenarios.get("reason"):
            note.append(r.dcf_scenarios["reason"])
        note.append("偏移同時套用到：杜邦（EBIT 利潤率／週轉）、Z''（EBIT）、DDM（Ke／g）、"
                    "可比（倍數彈性）、DCF（成長／利潤率／折現率）。")
        self.scen_note.config(text="　".join(note))
        self.driver_lbl.config(
            text=f"ROE 驅動來源：{r.dupont['driver']['title']}\n{r.dupont['driver']['text']}")
        self._tags(self.tags1, d.get("tags"))
        # --- 卡片 2
        for iid in self.credit_table.get_children():
            self.credit_table.delete(*self.credit_table.get_children())
        for row in r.credit["rows"]:
            self.credit_table.insert("", "end", values=(
                row["year"], f"{row['z']:.2f}" if row["z"] is not None else "—",
                {"safe": "安全區", "grey": "灰色區", "distress": "危險區"}.get(row["zone"], "—"),
                {"safe": "安全區", "grey": "灰色區", "distress": "危險區"}.get(row.get("zone_adj", ""), "—"),
                f"{row['current_ratio']:.2f}" if row["current_ratio"] is not None else "—",
                f"{row['quick_ratio']:.2f}" if row["quick_ratio"] is not None else "—",
                f"{row['debt_ratio'] * 100:.1f}%" if row["debt_ratio"] is not None else "—",
                f"{row['interest_coverage']:.2f}" if row["interest_coverage"] is not None else "—"))
        self._tags(self.tags2, r.credit.get("tags"))
        rule = r.credit.get("industry_rule")
        if rule:
            self.ind_note.config(
                text=f"🛩 行業備註（{rule['label']}）：{rule['note']}\n"
                     f"重點追蹤：{rule['watch']}")
        else:
            self.ind_note.config(text="分級門檻沿用 Altman 原始 Z''（非製造業）標準；"
                                      "若屬航空／設備租賃等高槓桿行業，載入後會自動套用行業調整門檻。")
        # --- 卡片 3
        d = r.ddm
        for key, var in (("dps", "dps"), ("ke", "ke"), ("g", "g")):
            v = d.get(var if var != "dps" else "dps")
            if v is not None and not self.ddm_vars[key].get():
                self.ddm_vars[key].insert(0, f"{v:.2f}" if key == "dps" else f"{v * 100:.2f}")
        if d.get("disabled"):
            self.ddm_note.config(text="⚠ " + d.get("reason", ""), fg="#F08C00")
            self.ddm_kpis["price"].config(text="不適用")
        else:
            self.ddm_note.config(text="")
            self.ddm_kpis["price"].config(
                text=f"{r.currency} {d['price']:,.2f}" if d.get("price") else "—")
        self.ddm_kpis["div_yield"].config(text=diag_export._p(d.get("div_yield")))
        self.ddm_kpis["fcf_yield"].config(text=diag_export._p(d.get("fcf_yield")))
        self.ddm_kpis["risk_free"].config(text=diag_export._p(d.get("risk_free")))
        for iid in self.ddm_table.get_children():
            self.ddm_table.delete(*self.ddm_table.get_children())
        for s in d.get("scenarios") or []:
            self.ddm_table.insert("", "end", values=(
                s["label"], f"{s['ke'] * 100:.2f}%", f"{s['g'] * 100:.2f}%",
                f"{s['price']:,.2f}" if s.get("valid") else "n/a",
                f"{s['upside'] * 100:+.1f}%" if s.get("upside") is not None else "—"))
        self._tags(self.tags3, d.get("tags"))
        # --- 卡片 4
        c = r.comps
        for iid in self.comps_table.get_children():
            self.comps_table.delete(*self.comps_table.get_children())
        for row in c.get("rows") or []:
            self.comps_table.insert("", "end", values=(
                row["label"],
                f"{row['target']:.2f}" if row["target"] else "—",
                f"{row['median']:.2f}" if row["median"] else "—",
                f"{row['mean']:.2f}" if row["mean"] else "—",
                f"{row['q1']:.2f}–{row['q3']:.2f}" if row["q1"] else "—",
                f"{row['implied_price']:,.2f}" if row["valid"] else "隱藏",
                f"{row['premium'] * 100:+.1f}%" if row["premium"] is not None else "—"))
        self.verdict_lbl.config(text=c.get("verdict_text") or "")
        self.peer_lbl.config(text=self.peer_lbl.cget("text") or
                             f"自動樣本池（{c.get('n_peers')} 家）")
        for iid in self.comps_table2.get_children():
            self.comps_table2.delete(*self.comps_table2.get_children())
        for s in c.get("scenarios") or []:
            self.comps_table2.insert("", "end", values=(
                s["label"], f"{s['price']:,.2f}",
                f"{s['upside'] * 100:+.1f}%" if s.get("upside") is not None else "—"))
        self._tags(self.tags4, c.get("tags"))
        # --- 卡片 5
        s = r.summary
        conf = r.conflict or {}
        if conf.get("text"):
            self.conflict_lbl.config(
                text=("⚠ 相對估值 vs DDM：" if conf.get("conflict") else "• 相對估值 vs DDM：")
                + conf["text"])
            detail = "\n".join("— " + c for c in conf.get("causes") or [])
            if conf.get("advice"):
                detail += ("\n▶ 建議：" + conf["advice"]) if conf.get("conflict") \
                    else ("\n▶ " + conf["advice"])
            self.conflict_detail.config(text=detail)
        else:
            self.conflict_lbl.config(text="相對估值 vs DDM：缺少市價或結果，無法交叉判斷。")
            self.conflict_detail.config(text="")
        tax = r.tax or {}
        self.tax_lbl.config(
            text=f"🧾 稅制：{tax.get('label')}｜法定邊際稅率 {diag_export._p(tax.get('statutory_rate'))}"
                 f"｜報表隱含有效稅率 {diag_export._p(tax.get('effective_rate_reported'))}（僅對照）"
                 f"｜所有稅額呼叫 dcfcore.tax，不另寫稅務邏輯")
        self.summary_text.config(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("end", s.get("text") or "")
        calib = []
        dcf = s.get("cross") or {}
        if dcf.get("dcf_price"):
            calib.append("→ DCF 對照：上方已比較 DCF 內在價與可比反推；以下為假設合理性檢查。")
        sug = r.suggestions
        if sug.get("revenue_cagr") is not None:
            calib.append(f"歷史營收 CAGR {sug['revenue_cagr'] * 100:.1f}%"
                         f"｜行業成長預期中位數 "
                         f"{sug['industry_growth'] * 100:.1f}%" if sug.get("industry_growth") is not None else "")
        if sug.get("hist_ebit_margin") is not None:
            calib.append(f"歷史 EBIT 利潤率 {sug['hist_ebit_margin'] * 100:.1f}%"
                         f"｜行業中位數 {sug['industry_margin'] * 100:.1f}%"
                         if sug.get("industry_margin") is not None else "")
        dcfst = (self.app.dcf_snapshot().get("state") or {})
        if dcfst.get("growth") and sug.get("revenue_cagr") is not None:
            g0 = dcfst["growth"][0]
            rel = "偏高" if g0 > sug["revenue_cagr"] * 1.2 else (
                "偏低" if g0 < sug["revenue_cagr"] * 0.8 else "接近")
            calib.append(f"DCF 首年營收增長 {g0 * 100:.1f}% vs 歷史 CAGR "
                         f"{sug['revenue_cagr'] * 100:.1f}%：{rel}"
                         + ("（僅標示，未修改 DCF）" if rel != "接近" else "（僅標示，未修改 DCF）"))
        if dcfst.get("margin") and sug.get("hist_ebit_margin") is not None:
            m0 = dcfst["margin"][0]
            rel = "偏高" if m0 > sug["hist_ebit_margin"] * 1.2 else (
                "偏低" if m0 < sug["hist_ebit_margin"] * 0.8 else "接近")
            calib.append(f"DCF 首年 EBIT 利潤率 {m0 * 100:.1f}% vs 歷史 "
                         f"{sug['hist_ebit_margin'] * 100:.1f}%：{rel}（僅標示，未修改 DCF）")
        if r.notes:
            calib.append("資料限制：" + "；".join(r.notes[:4]))
        self.calib_lbl.config(text="\n".join(x for x in calib if x))
        # --- 圖表
        for kind in self.figures:
            diag_charts.draw(self.figures[kind], kind, r)
            self.canvases[kind].draw()
