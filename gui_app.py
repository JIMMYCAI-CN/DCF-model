"""
gui_app.py — DCF 股價估算器（FCFF）六步精靈（Tkinter 桌面版）
================================================================
獨立程式：只做「企業 DCF → 預計股價」，情景／敏感性／風險標籤僅為輔助。
與專案 NPV 工具完全分離，不共用任何專案現金流口徑。

六步：
  1 公司與財報     — ①自動查詢（輸入代號／公司名，Yahoo 即時抓取，免匯入檔案）
                      ②示範財報或自備 CSV／JSON（備用）；幣別／單位／稅制
  2 基期資料       — 自動帶入，任何欄位可覆寫（覆寫即鎖定🔒）
  3 預測設定       — 高速增長年數、逐年成長率／EBIT 利潤率、永續增長率 g
  4 折現率 WACC    — 手動輸入或由宏觀模型取得（失敗可降級，不阻斷）
  5 情景與敏感性   — 三情景偏移、熱圖範圍（輔助，可全部用預設）
  6 執行估值與結果 — 預計股價、風險標籤、四張輔助圖、匯出

啟動：  python gui_app.py    （需 Python 3.9+ 與 matplotlib；本機用 py 亦可）
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dcfcore import dcf, macro_analyzer, statements as st           # noqa: E402
from dcfcore.dcf import DCFError                                    # noqa: E402

import mpl_charts                                                   # noqa: E402
import exporter                                                     # noqa: E402
import fetcher                                                      # noqa: E402

import matplotlib                                                   # noqa: E402
matplotlib.use("TkAgg")
from matplotlib.figure import Figure                                # noqa: E402
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg     # noqa: E402

# ---------------------------------------------------------------- 樣式
C_HEADER = "#1F5C5C"
C_BG = "#F4F6F7"
C_ACCENT = "#3B5BDB"
C_ERR = "#C92A2A"
C_OK = "#0CA678"
C_LOCK = "#E8A317"

FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 15, "bold")
FONT_BIG = ("Microsoft YaHei UI", 26, "bold")

PAGES = ["公司與財報", "基期資料（自動帶入・可覆寫）", "預測設定",
         "折現率 WACC", "情景與敏感性（輔助）", "執行估值與結果"]

SAMPLES = [("恆晉科技集團（香港・五年）", "hk_tech"),
           ("濠景文旅（澳門・兩年）", "mo_resort"),
           ("金悦餐飲（香港・四年）", "hk_food")]

BASE_FIELDS = [  # (key, 標籤, 說明)
    ("revenue", "營業收入", "損益表首行"),
    ("ebit", "EBIT（營業利潤）", "損益表"),
    ("da", "折舊攤銷", "現金流量表"),
    ("capex", "資本開支", "現金流量表投資活動"),
    ("nwc", "營運資金", "應收＋存貨−應付"),
    ("net_debt", "淨負債", "計息負債＋非控股權益−現金"),
    ("shares", "發行股數（百萬股等）", "備忘"),
    ("price", "現價", "備忘"),
]


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).strip().replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


class App:
    """六步精靈主視窗（DCF 專用）。"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.v: Dict[str, tk.StringVar] = {}
        self.year_cells: Dict[str, List[tk.Entry]] = {}   # "growth"/"margin" → entries
        self.base_entries: Dict[str, tk.Entry] = {}
        self.base_ratio_labels: Dict[str, tk.Label] = {}
        self.lock_buttons: Dict[str, tk.Button] = {}
        self.figures: Dict[str, Figure] = {}
        self.canvases: Dict[str, FigureCanvasTkAgg] = {}
        self._built_year_grid_n: int = -1

        self.parsed = None        # ParsedStatements
        self.base = None          # BaseExtract
        self.meta: Dict[str, Any] = {}
        self.state: Optional[Dict[str, Any]] = None
        self.macro_snapshot = None
        self.result = None
        self.user_edited: set = set()   # 使用者手動改過的 DCF 假設（跨頁籤保護）
        self.feed = fetcher.YahooFeed()  # 自動抓取（免匯入檔案）
        self._q: "queue.Queue" = queue.Queue()   # 背景執行緒 → 主執行緒的結果佇列

        root.title("DCF 股價估算器 ＋ 企業診斷 — 香港／澳門・IFRS")
        root.geometry("1080x780")
        root.minsize(1000, 700)
        root.configure(bg=C_BG)

        self._build_header()
        # 兩個頁籤：① DCF 股價估值（原本六步精靈）② 企業診斷（四合一參考校準）
        self.nb = ttk.Notebook(self.root)
        self.tab_dcf = tk.Frame(self.nb, bg=C_BG)
        self.nb.add(self.tab_dcf, text=" ① DCF 股價估值 ")
        self._build_footer(self.tab_dcf)
        self._build_body(self.tab_dcf)
        self._build_pages()

        import diag_tab
        self.diag = diag_tab.DiagnosticsTab(self, self.nb)

        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_change)
        self.show_page(0)
        self.root.after(120, self._drain_queue)

    def _drain_queue(self) -> None:
        """在主執行緒取出背景工作的結果並更新 UI（避免跨執行緒呼叫 Tk）。"""
        try:
            while True:
                fn, args = self._q.get_nowait()
                try:
                    fn(*args)
                except Exception as exc:          # 單一工作出錯不影響後續
                    print("背景工作回呼失敗：", exc)
        except queue.Empty:
            pass
        try:
            self.root.after(120, self._drain_queue)
        except Exception:
            pass                               # 視窗已關閉

    def _on_tab_change(self, _e=None) -> None:
        if self.nb.index(self.nb.select()) == 1:
            self.step_label.config(text="企業診斷｜四合一配套模組（參考校準，不覆寫 DCF 假設）")
            self.diag.refresh()

    # ================================================================ 骨架
    def _build_header(self) -> None:
        head = tk.Frame(self.root, bg=C_HEADER)
        head.pack(fill="x")
        tk.Label(head, text="DCF 股價估算器（FCFF）— 澳門所得補充稅／香港利得稅",
                 font=FONT_TITLE, bg=C_HEADER, fg="white"
                 ).pack(anchor="w", padx=18, pady=(10, 0))
        self.step_label = tk.Label(head, text="", font=FONT, bg=C_HEADER, fg="#CFE8E8")
        self.step_label.pack(anchor="w", padx=18, pady=(0, 10))

    def _build_footer(self, parent: Optional[tk.Frame] = None) -> None:
        bar = tk.Frame(parent or self.root, bg=C_BG)
        bar.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        tk.Button(bar, text="載入示範財報", font=FONT, command=self.load_sample,
                  bg="#E8EDF2", relief="groove", padx=10).pack(side="left")
        tk.Button(bar, text="📂 載入自備財報（CSV／JSON）", font=FONT,
                  command=self.load_own_file, bg="#E8EDF2", relief="groove",
                  padx=10).pack(side="left", padx=(6, 0))
        self.btn_back = tk.Button(bar, text="◀ 上一步", font=FONT_BOLD, state="disabled",
                                  command=lambda: self.show_page(self.page_idx - 1),
                                  bg="white", relief="groove", padx=16, pady=3)
        self.btn_back.pack(side="left", padx=(16, 8))
        self.btn_next = tk.Button(bar, text="下一步 ▶", font=FONT_BOLD,
                                  command=lambda: self.show_page(self.page_idx + 1),
                                  bg=C_ACCENT, fg="white", relief="flat", padx=18, pady=3)
        self.btn_next.pack(side="left")
        self.page_label = tk.Label(bar, text="", font=FONT, bg=C_BG, fg="#7A868F")
        self.page_label.pack(side="left", padx=14)
        tk.Button(bar, text="▶ 執行估值", font=FONT_BOLD, command=self.run_valuation,
                  bg="#0E7C7B", fg="white", relief="flat", padx=20, pady=3
                  ).pack(side="right")

    def _build_body(self, parent: Optional[tk.Frame] = None) -> None:
        self.container = tk.Frame(parent or self.root, bg=C_BG)
        self.container.pack(fill="both", expand=True, padx=14, pady=10)

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

    def _build_pages(self) -> None:
        builders = [self._page_company, self._page_base, self._page_forecast,
                    self._page_wacc, self._page_sens, self._page_result]
        self.pages: List[tk.Frame] = []
        for b in builders:
            wrapper = tk.Frame(self.container, bg=C_BG)
            inner = self._scrollable(wrapper)
            b(inner)
            self.pages.append(wrapper)

    def show_page(self, idx: int) -> None:
        idx = max(0, min(idx, len(self.pages) - 1))
        self.page_idx = idx
        for p in self.pages:
            p.pack_forget()
        self.pages[idx].pack(fill="both", expand=True)
        self.step_label.config(text=f"步驟 {idx + 1}／{len(self.pages)}　{PAGES[idx]}")
        self.page_label.config(text=f"第 {idx + 1} 頁，共 {len(self.pages)} 頁")
        self.btn_back.config(state="normal" if idx > 0 else "disabled")
        self.btn_next.config(state="normal" if idx < len(self.pages) - 1 else "disabled")
        if idx == 5:
            self.run_valuation()
        if idx == 1:
            self.refresh_base_entries(respect_locks=False)

    # ================================================================ 共用
    def var(self, key: str, init: str = "") -> tk.StringVar:
        if key not in self.v:
            self.v[key] = tk.StringVar(value=init)
        return self.v[key]

    def _section(self, parent: tk.Frame, title: str) -> tk.Frame:
        lf = tk.LabelFrame(parent, text=" " + title + " ", font=FONT_BOLD,
                           bg=C_BG, fg=C_HEADER, bd=1, relief="groove")
        lf.pack(fill="x", pady=(8, 4))
        inner = tk.Frame(lf, bg=C_BG)
        inner.pack(anchor="w", padx=14, pady=8)
        return inner

    def _entry_row(self, parent: tk.Frame, r: int, label: str, key: str,
                   init: str = "", width: int = 16, suffix: str = "",
                   state: str = "normal") -> tk.Entry:
        tk.Label(parent, text=label, font=FONT, bg=C_BG).grid(
            row=r, column=0, sticky="e", pady=2, padx=(0, 6))
        e = tk.Entry(parent, textvariable=self.var(key, init), width=width,
                     font=FONT, justify="right", state=state)
        e.grid(row=r, column=1, pady=2)
        if suffix:
            tk.Label(parent, text=suffix, font=FONT_SMALL, bg=C_BG, fg="#7A868F"
                     ).grid(row=r, column=2, sticky="w", padx=4)
        return e

    def _hint(self, parent: tk.Frame, text: str) -> tk.Label:
        lb = tk.Label(parent, text=text, font=FONT_SMALL, bg=C_BG, fg="#7A868F",
                      justify="left", wraplength=880)
        lb.pack(anchor="w", pady=(4, 0))
        return lb

    def _num(self, key: str, name: str, lo=None, hi=None) -> float:
        raw = self.var(key).get().strip().replace(",", "")
        try:
            x = float(raw)
        except ValueError:
            raise ValueError(f"「{name}」必須是數字，目前是「{raw or '空白'}」。")
        if lo is not None and x < lo:
            raise ValueError(f"「{name}」不可小於 {lo:g}。")
        if hi is not None and x > hi:
            raise ValueError(f"「{name}」不可大於 {hi:g}。")
        return x

    def _pct(self, key: str, name: str) -> float:
        return self._num(key, name) / 100.0

    # ============================================================ 第 1 頁
    def _page_company(self, f: tk.Frame) -> None:
        sec = self._section(f, "公司基本資料")
        self._entry_row(sec, 0, "公司名稱：", "company_name", "示範公司（請載入財報）", 30)
        self._entry_row(sec, 1, "股票代號：", "ticker", "", 10)
        tk.Label(sec, text="報表幣別：", font=FONT, bg=C_BG).grid(
            row=2, column=0, sticky="e", padx=(0, 6))
        cb = ttk.Combobox(sec, textvariable=self.var("currency"), width=8,
                          values=("HKD", "MOP", "CNY"), state="readonly")
        cb.grid(row=2, column=1, sticky="w")
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_currency())
        self.var("currency").set("HKD")
        tk.Label(sec, text="報表單位：", font=FONT, bg=C_BG).grid(
            row=3, column=0, sticky="e", padx=(0, 6))
        cb2 = ttk.Combobox(sec, textvariable=self.var("unit"), width=8,
                           values=("百萬", "千", "元"), state="readonly")
        cb2.grid(row=3, column=1, sticky="w")
        self.var("unit").set("百萬")

        sec2 = self._section(f, "適用法律與稅制")
        self.jur_var = tk.StringVar(value="HK")
        rb = tk.Frame(sec2, bg=C_BG)
        rb.pack(anchor="w")
        tk.Radiobutton(rb, text="香港利得稅（HKD，兩級制）", variable=self.jur_var,
                       value="HK", command=lambda: self._on_jur(True), font=FONT,
                       bg=C_BG).pack(side="left")
        tk.Radiobutton(rb, text="澳門所得補充稅（MOP）", variable=self.jur_var,
                       value="MO", command=lambda: self._on_jur(True), font=FONT,
                       bg=C_BG).pack(side="left", padx=(18, 0))
        self._hint(sec2, "香港兩級制：首 200 萬港元 8.25%，其後 16.5%。"
                         "澳門：A 組帳目完備／B 組中小企；稅率口徑可選預算案（首 60 萬豁免，"
                         "超出 12%）或累進 3–12%，亦可自訂固定稅率。")
        self.mo_frame = tk.Frame(sec2, bg=C_BG)
        self.mo_frame.pack(anchor="w", pady=(4, 0))
        tk.Label(self.mo_frame, text="納稅人組別：", font=FONT, bg=C_BG).pack(side="left")
        self.mo_group = tk.StringVar(value="B")
        tk.Radiobutton(self.mo_frame, text="A 組", variable=self.mo_group, value="A",
                       font=FONT, bg=C_BG).pack(side="left")
        tk.Radiobutton(self.mo_frame, text="B 組", variable=self.mo_group, value="B",
                       font=FONT, bg=C_BG).pack(side="left", padx=(4, 14))
        tk.Label(self.mo_frame, text="稅率口徑：", font=FONT, bg=C_BG).pack(side="left")
        self.mo_regime = tk.StringVar(value="budget")
        tk.Radiobutton(self.mo_frame, text="預算案", variable=self.mo_regime, value="budget",
                       font=FONT, bg=C_BG).pack(side="left")
        tk.Radiobutton(self.mo_frame, text="累進 3–12%", variable=self.mo_regime,
                       value="progressive", font=FONT, bg=C_BG).pack(side="left", padx=(4, 0))
        self._on_jur()

        # ---------------- ① 自動查詢（主要入口） ----------------
        sec3 = self._section(f, "① 自動查詢財報（輸入代號或公司名，不用匯入檔案）")
        srow = tk.Frame(sec3, bg=C_BG)
        srow.pack(anchor="w", fill="x")
        tk.Label(srow, text="查詢：", font=FONT, bg=C_BG).pack(side="left")
        self.fetch_q = tk.StringVar(value="")
        qe = tk.Entry(srow, textvariable=self.fetch_q, width=30, font=FONT)
        qe.pack(side="left", padx=(0, 6))
        qe.bind("<Return>", lambda _e: self._run_search())
        self.search_btn = tk.Button(srow, text="🔍 搜尋", font=FONT,
                                    command=self._run_search, bg="#E8EDF2",
                                    relief="groove", padx=10)
        self.search_btn.pack(side="left")
        tk.Label(srow, text="　常用：", font=FONT, bg=C_BG).pack(side="left")
        self.preset_box = ttk.Combobox(srow, width=26, state="readonly",
                                       values=[n for n, _ in fetcher.PRESETS])
        self.preset_box.pack(side="left")
        self.preset_box.bind("<<ComboboxSelected>>", self._on_preset)

        self._fetch_rows: List[Dict[str, Any]] = []
        lrow = tk.Frame(sec3, bg=C_BG)
        lrow.pack(anchor="w", fill="x", pady=(6, 0))
        self.result_box = tk.Listbox(lrow, height=5, font=FONT_SMALL, width=96,
                                     activestyle="dotbox", exportselection=False)
        self.result_box.pack(side="left", fill="x", expand=True)
        sb = tk.Scrollbar(lrow, command=self.result_box.yview)
        sb.pack(side="left", fill="y")
        self.result_box.config(yscrollcommand=sb.set)
        self.result_box.bind("<Double-Button-1>", lambda _e: self._fetch_selected())
        self.result_box.bind("<Return>", lambda _e: self._fetch_selected())

        brow = tk.Frame(sec3, bg=C_BG)
        brow.pack(anchor="w", pady=(6, 0))
        self.fetch_btn = tk.Button(brow, text="⬇ 抓取並帶入", font=FONT_BOLD,
                                   command=self._fetch_selected, bg=C_ACCENT, fg="white",
                                   relief="flat", padx=14, state="disabled")
        self.fetch_btn.pack(side="left")
        self.force_var = tk.BooleanVar(value=False)
        tk.Checkbutton(brow, text="強制更新", variable=self.force_var, font=FONT_SMALL,
                       bg=C_BG).pack(side="left", padx=(10, 0))
        self.search_info = tk.Label(brow, text="輸入代號（0700、1928.HK、600519）或公司名，按 Enter 搜尋。",
                                    font=FONT_SMALL, bg=C_BG, fg="#7A868F")
        self.search_info.pack(side="left", padx=10)

        # ---------------- ② 其他來源（備用） ----------------
        sec4 = self._section(f, "② 其他來源（可選）")
        row = tk.Frame(sec4, bg=C_BG)
        row.pack(anchor="w")
        tk.Label(row, text="示範公司：", font=FONT, bg=C_BG).pack(side="left")
        self.sample_box = ttk.Combobox(row, width=26, state="readonly",
                                       values=[n for n, _ in SAMPLES])
        self.sample_box.current(0)
        self.sample_box.pack(side="left", padx=4)
        tk.Button(row, text="載入", font=FONT, command=self.load_sample,
                  bg="#E8EDF2", relief="groove", padx=12).pack(side="left", padx=4)
        tk.Button(row, text="📂 自備 CSV／JSON", font=FONT, command=self.load_own_file,
                  bg="#E8EDF2", relief="groove", padx=10).pack(side="left", padx=(10, 0))
        self.parse_info = tk.Label(sec4, text="尚未載入財報。載入後自動解析並帶入基期資料。",
                                   font=FONT_SMALL, bg=C_BG, fg="#7A868F",
                                   justify="left", wraplength=880, anchor="w")
        self.parse_info.pack(anchor="w", pady=(8, 0), fill="x")

    def _on_jur(self, set_ccy: bool = True) -> None:
        is_mo = self.jur_var.get() == "MO"
        for w in self.mo_frame.winfo_children():
            w.config(state="normal" if is_mo else "disabled")
        if set_ccy:
            # 只有使用者主動切換稅制時才連動幣別（載入財報時不可覆蓋報表幣別）
            self.var("currency").set("MOP" if is_mo else "HKD")

    def _on_currency(self) -> None:
        cur = self.var("currency").get()
        self.jur_var.set("MO" if cur == "MOP" else "HK")
        self._on_jur(set_ccy=False)

    # ------------------------------------------------------------ 財報載入
    def load_sample(self) -> None:
        cid = SAMPLES[self.sample_box.current()][1]
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "samples", f"{cid}.json")
        self._load_payload_file(path)

    def load_own_file(self) -> None:
        p = filedialog.askopenfilename(
            title="選擇財報（CSV 或 JSON）",
            filetypes=[("財報檔", "*.csv *.json"), ("所有檔案", "*.*")])
        if p:
            self._load_payload_file(p)

    # ------------------------------------------------------ 自動查詢（免匯入）
    def _on_preset(self, _e=None) -> None:
        idx = self.preset_box.current()
        if idx >= 0:
            self._fetch_symbol(fetcher.PRESETS[idx][1])

    def _run_search(self) -> None:
        q = self.fetch_q.get().strip()
        if not q:
            self.search_info.config(text="請先輸入股票代號或公司名稱。", fg=C_ERR)
            return
        self.search_btn.config(state="disabled", text="搜尋中…")
        self.search_info.config(text=f"正在搜尋「{q}」…", fg="#7A868F")

        def work() -> None:
            try:
                rows: Any = self.feed.search(q)
            except Exception as exc:                      # 網路／端點錯誤
                rows = exc
            self._q.put((self._on_search_done, (q, rows)))

        threading.Thread(target=work, daemon=True).start()

    def _on_search_done(self, q: str, rows: Any) -> None:
        self.search_btn.config(state="normal", text="🔍 搜尋")
        if isinstance(rows, Exception):
            msg = getattr(rows, "message", str(rows))
            self.search_info.config(text=f"✗ 搜尋失敗：{msg}", fg=C_ERR)
            return
        self._fetch_rows = list(rows or [])
        self.result_box.delete(0, "end")
        for r in self._fetch_rows:
            self.result_box.insert(
                "end", f'{r["symbol"]:<11s}｜{r["name"][:34]:<34s}｜{r["market_label"]}'
                       f'｜{r["industry"][:20]}')
        if not self._fetch_rows:
            self.fetch_btn.config(state="disabled")
            self.search_info.config(text="找不到相符的上市公司，請換個關鍵字或直接輸入代號。",
                                    fg=C_ERR)
            return

        self.fetch_btn.config(state="normal")
        self.result_box.selection_set(0)
        top = self._fetch_rows[0]
        want = fetcher.normalize_symbol(q).upper()
        self.search_info.config(
            text=f"找到 {len(self._fetch_rows)} 筆，雙擊任一列抓取；已預選 {top['symbol']}。",
            fg="#0CA678")
        # 代號完全相符或僅一筆 → 直接抓，省一次點擊
        if len(self._fetch_rows) == 1 or top["symbol"].upper() == want:
            self._fetch_symbol(top["symbol"])

    def _fetch_selected(self) -> None:
        sel = self.result_box.curselection()
        if not sel or not self._fetch_rows:
            self.search_info.config(text="請先搜尋並在清單中選一家公司。", fg=C_ERR)
            return
        self._fetch_symbol(self._fetch_rows[sel[0]]["symbol"])

    def _fetch_symbol(self, symbol: str) -> None:
        sym = fetcher.normalize_symbol(symbol)
        if not sym:
            return
        self.fetch_btn.config(state="disabled", text="抓取中…")
        self.search_btn.config(state="disabled")
        self.search_info.config(text=f"正在抓取 {sym} 的年度財報與現價…", fg="#7A868F")
        force = bool(self.force_var.get())      # Tk 變數只能在主執行緒讀

        def work() -> None:
            try:
                payload, info = self.feed.payload(sym, force=force)
                err = None
            except Exception as exc:
                payload, info, err = None, None, exc
            self._q.put((self._on_fetch_done, (sym, payload, info, err)))

        threading.Thread(target=work, daemon=True).start()

    def _on_fetch_done(self, sym: str, payload: Optional[Dict[str, Any]],
                       info: Optional[Dict[str, Any]], err: Any) -> None:
        self.fetch_btn.config(state="normal", text="⬇ 抓取並帶入")
        self.search_btn.config(state="normal")
        if err is not None or not payload:
            msg = getattr(err, "message", str(err)) if err else "沒有回傳資料"
            self.search_info.config(text=f"✗ 抓取失敗：{msg}", fg=C_ERR)
            messagebox.showwarning(
                "抓取失敗",
                f"{sym} 的資料抓取失敗：\n{msg}\n\n"
                "可改用下方示範財報或自備 CSV／JSON；已有快取時會自動改用快取。",
                parent=self.root)
            return

        try:
            self._apply_payload(payload, f"yahoo:{sym}")
        except Exception as exc:
            self.search_info.config(text=f"✗ 解析失敗：{exc}", fg=C_ERR)
            messagebox.showerror("解析失敗", f"{sym} 的財報無法解析：\n{exc}", parent=self.root)
            return

        info = info or {}
        src = {"network": "即時抓取", "cache": "快取（12 小時內）",
               "stale": "舊快取（即時抓取失敗）"}.get(str(info.get("source")), "即時抓取")
        years = info.get("years") or []
        span = f"{years[0]}–{years[-1]}，共 {len(years)} 年" if years else ""
        self.search_info.config(
            text=(f"✓ {info.get('name', sym)}　{src}｜{span}｜{info.get('currency', '')}"
                  f"（{info.get('unit', '')}）｜現價 {info.get('price')}"),
            fg=C_OK)
        if info.get("warnings"):
            self.parse_info.config(text="／".join(info["warnings"]) + "\n" + self.parse_info.cget("text"),
                                   fg="#F08C00")

    def _load_payload_file(self, path: str) -> None:
        try:
            if path.lower().endswith(".json"):
                with open(path, encoding="utf-8") as fh:
                    payload = json.load(fh)
            else:
                with open(path, encoding="utf-8") as fh:
                    payload = {"items": st.parse_csv_text(fh.read())}
            self._apply_payload(payload, path)
        except Exception as e:
            messagebox.showerror("載入失敗", f"無法解析財報：\n{e}", parent=self.root)

    def _apply_payload(self, payload: Dict[str, Any], path: str = "") -> None:
        parsed = st.parse_payload(payload)
        base = st.extract_base(parsed)
        meta = dict(payload.get("meta") or {})
        self.parsed, self.base, self.meta = parsed, base, meta
        self.state = dcf.default_state(base, meta)

        # 帶入第 1 頁欄位（先定稅制司法管轄，再定幣別，避免被 _on_jur 覆蓋）
        name = meta.get("name") or meta.get("company_name") or os.path.basename(path)
        self.var("company_name").set(str(name))
        self.var("ticker").set(str(meta.get("ticker") or ""))
        jur = str(meta.get("jurisdiction") or
                  ("MO" if str(base.currency).upper() == "MOP" else "HK")).upper()
        self.jur_var.set(jur)
        if base.currency:
            self.var("currency").set(base.currency.upper()[:3] or "HKD")
        if base.unit:
            self.var("unit").set(base.unit)
        self._on_jur(set_ccy=False)      # 載入財報時不可讓稅制連動覆蓋幣別

        info = (f"✓ 已載入：{name}　基期 {base.base_year} 年，歷史樣本 {base.history_years} 年\n"
                f"　科目對映：{len(parsed.routes)} 項"
                + (f"｜未對映：{', '.join(parsed.unmatched[:6])}" if parsed.unmatched else "")
                + ("\n　" + "；".join(parsed.warnings[:3]) if parsed.warnings else ""))
        self.parse_info.config(text=info, fg="#0CA678" if not parsed.unmatched else "#F08C00")

        # 基期頁即時刷新（鎖定清空＝新財報從頭來）
        self.refresh_base_entries(respect_locks=False)
        self.refresh_forecast_from_state()
        self.refresh_wacc_fields()
        self.user_edited.clear()
        if hasattr(self, "diag"):
            self.diag.refresh()

    # ============================================================ 第 2 頁
    def _page_base(self, f: tk.Frame) -> None:
        sec = self._section(f, "基期資料（由財報自動帶入；任何欄位可人工覆寫）")
        self._hint(sec, "覆寫任何欄位後自動鎖定 🔒——之後「重新抓取財報」只會刷新未鎖定的欄位，"
                        "你改過的數字絕不會被自動覆蓋。金額單位＝報表單位（見第 1 頁）。")
        grid = tk.Frame(sec, bg=C_BG)
        grid.pack(anchor="w", pady=(6, 0))
        for i, (key, label, _src) in enumerate(BASE_FIELDS):
            e = self._entry_row(grid, i, label + "：", f"base_{key}", "", 16)
            self.base_entries[key] = e
            lk = tk.Button(grid, text="🔒", font=FONT_SMALL, width=3,
                           command=lambda k=key: self.toggle_lock(k))
            lk.grid(row=i, column=3, padx=(4, 0))
            self.lock_buttons[key] = lk
            rl = tk.Label(grid, text="", font=FONT_SMALL, bg=C_BG, fg="#7A868F", width=22)
            rl.grid(row=i, column=4, sticky="w", padx=(10, 0))
            self.base_ratio_labels[key] = rl
            e.bind("<KeyRelease>", lambda _e, k=key: self._on_base_edit(k))

        act = tk.Frame(sec, bg=C_BG)
        act.pack(anchor="w", pady=(10, 0))
        tk.Button(act, text="↻ 重新抓取財報（只刷新未鎖定欄位）", font=FONT_BOLD,
                  command=lambda: self.refresh_base_entries(respect_locks=True),
                  bg="#E8EDF2", relief="groove", padx=10).pack(side="left")
        self.lock_note = tk.Label(act, text="", font=FONT_SMALL, bg=C_BG, fg=C_LOCK)
        self.lock_note.pack(side="left", padx=10)

    def _on_base_edit(self, key: str) -> None:
        """使用者改了基期欄位 → 寫入 base_override 並自動鎖定。"""
        if self.state is None:
            return
        self.user_edited.add("base." + key)
        raw = self.var(f"base_{key}").get().strip().replace(",", "")
        if raw == "":
            self.state["base_override"].pop(key, None)
            self.state["locks"].pop(key, None)
        else:
            try:
                self.state["base_override"][key] = float(raw)
                self.state["locks"][key] = True
            except ValueError:
                pass
        self._paint_locks()

    def toggle_lock(self, key: str) -> None:
        if self.state is None:
            return
        locks = self.state["locks"]
        if locks.get(key):
            locks.pop(key)
        elif key in self.state["base_override"]:
            locks[key] = True
        self._paint_locks()

    def _paint_locks(self) -> None:
        n_locked = sum(1 for v in self.state.get("locks", {}).values() if v)
        for key, btn in self.lock_buttons.items():
            locked = bool(self.state["locks"].get(key))
            btn.config(bg=C_LOCK if locked else "#E8EDF2",
                       fg="white" if locked else "#343A40",
                       text="🔒" if locked else "🔓")
        self.lock_note.config(
            text=f"已鎖定 {n_locked} 項——鎖定欄位不會被「重新抓取財報」覆蓋" if n_locked else "")

    def refresh_base_entries(self, respect_locks: bool = True) -> None:
        """用財報重新萃取基期；respect_locks=False 代表全新載入。"""
        if self.base is None or self.state is None:
            return
        if respect_locks:
            # 只更新未鎖定欄位的自動值
            auto = self.base.values
            for key in self.state["base_override"]:
                if not self.state["locks"].get(key) and key in auto:
                    self.state["base_override"][key] = auto[key]
            fresh = dcf.default_state(self.base, self.meta)
            for k in ("da_pct", "capex_pct", "nwc_pct", "tax_rate"):
                if not self.state["locks"].get(f"ratio_{k}"):
                    pass  # 比例欄位在第 3 頁，另行刷新
        else:
            self.state = dcf.default_state(self.base, self.meta)

        ov = self.state["base_override"]
        for key, _label, _src in BASE_FIELDS:
            if not respect_locks or not self.state["locks"].get(key):
                if key in self.base.values:
                    ov[key] = self.base.values[key]
            val = ov.get(key)
            self.var(f"base_{key}").set("" if val is None else f"{val:,.2f}")

        rev = ov.get("revenue") or self.base.values.get("revenue") or 0
        ratio_map = {"revenue": None, "ebit": "EBIT 率", "da": "D&A 率",
                     "capex": "CAPEX 率", "nwc": "佔營收", "net_debt": None,
                     "shares": None, "price": None}
        for key, name in ratio_map.items():
            lb = self.base_ratio_labels[key]
            if name:
                v = ov.get(key) or 0
                lb.config(text=f"{name} {v / rev * 100:.1f}%" if rev else "")
            else:
                lb.config(text="")
        self._paint_locks()

    # ============================================================ 第 3 頁
    def _page_forecast(self, f: tk.Frame) -> None:
        sec = self._section(f, "高速增長期（預設 5 年，可自訂 1–15 年）")
        row = tk.Frame(sec, bg=C_BG)
        row.pack(anchor="w")
        tk.Label(row, text="年期：", font=FONT, bg=C_BG).pack(side="left")
        self.years_var = tk.StringVar(value="5")
        sp = tk.Spinbox(row, from_=1, to=15, textvariable=self.years_var, width=5,
                        font=FONT, command=self.rebuild_year_grid)
        sp.pack(side="left", padx=6)
        self.years_var.trace_add("write", lambda *_: self.rebuild_year_grid())
        tk.Button(row, text="套用年數（重設為等值預設）", font=FONT_SMALL,
                  command=self.apply_years_reset, bg="#E8EDF2", relief="groove"
                  ).pack(side="left", padx=12)

        self.year_sec = self._section(f, "逐年假設（可指定任意年度單獨修改）")
        self.year_grid = tk.Frame(self.year_sec, bg=C_BG)
        self.year_grid.pack(anchor="w")

        sec2 = self._section(f, "永續增長率 g（計算終值）")
        r = tk.Frame(sec2, bg=C_BG)
        r.pack(anchor="w")
        tk.Label(r, text="永續增長率 g：", font=FONT, bg=C_BG).pack(side="left")
        self.g_entry = tk.Entry(r, textvariable=self.var("g_pct", "2.5"), width=8,
                                font=FONT, justify="right")
        self.g_entry.pack(side="left", padx=6)
        tk.Label(r, text="%", font=FONT, bg=C_BG).pack(side="left")
        self.g_guard = tk.Label(sec2, text="", font=FONT_SMALL, bg=C_BG, fg=C_ERR)
        self.g_guard.pack(anchor="w", pady=(4, 0))
        self.g_entry.bind("<KeyRelease>", lambda _e: self._on_g_edit())
        self._hint(sec2, "防錯：WACC ≤ g 時 Gordon 終值公式無解，程式會禁止計算並彈出提示。"
                         "一般建議 g 不高於長期名義 GDP 增長（本樣本約 3.2%）。")

    def rebuild_year_grid(self) -> None:
        if self.state is None:
            return
        try:
            years = max(1, min(int(self.years_var.get()), 15))
        except ValueError:
            return
        self.state["years"] = years
        for w in self.year_grid.winfo_children():
            w.destroy()
        self.year_cells = {"growth": [], "margin": []}
        tk.Label(self.year_grid, text="年度", font=FONT_BOLD, bg=C_BG, width=8).grid(
            row=0, column=0)
        tk.Label(self.year_grid, text="營收成長率 %", font=FONT_BOLD, bg=C_BG, width=14).grid(
            row=0, column=1)
        tk.Label(self.year_grid, text="EBIT 利潤率 %", font=FONT_BOLD, bg=C_BG, width=14).grid(
            row=0, column=2)
        growth = self.state.get("growth") or []
        margin = self.state.get("margin") or []
        base_year = self.base.base_year if self.base else 2025
        for i in range(years):
            tk.Label(self.year_grid, text=f"第 {i + 1} 年\n（{base_year + i + 1}）",
                     font=FONT_SMALL, bg=C_BG).grid(row=i + 1, column=0, pady=1)
            for col, key, vals in (("growth", "growth", growth), ("margin", "margin", margin)):
                v = vals[i] * 100 if i < len(vals) else None
                e = tk.Entry(self.year_grid, width=10, font=FONT, justify="right")
                e.insert(0, f"{v:.1f}" if v is not None else "")
                e.grid(row=i + 1, column=1 if col == "growth" else 2, padx=2, pady=1)
                e.bind("<KeyRelease>", lambda _e: self._on_year_edit())
                self.year_cells[key].append(e)
        self._built_year_grid_n = years

    def _on_year_edit(self) -> None:
        self.user_edited.update(("growth", "margin"))
        self.collect_year_grid()

    def collect_year_grid(self) -> None:
        if self.state is None:
            return
        g, m = [], []
        for e in self.year_cells["growth"]:
            g.append(_f(e.get(), 0.0) / 100.0)
        for e in self.year_cells["margin"]:
            m.append(_f(e.get(), 0.0) / 100.0)
        self.state["growth"], self.state["margin"] = g, m

    def apply_years_reset(self) -> None:
        if self.state is None or self.base is None:
            return
        self.state = {**self.state,
                      **{k: v for k, v in dcf.default_state(self.base, self.meta).items()
                         if k in ("growth", "margin")}}
        self.state["years"] = max(1, min(int(_f(self.years_var.get(), 5)), 15))
        self.state["growth"] = self.state["growth"][:self.state["years"]]
        self.state["margin"] = self.state["margin"][:self.state["years"]]
        self.rebuild_year_grid()

    def refresh_forecast_from_state(self) -> None:
        if self.state is None:
            return
        self.years_var.set(str(self.state.get("years", 5)))
        self.var("g_pct").set(f"{self.state.get('g', 0.025) * 100:.1f}")
        self.rebuild_year_grid()
        self.check_g_guard()

    def _on_g_edit(self) -> None:
        self.user_edited.add("g")
        self.check_g_guard()

    def check_g_guard(self) -> None:
        if self.state is None:
            return
        g = _f(self.var("g_pct").get()) / 100.0
        self.state["g"] = g
        wacc = self.state.get("wacc", 0.09)
        if g >= wacc:
            self.g_guard.config(
                text=f"⚠ WACC（{wacc * 100:.2f}%）≤ g（{g * 100:.2f}%）：目前無法計算終值，"
                     "執行估值會被禁止。請調低 g 或調高 WACC。", fg=C_ERR)
        else:
            spread = (wacc - g) * 100
            self.g_guard.config(
                text=f"✓ WACC − g = {spread:.2f} 個百分點" +
                     ("（差距偏小，終值敏感度高）" if spread < 3 else ""), fg=C_OK)

    # ============================================================ 第 4 頁
    def _page_wacc(self, f: tk.Frame) -> None:
        sec = self._section(f, "折現率 WACC（加權平均資本成本）")
        row = tk.Frame(sec, bg=C_BG)
        row.pack(anchor="w")
        tk.Label(row, text="基準 WACC：", font=FONT, bg=C_BG).pack(side="left")
        self.wacc_entry = tk.Entry(row, textvariable=self.var("wacc_pct", "9.0"),
                                   width=8, font=FONT, justify="right")
        self.wacc_entry.pack(side="left", padx=6)
        self.wacc_entry.bind("<KeyRelease>", lambda _e: self.user_edited.add("wacc"))
        tk.Label(row, text="%", font=FONT, bg=C_BG).pack(side="left")
        tk.Button(row, text="⚡ 由宏觀模型取得 WACC", font=FONT_BOLD,
                  command=self.fetch_macro_wacc, bg="#0E7C7B", fg="white",
                  relief="flat", padx=12).pack(side="left", padx=(18, 0))
        self.sim_fail = tk.BooleanVar(value=False)
        tk.Checkbutton(sec, text="（測試用）模擬宏觀抓取失敗", variable=self.sim_fail,
                       font=FONT_SMALL, bg=C_BG).pack(anchor="w")
        self.macro_detail = tk.Label(sec, text="", font=FONT_SMALL, bg=C_BG,
                                     fg="#7A868F", justify="left", wraplength=880,
                                     anchor="w")
        self.macro_detail.pack(anchor="w", pady=(6, 0), fill="x")
        self._hint(sec, "宏觀抓取失敗時不會阻斷計算——保留手動輸入的 WACC 即可繼續。"
                        "折現率亦會自動套用到敏感性熱圖的中心點。")

    def refresh_wacc_fields(self) -> None:
        if self.state is None:
            return
        self.var("wacc_pct").set(f"{self.state.get('wacc', 0.09) * 100:.1f}")
        self.check_g_guard()

    def _collect_wacc(self) -> None:
        if self.state is None:
            return
        w = _f(self.var("wacc_pct").get()) / 100.0
        self.state["wacc"] = w
        self.state["mo_regime"] = self.mo_regime.get() if hasattr(self, "mo_regime") else "budget"
        self.check_g_guard()

    def fetch_macro_wacc(self) -> None:
        self._collect_wacc()
        if self.state is None:
            return
        try:
            snap = macro_analyzer.fetch(
                {"jurisdiction": self.jur_var.get(), "name": self.var("company_name").get()},
                simulate_failure=self.sim_fail.get())
        except Exception as e:
            self.macro_detail.config(
                text=f"⚠ 宏觀模型抓取失敗（{e}）。已保留手動 WACC "
                     f"{self.var('wacc_pct').get()}%，可直接繼續計算。", fg="#F08C00")
            return
        self.macro_snapshot = snap
        self.state["wacc"] = snap.wacc
        self.state["macro_gdp"] = snap.nominal_gdp_growth
        self.var("wacc_pct").set(f"{snap.wacc * 100:.2f}")
        self.check_g_guard()
        d = getattr(snap, "detail", None) or {}
        lines = [f"✓ 宏觀 WACC = {snap.wacc * 100:.2f}%｜長期名義 GDP = "
                 f"{snap.nominal_gdp_growth * 100:.2f}%"]
        for k, v in d.items():
            lines.append(f"　{k}：{v}")
        self.macro_detail.config(text="\n".join(lines), fg=C_OK)

    # ============================================================ 第 5 頁
    def _page_sens(self, f: tk.Frame) -> None:
        self._hint(f, "本頁全部為輔助分析，保持預設值即可直接執行估值；"
                      "主結論永遠以「基準情景的預計股價」為準。")
        sec = self._section(f, "三情景偏移（相對基準，可自訂幅度）")
        head = tk.Frame(sec, bg=C_BG)
        head.pack(anchor="w")
        for c, title in enumerate(["", "營收成長 ±pp", "EBIT 利潤率 ±pp",
                                   "WACC ±pp", "永續 g ±pp"]):
            tk.Label(head, text=title, font=FONT_BOLD, bg=C_BG, width=14).grid(
                row=0, column=c)
        for ri, (key, label) in enumerate((("optimistic", "樂觀"), ("conservative", "保守"))):
            tk.Label(head, text=label, font=FONT, bg=C_BG, width=8).grid(
                row=ri + 1, column=0, sticky="e")
            for c, fk in enumerate(("growth", "margin", "wacc", "g")):
                init = dcf.DEFAULT_OFFSETS[key][fk] * 100
                e = tk.Entry(head, width=8, font=FONT, justify="right")
                e.insert(0, f"{init:+.1f}")
                e.grid(row=ri + 1, column=c + 1, padx=3)
                e.bind("<KeyRelease>", lambda _e: self.collect_offsets())
        self.offset_entries = head

        sec2 = self._section(f, "敏感性熱圖範圍（WACC × 永續增長率）")
        r = tk.Frame(sec2, bg=C_BG)
        r.pack(anchor="w")
        for label, key, init in (("WACC 範圍 ±", "wacc_span_pct", "2.0"),
                                 ("g 範圍 ±", "g_span_pct", "1.0"),
                                 ("格數", "sens_steps", "9")):
            tk.Label(r, text=label, font=FONT, bg=C_BG).pack(side="left", padx=(10, 2))
            e = tk.Entry(r, textvariable=self.var(key, init), width=6, font=FONT,
                         justify="right")
            e.pack(side="left")
        tk.Label(r, text="pp", font=FONT_SMALL, bg=C_BG).pack(side="left")

    def collect_offsets(self) -> None:
        if self.state is None:
            return
        offs = {}
        rows = {"optimistic": 1, "conservative": 2}
        kids = self.offset_entries.winfo_children()
        for key, ri in rows.items():
            d = {}
            for c, fk in enumerate(("growth", "margin", "wacc", "g")):
                e = kids[ri * 5 + c + 1]
                d[fk] = _f(e.get(), 0.0) / 100.0
            offs[key] = d
        offs["base"] = {k: 0.0 for k in ("growth", "margin", "wacc", "g")}
        self.state["offsets"] = offs

    def collect_sens(self) -> None:
        if self.state is None:
            return
        self.state["wacc_span"] = _f(self.var("wacc_span_pct", "2"), 2) / 100.0
        self.state["g_span"] = _f(self.var("g_span_pct", "1"), 1) / 100.0
        self.state["sens_steps"] = max(3, min(int(_f(self.var("sens_steps", "9"), 9)), 15))

    # ============================================================ 第 6 頁
    def _page_result(self, f: tk.Frame) -> None:
        # KPI 區
        kpi = tk.Frame(f, bg="#1F5C5C")
        kpi.pack(fill="x", pady=(8, 6))
        self.kpi_price = tk.Label(kpi, text="—", font=FONT_BIG, bg="#1F5C5C", fg="white")
        self.kpi_price.pack(anchor="w", padx=20, pady=(10, 0))
        self.kpi_sub = tk.Label(kpi, text="載入財報後，按「▶ 執行估值」自動計算。",
                                font=FONT, bg="#1F5C5C", fg="#CFE8E8")
        self.kpi_sub.pack(anchor="w", padx=20, pady=(0, 4))
        self.kpi_side = tk.Label(kpi, text="", font=FONT, bg="#1F5C5C", fg="#CFE8E8",
                                 justify="right")
        self.kpi_side.pack(anchor="e", padx=20)

        self.tags_frame = tk.Frame(f, bg=C_BG)
        self.tags_frame.pack(fill="x", pady=4)

        # 四張圖 2×2
        charts = tk.Frame(f, bg=C_BG)
        charts.pack(fill="x", pady=4)
        kinds = [("heatmap", "WACC × g 敏感性熱圖"),
                 ("contribution", "折現現金流逐年貢獻"),
                 ("scenario", "三情景預計股價"),
                 ("bridge", "EV → 股權價值橋接")]
        self.figures, self.canvases = {}, {}
        for i, (kind, _t) in enumerate(kinds):
            cell = tk.Frame(charts, bg="white", bd=1, relief="solid")
            cell.grid(row=i // 2, column=i % 2, padx=6, pady=6, sticky="nsew")
            fig = Figure(figsize=(4.9, 3.4), dpi=96)
            canvas = FigureCanvasTkAgg(fig, master=cell)
            canvas.get_tk_widget().pack(fill="both", expand=True)
            self.figures[kind], self.canvases[kind] = fig, canvas

        exp = tk.Frame(f, bg=C_BG)
        exp.pack(anchor="w", pady=6)
        tk.Button(exp, text="💾 匯出 JSON（含全部參數溯源）", font=FONT,
                  command=lambda: self.export("json"), bg="#E8EDF2",
                  relief="groove", padx=10).pack(side="left")
        tk.Button(exp, text="📄 匯出報告（HTML，可列印 PDF）", font=FONT,
                  command=lambda: self.export("html"), bg="#E8EDF2",
                  relief="groove", padx=10).pack(side="left", padx=(8, 0))

        # 逐年表
        self.table = ttk.Treeview(f, columns=("y", "rev", "fcff", "pv", "cum"),
                                  show="headings", height=7)
        for col, w in (("y", 70), ("rev", 130), ("fcff", 130), ("pv", 130), ("cum", 130)):
            self.table.heading(col, text={"y": "年度", "rev": "營收", "fcff": "FCFF",
                                          "pv": "折現值", "cum": "累計現值"}[col])
            self.table.column(col, width=w, anchor="e")
        self.table.pack(fill="x", pady=6)

    # ------------------------------------------------ 跨頁籤橋接（診斷用）
    def dcf_snapshot(self) -> Dict[str, Any]:
        """給診斷頁籤的唯讀快照：狀態、結果、使用者已改欄位。"""
        return {
            "state": self.state,
            "result": self.result,
            "edited": set(self.user_edited),
            "has_company": self.base is not None,
        }

    def push_suggestions(self, values: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        """把診斷模組的建議值推送為 DCF 欄位預設。
        只寫入「使用者從未手動修改」的欄位；被改過的一律跳過。"""
        applied, skipped = [], []
        st = self.state
        if st is None:
            return applied, skipped
        for key, val in values.items():
            if key in self.user_edited:
                skipped.append(key)
                continue
            if key == "growth":
                st["growth"] = [float(v) for v in val]
            elif key == "margin":
                st["margin"] = [float(v) for v in val]
            elif key in ("wacc", "g"):
                st[key] = float(val)
                self.var("wacc_pct" if key == "wacc" else "g_pct").set(
                    f"{float(val) * 100:.1f}")
            else:
                continue
            applied.append(key)
        self.refresh_forecast_from_state()
        self.refresh_wacc_fields()
        return applied, skipped

    # ---------------------------------------------------------------- 執行
    def _collect_state(self) -> None:
        self._collect_wacc()
        self.collect_year_grid()
        self.collect_offsets()
        self.collect_sens()

    def run_valuation(self) -> None:
        if self.base is None or self.state is None:
            self.kpi_price.config(text="—")
            self.kpi_sub.config(text="請先在第 1 頁載入財報。")
            return
        self._collect_state()
        # 前端防錯：WACC ≤ g 直接彈窗禁止
        wacc, g = self.state.get("wacc", 0), self.state.get("g", 0)
        if g >= wacc:
            messagebox.showerror(
                "無法計算：WACC ≤ 永續增長率",
                f"WACC（{wacc * 100:.2f}%）必須大於永續增長率 g（{g * 100:.2f}%），\n"
                "否則 Gordon 終值公式分母 ≤ 0，數學上無解。\n\n"
                "請到「預測設定」調低 g，或到「折現率」調高 WACC。", parent=self.root)
            return
        try:
            self.result = dcf.compute(self.state, self.base, self.meta)
        except DCFError as e:
            messagebox.showerror("計算中止", f"{e}\n\n{getattr(e, 'hint', '')}",
                                 parent=self.root)
            return
        except Exception as e:
            messagebox.showerror("計算失敗", str(e), parent=self.root)
            return
        self.render_result()

    def render_result(self) -> None:
        r = self.result
        s = r.summary
        self.kpi_price.config(text=f"{r.inp.currency} {s['intrinsic_price']:,.2f}")
        up = s.get("upside")
        arrow = "▲" if (up or 0) > 0 else "▼"
        self.kpi_sub.config(
            text=f"{r.inp.company_name}｜現價 {s['market_price']:,.2f}　"
                 f"{arrow} 空間 {(up or 0) * 100:+.1f}%　"
                 f"（WACC {s['wacc'] * 100:.1f}% ／ g {s['g'] * 100:.1f}%）")
        self.kpi_side.config(
            text=f"EV {s['ev']:,.0f}\n股權 {s['equity']:,.0f}\n"
                 f"終值佔 EV {s['tv_share'] * 100:.0f}%")

        # 風險標籤
        for w in self.tags_frame.winfo_children():
            w.destroy()
        for t in r.tags:
            color = {"high": "#FFF0F0", "med": "#FFF8E6"}.get(t["level"], "#E6FCF5")
            bar = {"high": C_ERR, "med": "#F08C00"}.get(t["level"], C_OK)
            box = tk.Frame(self.tags_frame, bg=color, bd=0)
            box.pack(fill="x", pady=2)
            tk.Label(box, text="⚠ " + t["title"], font=FONT_BOLD, bg=color, fg=bar
                     ).pack(anchor="w", padx=8, pady=(3, 0))
            tk.Label(box, text=t.get("detail", ""), font=FONT_SMALL, bg=color,
                     fg="#495057", wraplength=940, justify="left").pack(
                anchor="w", padx=8, pady=(0, 3))
        if not r.tags:
            tk.Label(self.tags_frame, text="✓ 未觸發自動風險標籤", font=FONT,
                     bg="#E6FCF5", fg=C_OK, anchor="w").pack(fill="x", ipady=4)

        # 圖表
        for kind, fig in self.figures.items():
            mpl_charts.draw(fig, kind, r)
            self.canvases[kind].draw()

        # 逐年表
        for iid in self.table.get_children():
            self.table.delete(*self.table.get_children())
        cur = r.inp.currency
        for row in r.rows:
            self.table.insert("", "end", values=(
                row["fiscal_year"], f"{row['revenue']:,.0f}", f"{row['fcff']:,.0f}",
                f"{row['pv']:,.0f}", f"{row['cum_pv']:,.0f}"))

    def export(self, what: str) -> None:
        if self.result is None:
            messagebox.showinfo("尚無結果", "請先執行估值。", parent=self.root)
            return
        paths = exporter.save_files(self.result)
        p = paths["json"] if what == "json" else paths["html"]
        if messagebox.askyesno("匯出完成", f"已輸出：\n{p}\n\n要立即開啟嗎？",
                               parent=self.root):
            os.startfile(p)

    # ---------------------------------------------------------------- 收尾
    def on_close(self) -> None:
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
