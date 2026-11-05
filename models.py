"""
models.py — 資料模型（所有假設的唯一來源）v2.0
=========================================================
把「專案假設」全部收斂成 dataclass：
  1. 任何分析函數都只吃這些物件，不會到處傳一長串參數
  2. 可以一鍵序列化成 JSON（專案檔 .fcff），方便存檔、比對、版本管理
  3. 集中做輸入校驗，UI 與 CLI 共用同一套規則

澳門稅制口徑（v2.0 依《所得補充稅規章》與年度預算案整理）
---------------------------------------------------------
  納稅人分組
    A 組：股份有限公司、資本 ≥ MOP 100 萬、近三年平均可課稅利潤 ≥ MOP 100 萬、
          最終母實體，或自願設有適當組織帳目並聲明列入者。
          每年 4–6 月經執業會計師申報；虧損可結轉 3 年。
    B 組：不屬 A 組者（多為中小微企），每年 2–3 月遞交 M/1 申報；
          由財政局按申報或指數方法核定；虧損僅在當年度考慮，不可結轉。

  稅率（規章級距，累進）
    ≤ 32,000              豁免
    32,001 – 65,000        3%
    65,001 – 100,000       5%
    100,001 – 200,000      7%
    200,001 – 300,000      9%
    > 300,000             12%
  年度預算案另設優惠：可課稅收益豁免額 MOP 600,000，超出部分 12%
  （2024/2025 年度均如此，本工具以「預算案口徑」為預設）。

  研發開支加計扣除（A 組、科技創新業務）
    首 MOP 3,000,000 開支 → 可扣減 3 倍可課稅收益
    超出部分               → 可扣減 2 倍
    總扣減上限 MOP 15,000,000

名詞對照（本工具一律以 MOP 為單位）
  units       年度銷量
  price       單位售價
  unit_cogs   單位銷貨成本
  rent        租金（固定成本之一）
  labor       人力薪金（固定成本之一）
  other_fixed 其他固定開支（水電、保險、行政…）
  EBITDA      現金營業利益 =（售價 − 單位COGS）× 銷量 − 三項固定成本 − 研發開支
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

CURRENCY = "MOP"

# ---------------------------------------------------------------- 常數
ASSET_CATEGORIES: Dict[str, Tuple[str, Optional[int]]] = {
    "building":    ("樓宇", 50),
    "leasehold":   ("租賃改良（按租期）", None),
    "machinery":   ("工業機器設備", 10),
    "furniture":   ("傢具及辦公設備", 5),
    "vehicle":     ("車輛", 5),
    "electronics": ("電子及電腦設備", 3),
    "ifrs16":      ("IFRS16 使用權資產", None),
    "custom":      ("自訂類別", None),
}

# 澳門所得補充稅累進級距：(該段上限, 該段稅率)。最後一段用 inf。
MACAU_BRACKETS: List[Tuple[float, float]] = [
    (32_000.0, 0.00),
    (65_000.0, 0.03),
    (100_000.0, 0.05),
    (200_000.0, 0.07),
    (300_000.0, 0.09),
    (math.inf, 0.12),
]

TAX_REGIMES: Dict[str, str] = {
    "budget":      "預算案口徑：首 60 萬豁免，超出 12%（現行年度優惠）",
    "progressive": "規章累進級距：3%、5%、7%、9%、12%",
    "fixed":       "自訂固定稅率（教學／跨境比較用）",
    "hk":          "香港利得稅・兩級制：首 200 萬 8.25%，其餘 16.5%（法團）",
}

# 適用法律／稅制
TAX_JURISDICTIONS: Dict[str, str] = {
    "MO": "澳門所得補充稅",
    "HK": "香港利得稅",
}

# 香港利得稅（法團兩級制）預設參數
HK_THRESHOLD: float = 2_000_000.0
HK_LOW_RATE: float = 0.0825
HK_HIGH_RATE: float = 0.165

TAXPAYER_GROUPS: Dict[str, str] = {
    "A": "A 組（帳目完備／有限公司；虧損可結轉 3 年，可享研發加計扣除）",
    "B": "B 組（中小微企；M/1 申報，虧損不可結轉）",
}

PROJECT_TYPES: Dict[int, str] = {
    1: "新產品／新業務擴張",
    2: "設備重置替換",
    3: "維護更新專案",
    4: "合規／環保／消防強制專案",
    5: "門店租賃裝修（租約到期終止）",
}

# 敏感性因子（順序即報表順序）
SENS_FACTORS: List[Tuple[str, str]] = [
    ("units", "年度銷量"),
    ("price", "單位售價"),
    ("unit_cogs", "單位COGS"),
    ("fixed_cost", "固定成本（三項合計）"),
    ("nwc_ratio", "NWC銷售佔比"),
    ("wacc", "WACC折現率"),
]

ANALYSIS_MODES: Dict[str, str] = {
    "full": "完整版 FCFF（三情景＋敏感性＋盈虧平衡＋圖表）",
    "lite": "輕量快速 NPV（只算基準情景，秒出結果）",
}

TERMINAL_MODES: Dict[str, str] = {
    "perpetual": "永續增長終值（Gordon）",
    "residual":  "不計永續價值，以資產殘值收尾",
    "none":      "完全不計期末價值",
}

NWC_MODES: Dict[str, str] = {
    "ratio":  "按銷售額比例（NWC = 銷售 × 比例）",
    "manual": "手動逐年輸入 NWC 變動額",
}

ONE_OFF_TREATMENTS: Dict[str, str] = {
    "deductible": "可扣稅費用（印花稅、顧問費…）",
    "capital":    "資本化（併入資產成本，不即時扣稅）",
    "non_tax":    "不影響稅務（保證金、押金…）",
}


class ConfigError(ValueError):
    """參數不合理時拋出，由 CLI／GUI 統一接住並顯示友善訊息。"""


def _as_list(value: Any) -> List[float]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [float(x) for x in value]
    return [float(value)]


# ---------------------------------------------------------------- 稅務
@dataclass
class TaxConfig:
    """所得稅設定：澳門所得補充稅 或 香港利得稅（jurisdiction 切換）。"""

    jurisdiction: str = "MO"            # "MO" = 澳門 ｜ "HK" = 香港
    taxpayer_group: str = "B"           # 澳門專用："A" | "B"
    regime: str = "budget"              # budget | progressive | fixed | hk
    free_threshold: float = 600_000.0   # regime=budget 的豁免額
    fixed_rate: float = 0.12            # regime=fixed 的稅率
    brackets: List[Tuple[float, float]] = field(
        default_factory=lambda: [(u, r) for u, r in MACAU_BRACKETS]
    )
    # 香港利得稅（法團兩級制）
    hk_threshold: float = HK_THRESHOLD  # 兩級制首段限額
    hk_low_rate: float = HK_LOW_RATE    # 首 hk_threshold 適用
    hk_high_rate: float = HK_HIGH_RATE  # 超出部分適用
    # 虧損結轉：None = 依稅制自動（澳門 A 組 3 年、B 組 0 年；香港無限期）
    loss_carryforward_years: Optional[int] = None

    # 研發開支加計扣除（A 組、科技創新業務）
    rd_enabled: bool = False
    rd_expense: float = 0.0             # 每年研發開支（另行列支，不重複計入固定成本）
    rd_first_limit: float = 3_000_000.0
    rd_first_multiple: float = 3.0      # 首 300 萬 → 扣減 3 倍
    rd_excess_multiple: float = 2.0     # 超出部分 → 扣減 2 倍
    rd_cap: float = 15_000_000.0

    # 政府補助
    subsidy_enabled: bool = False
    subsidy_amount: float = 0.0
    subsidy_year: int = 1
    subsidy_taxable: bool = True

    note: str = ""

    # -- 衍生 ---------------------------------------------------------
    @property
    def is_hk(self) -> bool:
        return self.jurisdiction == "HK"

    @property
    def group_label(self) -> str:
        if self.is_hk:
            return "香港利得稅・法團（兩級制）"
        return "A 組納稅人" if self.taxpayer_group == "A" else "B 組納稅人"

    @property
    def carry_years(self) -> int:
        if self.is_hk:
            # 香港稅務虧損可無限期結轉
            return 999 if self.loss_carryforward_years is None \
                else max(0, int(self.loss_carryforward_years))
        if self.loss_carryforward_years is not None:
            return max(0, int(self.loss_carryforward_years))
        return 3 if self.taxpayer_group == "A" else 0

    @property
    def label(self) -> str:
        if self.is_hk:
            return (f"香港利得稅｜兩級制 {self.hk_low_rate:.2%} / "
                    f"{self.hk_high_rate:.2%}")
        base = TAX_REGIMES.get(self.regime, self.regime).split("（")[0]
        return f"澳門所得補充稅・{self.group_label}｜{base}"

    @property
    def effective_headline_rate(self) -> float:
        """名義最高邊際稅率（報表說明用）。"""
        if self.is_hk:
            return self.hk_high_rate
        if self.regime == "budget":
            return 0.12
        if self.regime == "fixed":
            return self.fixed_rate
        return 0.12

    @property
    def rd_eligible(self) -> bool:
        if self.is_hk:
            return self.rd_enabled
        return self.rd_enabled and self.taxpayer_group == "A"

    def apply_jurisdiction(self) -> None:
        """切換澳門／香港時自動套用該司法管轄區的預設稅務參數。"""
        if self.jurisdiction == "HK":
            self.regime = "hk"
            self.taxpayer_group = "A"          # 香港無 A/B 組概念
            # 研發開支加計扣除：首 200 萬 3 倍、其餘 2 倍、無上限
            self.rd_first_limit = 2_000_000.0
            self.rd_first_multiple = 3.0
            self.rd_excess_multiple = 2.0
            self.rd_cap = 1.0e12
        else:
            self.jurisdiction = "MO"
            # 還原澳門預設（預算案口徑）
            if self.regime == "hk":
                self.regime = "budget"
            self.rd_first_limit = 3_000_000.0
            self.rd_first_multiple = 3.0
            self.rd_excess_multiple = 2.0
            self.rd_cap = 15_000_000.0

    def validate(self) -> None:
        self.apply_jurisdiction()
        if self.jurisdiction not in TAX_JURISDICTIONS:
            raise ConfigError("適用法律必須是 MO（澳門）或 HK（香港）。")
        if self.taxpayer_group not in ("A", "B"):
            raise ConfigError("納稅人組別必須是 A 組或 B 組。")
        if self.regime not in TAX_REGIMES:
            raise ConfigError("稅率口徑必須是 budget／progressive／fixed／hk 之一。")
        if self.free_threshold < 0:
            raise ConfigError("豁免額不可為負數。")
        if not 0 <= self.fixed_rate <= 0.6:
            raise ConfigError("自訂稅率請介於 0% 至 60% 之間。")
        if self.rd_enabled:
            if self.taxpayer_group != "A":
                raise ConfigError(
                    "研發開支加計扣除僅適用於 A 組納稅人；B 組請先切換組別，"
                    "或關閉此項優惠。")
            if self.rd_expense < 0:
                raise ConfigError("研發開支不可為負數。")
            if self.rd_first_limit < 0 or self.rd_cap < 0:
                raise ConfigError("研發扣減限額不可為負數。")
            if self.rd_first_multiple < 1 or self.rd_excess_multiple < 1:
                raise ConfigError("研發扣減倍數必須 ≥ 1（1 倍＝普通扣減）。")
        if self.subsidy_enabled:
            if self.subsidy_amount < 0:
                raise ConfigError("政府補助金額不可為負數。")
            if self.subsidy_year < 1:
                raise ConfigError("政府補助發生年度必須 ≥ 1。")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["brackets"] = [[u if math.isfinite(u) else None, r] for u, r in self.brackets]
        return d

    @staticmethod
    def from_dict(data: dict) -> "TaxConfig":
        data = dict(data or {})
        # 舊版 v1.x → v2：mode 1/2/3 對應 budget/progressive/fixed
        if "regime" not in data and "mode" in data:
            data["regime"] = {1: "budget", 2: "progressive", 3: "fixed"}.get(
                int(data.get("mode", 1)), "budget")
        brackets = data.get("brackets")
        if brackets:
            data["brackets"] = [(float(u) if u is not None else math.inf, float(r))
                                for u, r in brackets]
        else:
            data.pop("brackets", None)
        allowed = {f for f in TaxConfig.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in allowed and k != "brackets"}
        cfg = TaxConfig(**clean)
        if brackets:
            cfg.brackets = [(float(u) if u is not None else math.inf, float(r))
                            for u, r in brackets]
        return cfg


# ---------------------------------------------------------------- 資產
@dataclass
class AssetItem:
    """一筆資本支出（可多筆、可分期投入、可中途處置、可為 IFRS16 租賃）。"""

    name: str = "資產"
    category: str = "machinery"
    cost: float = 0.0
    start_year: int = 1          # 投入年度（1 = 第 1 年）
    dep_years: int = 10
    residual_pct: float = 0.0    # 專案結束時的估計殘值（佔成本比例）
    staging: List[float] = field(default_factory=list)  # 分期付款比例，例如 [0.5, 0.3, 0.2]
    # 處置
    disposal_year: int = 0       # 0 = 期內不處置
    disposal_proceeds: float = 0.0
    disposal_book_value: Optional[float] = None   # None = 自動以折舊後帳面值計算
    # IFRS16
    lease_term: int = 0
    lease_payment: float = 0.0   # 每年租金
    lease_rate: float = 0.05     # 增量借款利率
    lease_capitalized: bool = False   # True = 資本化（ROU 資產）；False = 營運租賃（租金入成本）
    note: str = ""

    # -- 衍生 ---------------------------------------------------------
    @property
    def category_label(self) -> str:
        return ASSET_CATEGORIES.get(self.category, ("自訂類別", None))[0]

    @property
    def is_lease(self) -> bool:
        return self.category == "ifrs16"

    @property
    def payments(self) -> List[float]:
        """各年實際付款金額（分期）。空清單代表一次付清。"""
        if self.cost <= 0:
            return []
        if not self.staging:
            return [self.cost]
        total = sum(self.staging)
        if total <= 0:
            return [self.cost]
        return [self.cost * w / total for w in self.staging]

    def payment_year_map(self) -> Dict[int, float]:
        """{年度: 付款額}，起算於 start_year。"""
        return {self.start_year + i: amt for i, amt in enumerate(self.payments)}

    def depreciation_schedule(self, life: int) -> List[float]:
        """回傳 index=1..life 的折舊額（index 0 未使用）。處置年度起不再提折舊。"""
        sched = [0.0] * (life + 1)
        if self.cost <= 0 or self.dep_years <= 0:
            return sched
        if self.is_lease and self.lease_capitalized:
            years = max(1, min(self.lease_term or self.dep_years, life))
        else:
            years = self.dep_years
        annual = self.cost / years
        stop = self.disposal_year if self.disposal_year else life + 1
        for t in range(max(1, self.start_year), min(life, self.start_year + years - 1) + 1):
            if t >= stop:
                break
            sched[t] = annual
        return sched

    def book_value_at(self, year: int, life: int) -> float:
        """第 year 年『年初』（處置前）的帳面值。"""
        sched = self.depreciation_schedule(life)
        taken = sum(sched[1:max(1, year)])
        return max(self.cost - taken, 0.0)

    def validate(self, life: int) -> None:
        if self.cost < 0:
            raise ConfigError(f"資產「{self.name}」成本不可為負數。")
        if not 1 <= self.start_year <= life:
            raise ConfigError(
                f"資產「{self.name}」投入年度必須介於第 1 年至第 {life} 年。")
        if not 1 <= self.dep_years <= 60:
            raise ConfigError(f"資產「{self.name}」折舊年期請介於 1 至 60 年。")
        if not 0 <= self.residual_pct <= 1:
            raise ConfigError(f"資產「{self.name}」殘值比例請介於 0% 至 100%。")
        if self.staging:
            if any(w < 0 for w in self.staging):
                raise ConfigError(f"資產「{self.name}」分期比例不可為負數。")
            if abs(sum(self.staging) - 1.0) > 1e-6:
                raise ConfigError(
                    f"資產「{self.name}」分期比例合計須為 100%，目前為 "
                    f"{sum(self.staging):.1%}。")
            if self.start_year + len(self.staging) - 1 > life:
                raise ConfigError(
                    f"資產「{self.name}」分期付款跨越專案年期（第 "
                    f"{self.start_year}–{self.start_year + len(self.staging) - 1} 年），"
                    f"請縮短分期或調整投入年度。")
        if self.disposal_year:
            if not self.start_year <= self.disposal_year <= life:
                raise ConfigError(
                    f"資產「{self.name}」處置年度必須介於投入年度與專案年期之間。")
            if self.disposal_proceeds < 0:
                raise ConfigError(f"資產「{self.name}」處置價款不可為負數。")
            if self.disposal_book_value is not None and self.disposal_book_value < 0:
                raise ConfigError(f"資產「{self.name}」處置帳面值不可為負數。")
        if self.is_lease:
            if self.lease_payment < 0:
                raise ConfigError("IFRS16 租賃的年租金不可為負數。")
            if not 0 <= self.lease_rate <= 0.5:
                raise ConfigError("IFRS16 租賃的折現率請介於 0% 至 50%。")
            if self.lease_capitalized:
                if not 1 <= self.lease_term <= 60:
                    raise ConfigError("IFRS16 租賃期請介於 1 至 60 年。")
                if self.lease_term + self.start_year - 1 > life + 1:
                    raise ConfigError(
                        "IFRS16 租賃期超出專案年期太多，請縮短租期或延長專案年期。")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "AssetItem":
        allowed = set(AssetItem.__dataclass_fields__)
        return AssetItem(**{k: v for k, v in (data or {}).items() if k in allowed})


# ---------------------------------------------------------------- 一次性現金流
@dataclass
class OneOffFlow:
    """指定年度發生的一次性現金流（印花稅、顧問費、押金、補助以外的收支）。"""

    year: int = 1
    label: str = "一次性支出"
    amount: float = 0.0          # 正數＝流入、負數＝流出
    treatment: str = "deductible"   # deductible | capital | non_tax

    @property
    def treatment_label(self) -> str:
        return ONE_OFF_TREATMENTS.get(self.treatment, self.treatment)

    def validate(self, life: int) -> None:
        if not 1 <= self.year <= life:
            raise ConfigError(
                f"一次性現金流「{self.label}」的發生年度必須介於第 1 年至第 {life} 年。")
        if self.treatment not in ONE_OFF_TREATMENTS:
            raise ConfigError(f"一次性現金流「{self.label}」的稅務處理設定無效。")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "OneOffFlow":
        allowed = set(OneOffFlow.__dataclass_fields__)
        return OneOffFlow(**{k: v for k, v in (data or {}).items() if k in allowed})


# ---------------------------------------------------------------- 營運資本
@dataclass
class NWCPlan:
    mode: str = "ratio"              # ratio | manual
    ratio: float = 0.06              # NWC = 銷售額 × 比例
    manual: List[float] = field(default_factory=list)   # 逐年 ΔNWC（正數＝佔用）
    recover_at_end: bool = True      # 期末回收（不計永續終值時）

    def validate(self, life: int) -> None:
        if self.mode not in NWC_MODES:
            raise ConfigError("NWC 口徑必須是「按銷售比例」或「手動逐年輸入」。")
        if not 0 <= self.ratio <= 1:
            raise ConfigError("NWC 佔銷售比例請介於 0 至 1（例：6% 輸入 0.06）。")
        if self.mode == "manual":
            if len(self.manual) != life:
                raise ConfigError(
                    f"手動 NWC 需輸入 {life} 年的變動額，目前只有 {len(self.manual)} 年。")

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "NWCPlan":
        allowed = set(NWCPlan.__dataclass_fields__)
        return NWCPlan(**{k: v for k, v in (data or {}).items() if k in allowed})


# ---------------------------------------------------------------- 營運序列
@dataclass
class OperatingSeries:
    """
    逐年營運假設。長度必須等於專案年期——這是「防輸入錯位」的第一道關卡。

    固定成本拆成租金／人力／其他三項，各自可有獨立增長率；
    報表可分別檢視，但引擎只取合計。
    """

    units: List[float] = field(default_factory=list)
    price: List[float] = field(default_factory=list)
    unit_cogs: List[float] = field(default_factory=list)
    rent: List[float] = field(default_factory=list)
    labor: List[float] = field(default_factory=list)
    other_fixed: List[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.units)

    @property
    def fixed_cost(self) -> List[float]:
        return [a + b + c for a, b, c in zip(self.rent, self.labor, self.other_fixed)]

    def lengths(self) -> Dict[str, int]:
        return {
            "銷量": len(self.units), "售價": len(self.price),
            "單位COGS": len(self.unit_cogs), "租金": len(self.rent),
            "人力": len(self.labor), "其他固定": len(self.other_fixed),
        }

    def validate(self, life: int, label: str = "情景") -> None:
        for name, n in self.lengths().items():
            if n != life:
                raise ConfigError(
                    f"{label}的「{name}」序列長度為 {n} 年，與專案年期 {life} 年不符，"
                    f"請檢查是否輸入錯位。")
        for i in range(life):
            if self.price[i] <= 0:
                raise ConfigError(f"{label} 第 {i+1} 年售價必須大於 0。")
            for name, seq in (("單位成本", self.unit_cogs), ("銷量", self.units),
                              ("租金", self.rent), ("人力", self.labor),
                              ("其他固定開支", self.other_fixed)):
                if seq[i] < 0:
                    raise ConfigError(f"{label} 第 {i+1} 年{name}不可為負數。")

    def scaled(self, units: float = 1.0, price: float = 1.0,
               unit_cogs: float = 1.0, fixed_cost: float = 1.0) -> "OperatingSeries":
        """等比縮放（敏感性與盈虧平衡求解時使用）。"""
        return OperatingSeries(
            units=[u * units for u in self.units],
            price=[p * price for p in self.price],
            unit_cogs=[c * unit_cogs for c in self.unit_cogs],
            rent=[f * fixed_cost for f in self.rent],
            labor=[f * fixed_cost for f in self.labor],
            other_fixed=[f * fixed_cost for f in self.other_fixed],
        )

    @staticmethod
    def from_growth(price0: float, cogs0: float, units0: float, life: int,
                    rent0: float = 0.0, labor0: float = 0.0, other0: float = 0.0,
                    g_units: float = 0.0, g_price: float = 0.0, g_cogs: float = 0.0,
                    g_rent: float = 0.0, g_labor: float = 0.0,
                    g_other: float = 0.0) -> "OperatingSeries":
        """以「首年值 + 年成長率」生成逐年序列。"""
        t = range(life)
        return OperatingSeries(
            units=[units0 * (1 + g_units) ** i for i in t],
            price=[price0 * (1 + g_price) ** i for i in t],
            unit_cogs=[cogs0 * (1 + g_cogs) ** i for i in t],
            rent=[rent0 * (1 + g_rent) ** i for i in t],
            labor=[labor0 * (1 + g_labor) ** i for i in t],
            other_fixed=[other0 * (1 + g_other) ** i for i in t],
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "OperatingSeries":
        data = data or {}
        # 舊版 v1.x 只有 units/price/unit_cogs/fixed_cost → 全部歸到「其他固定」
        if "rent" not in data and "fixed_cost" in data:
            fc = _as_list(data.get("fixed_cost"))
            return OperatingSeries(
                units=_as_list(data.get("units")), price=_as_list(data.get("price")),
                unit_cogs=_as_list(data.get("unit_cogs")),
                rent=[0.0] * len(fc), labor=[0.0] * len(fc), other_fixed=fc)
        return OperatingSeries(
            units=_as_list(data.get("units")), price=_as_list(data.get("price")),
            unit_cogs=_as_list(data.get("unit_cogs")),
            rent=_as_list(data.get("rent")), labor=_as_list(data.get("labor")),
            other_fixed=_as_list(data.get("other_fixed")))


# ---------------------------------------------------------------- 情景
SCENARIO_META: Dict[str, Tuple[str, str]] = {
    "optimistic":   ("樂觀情景", "optimistic"),
    "base":         ("基準情景", "base"),
    "conservative": ("保守情景", "conservative"),
}


@dataclass
class ScenarioSpec:
    """
    單一情景。WACC 可由各情景獨立指定（None = 沿用專案的基準 WACC），
    永續終值開關與 g 亦各自獨立；probability 用於計算期望 NPV。
    """

    key: str = "base"
    name: str = "基準情景"
    series: OperatingSeries = field(default_factory=OperatingSeries)
    use_tv: bool = False
    g_perp: float = 0.02
    wacc: Optional[float] = None
    probability: float = 0.0
    note: str = ""
    delay_years: int = 0

    @property
    def color(self) -> str:
        return SCENARIO_META.get(self.key, ("", "base"))[1]

    def effective_wacc(self, fallback: float) -> float:
        return fallback if self.wacc is None else self.wacc

    def validate(self, life: int, fallback_wacc: float = 0.10) -> None:
        self.series.validate(life, self.name)
        if not 0 <= self.delay_years < life:
            raise ConfigError(f"{self.name}：延遲生效年份必須小於專案年期。")
        w = self.effective_wacc(fallback_wacc)
        if not 0 < w < 0.6:
            raise ConfigError(f"{self.name}：WACC 請介於 0% 至 60% 之間。")
        if self.use_tv:
            if not 0 <= self.g_perp <= 0.08:
                raise ConfigError(f"{self.name}：永續增長率請介於 0% 至 8%。")
            if w <= self.g_perp:
                raise ConfigError(
                    f"{self.name}：WACC（{w:.2%}）必須大於永續增長率（{self.g_perp:.2%}），"
                    f"否則終值公式無意義。")
        if not 0 <= self.probability <= 1:
            raise ConfigError(f"{self.name}：發生機率請介於 0% 至 100%。")

    def to_dict(self) -> dict:
        return {
            "name": self.name, "use_tv": self.use_tv, "g_perp": self.g_perp,
            "wacc": self.wacc, "probability": self.probability,
            "note": self.note, "delay_years": self.delay_years,
            "series": self.series.to_dict(),
        }


# ---------------------------------------------------------------- 專案
@dataclass
class ProjectConfig:
    """完整專案假設。"""

    name: str = "澳門資本預算專案"
    project_type: int = 1
    currency: str = CURRENCY
    unit_label: str = "件"
    life: int = 8
    wacc: float = 0.10
    analysis_mode: str = "full"        # full | lite

    assets: List[AssetItem] = field(default_factory=lambda: [AssetItem()])
    one_offs: List[OneOffFlow] = field(default_factory=list)
    nwc: NWCPlan = field(default_factory=NWCPlan)

    terminal_mode: str = "perpetual"   # perpetual | residual | none
    tv_maintenance_capex: bool = False

    reinvest_rate: Optional[float] = None   # MIRR 再投資率（None = WACC）
    finance_rate: Optional[float] = None    # MIRR 融資率（None = WACC）

    tax: TaxConfig = field(default_factory=TaxConfig)
    scenarios: Dict[str, ScenarioSpec] = field(default_factory=dict)
    # 每個因子的 [下浮幅度, 上浮幅度]，可完全不對稱
    sens_ranges: Dict[str, List[float]] = field(default_factory=lambda: {
        "units": [0.10, 0.10], "price": [0.10, 0.10], "unit_cogs": [0.10, 0.10],
        "fixed_cost": [0.10, 0.10], "nwc_ratio": [0.20, 0.20], "wacc": [0.10, 0.10],
    })
    sens_pair: List[str] = field(default_factory=lambda: ["price", "units"])
    note: str = ""

    # -- 便捷存取 -----------------------------------------------------
    @property
    def base(self) -> ScenarioSpec:
        return self.scenarios["base"]

    @property
    def optimistic(self) -> ScenarioSpec:
        return self.scenarios["optimistic"]

    @property
    def conservative(self) -> ScenarioSpec:
        return self.scenarios["conservative"]

    @property
    def ordered_scenarios(self) -> List[ScenarioSpec]:
        return [self.optimistic, self.base, self.conservative]

    @property
    def project_type_label(self) -> str:
        return PROJECT_TYPES.get(self.project_type, "自訂專案")

    @property
    def is_lite(self) -> bool:
        return self.analysis_mode == "lite"

    @property
    def capex_total(self) -> float:
        return sum(a.cost for a in self.assets)

    @property
    def recover_nwc(self) -> bool:
        return self.nwc.recover_at_end

    @property
    def nwc_ratio(self) -> float:
        return self.nwc.ratio

    @property
    def mirr_reinvest(self) -> float:
        return self.reinvest_rate if self.reinvest_rate is not None else self.wacc

    @property
    def mirr_finance(self) -> float:
        return self.finance_rate if self.finance_rate is not None else self.wacc

    def scenario_wacc(self, key: str) -> float:
        scn = self.scenarios.get(key)
        return self.wacc if scn is None else scn.effective_wacc(self.wacc)

    # -- 校驗 ---------------------------------------------------------
    def validate(self) -> None:
        # 幣別隨適用法律自動同步（MOP ↔ HKD）
        if self.tax.jurisdiction == "HK" and self.currency == "MOP":
            self.currency = "HKD"
        elif self.tax.jurisdiction == "MO" and self.currency == "HKD":
            self.currency = "MOP"
        if not 1 <= self.life <= 50:
            raise ConfigError("專案年期請介於 1 至 50 年。")
        if not 0 < self.wacc < 0.6:
            raise ConfigError("WACC 請介於 0% 至 60% 之間（例：9.5% 輸入 0.095）。")
        if self.analysis_mode not in ANALYSIS_MODES:
            raise ConfigError("分析模式必須是「完整版」或「輕量版」。")
        if self.terminal_mode not in TERMINAL_MODES:
            raise ConfigError("期末處理方式設定無效。")
        if not self.assets:
            raise ConfigError("至少要有一筆資本支出（可在第 2 頁新增）。")
        for a in self.assets:
            a.validate(self.life)
        for f in self.one_offs:
            f.validate(self.life)
        self.nwc.validate(self.life)
        self.tax.validate()
        if self.tax.subsidy_enabled and self.tax.subsidy_year > self.life:
            raise ConfigError(
                f"政府補助年度（第 {self.tax.subsidy_year} 年）超出專案年期 {self.life} 年。")
        if self.mirr_reinvest <= -1 or self.mirr_finance <= -1:
            raise ConfigError("MIRR 的再投資率／融資率必須大於 -100%。")

        for key in ("optimistic", "base", "conservative"):
            if key not in self.scenarios:
                raise ConfigError(f"缺少「{SCENARIO_META[key][0]}」設定。")
            self.scenarios[key].validate(self.life, self.wacc)

        for key, _ in SENS_FACTORS:
            pair = self.sens_ranges.get(key)
            if not pair or len(pair) != 2:
                raise ConfigError(f"敏感性因子「{key}」需要 [下浮, 上浮] 兩個幅度。")
            for v in pair:
                if not 0 <= v <= 1.0:
                    raise ConfigError(
                        f"敏感性因子「{key}」的浮動幅度請介於 0 與 1 之間（例：0.15 = 15%）。")
            if pair[0] == 0 and pair[1] == 0:
                raise ConfigError(f"敏感性因子「{key}」的上下幅度不可同時為 0。")

        if not self.is_lite:
            total_p = sum(s.probability for s in self.ordered_scenarios)
            if total_p > 0 and abs(total_p - 1.0) > 0.01:
                raise ConfigError(
                    f"三情景發生機率合計須為 100%，目前為 {total_p:.1%}。")

    # -- 提醒（不阻擋計算） -------------------------------------------
    def warnings(self) -> List[str]:
        out: List[str] = []
        for a in self.assets:
            if a.dep_years > self.life and a.start_year == 1:
                out.append(
                    f"資產「{a.name}」折舊年期 {a.dep_years} 年長於專案年期 {self.life} 年："
                    f"專案期內只攤提 {self.life} 年折舊，其餘年限的稅盾不會實現。")
            if a.start_year + a.dep_years - 1 > self.life:
                out.append(
                    f"資產「{a.name}」於第 {a.start_year} 年投入、攤銷 {a.dep_years} 年，"
                    f"攤銷期跨越專案年期；期末帳面值將以殘值方式處理。")
        if self.project_type == 5 and any(s.use_tv for s in self.ordered_scenarios):
            out.append(
                "專案類型為「門店租賃裝修（租約到期終止）」，但仍有情景啟用永續終值；"
                "租約到期後若無法續約，期末不應計入永續價值。")
        tv_flags = [s.use_tv for s in self.ordered_scenarios if s.use_tv]
        if self.nwc.recover_at_end and tv_flags and len(tv_flags) < 3:
            out.append(
                "三情景的終值設定不一致：計 TV 的情景不回收期末 NWC（事業延續，"
                "營運資本仍被佔用），不計 TV 的情景則會回收，比較 NPV 時請留意口徑差異。")
        if self.tv_maintenance_capex and tv_flags:
            out.append(
                "終值口徑已扣除維持性資本支出（以年折舊估算），TV 會低於"
                "「永續增長（未扣）」的簡化版本。")
        lease_cap = [a for a in self.assets if a.is_lease and a.lease_capitalized]
        if lease_cap:
            out.append(
                "IFRS16 租賃採「資本化」口徑：租金不計入營運成本，FCFF 因此較高，"
                "但租賃負債屬融資負債；計算股權價值時須自企業價值扣除租賃負債。")
        if self.tax.rd_enabled and self.tax.rd_expense > 0:
            out.append(
                "研發加計扣除以「另行列支的年度研發開支」計算，"
                "請確認該金額未同時包含在『其他固定開支』內，避免重複扣除。")
        if self.tax.taxpayer_group == "B" and self.tax.rd_enabled:
            out.append("B 組納稅人不適用研發加計扣除，該項已自動忽略。")
        return out

    # -- 序列化 -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema": "fcff-macau/2",
            "name": self.name,
            "project_type": self.project_type,
            "currency": self.currency,
            "unit_label": self.unit_label,
            "life": self.life,
            "wacc": self.wacc,
            "analysis_mode": self.analysis_mode,
            "terminal_mode": self.terminal_mode,
            "tv_maintenance_capex": self.tv_maintenance_capex,
            "reinvest_rate": self.reinvest_rate,
            "finance_rate": self.finance_rate,
            "note": self.note,
            "tax": self.tax.to_dict(),
            "assets": [a.to_dict() for a in self.assets],
            "one_offs": [f.to_dict() for f in self.one_offs],
            "nwc": self.nwc.to_dict(),
            "sens_ranges": {k: list(v) for k, v in self.sens_ranges.items()},
            "sens_pair": list(self.sens_pair),
            "scenarios": {k: s.to_dict() for k, s in self.scenarios.items()},
        }

    @staticmethod
    def from_dict(data: dict) -> "ProjectConfig":
        data = dict(data or {})
        defaults = ProjectConfig()

        tax = TaxConfig.from_dict(data.get("tax", {}))

        # 資產：新版 assets 清單；舊版單一 asset
        if "assets" in data:
            assets = [AssetItem.from_dict(a) for a in data.get("assets") or []]
        elif "asset" in data:
            old = data.get("asset") or {}
            assets = [AssetItem(
                name=old.get("category_label", "原有資產"),
                category=old.get("category", "machinery"),
                cost=float(old.get("capex_total", 0) or 0),
                start_year=1, dep_years=int(old.get("dep_years", 10) or 10),
                disposal_year=0, disposal_proceeds=0.0,
                note=old.get("note", ""),
            )]
            # 舊版「舊資產處置」→ 一筆零成本、帶處置價的資產
            if float(old.get("old_sale_proceeds", 0) or 0) > 0:
                assets.append(AssetItem(
                    name="舊資產處置", category="custom", cost=0.0,
                    start_year=1, dep_years=1, disposal_year=1,
                    disposal_proceeds=float(old.get("old_sale_proceeds", 0) or 0),
                    disposal_book_value=float(old.get("old_book_value", 0) or 0),
                ))
        else:
            assets = [AssetItem()]

        # NWC：新版 nwc dict；舊版 nwc_ratio / recover_nwc
        if "nwc" in data and isinstance(data["nwc"], dict):
            nwc = NWCPlan.from_dict(data["nwc"])
        else:
            nwc = NWCPlan(ratio=float(data.get("nwc_ratio", 0.06) or 0.06),
                          recover_at_end=bool(data.get("recover_nwc", True)))

        # 敏感度：新版 [down, up]；舊版單一浮點數
        sens = dict(defaults.sens_ranges)
        for k, v in (data.get("sens_ranges") or {}).items():
            if isinstance(v, (int, float)):
                sens[k] = [float(v), float(v)]
            elif isinstance(v, (list, tuple)) and len(v) == 2:
                sens[k] = [float(v[0]), float(v[1])]

        scenarios: Dict[str, ScenarioSpec] = {}
        for key, scn in (data.get("scenarios") or {}).items():
            scenarios[key] = ScenarioSpec(
                key=key,
                name=scn.get("name", SCENARIO_META.get(key, ("情景", ""))[0]),
                series=OperatingSeries.from_dict(scn.get("series", {})),
                use_tv=bool(scn.get("use_tv", False)),
                g_perp=float(scn.get("g_perp", 0.02) or 0.0),
                wacc=(None if scn.get("wacc") in (None, "") else float(scn["wacc"])),
                probability=float(scn.get("probability", 0) or 0),
                note=scn.get("note", ""),
                delay_years=int(scn.get("delay_years", 0) or 0),
            )

        terminal = data.get("terminal_mode")
        if terminal not in TERMINAL_MODES:
            terminal = "perpetual" if any(s.use_tv for s in scenarios.values()) else "none"

        mode = data.get("analysis_mode", "full")
        cfg = ProjectConfig(
            name=data.get("name", "未命名專案"),
            project_type=int(data.get("project_type", 1) or 1),
            currency=data.get("currency", CURRENCY),
            unit_label=data.get("unit_label", "件") or "件",
            life=int(data.get("life", 8) or 8),
            wacc=float(data.get("wacc", 0.10) or 0.10),
            analysis_mode=mode if mode in ANALYSIS_MODES else "full",
            assets=assets,
            one_offs=[OneOffFlow.from_dict(f) for f in (data.get("one_offs") or [])],
            nwc=nwc,
            terminal_mode=terminal,
            tv_maintenance_capex=bool(data.get("tv_maintenance_capex", False)),
            reinvest_rate=(None if data.get("reinvest_rate") in (None, "")
                           else float(data["reinvest_rate"])),
            finance_rate=(None if data.get("finance_rate") in (None, "")
                          else float(data["finance_rate"])),
            tax=tax,
            scenarios=scenarios,
            sens_ranges=sens,
            sens_pair=list(data.get("sens_pair") or ["price", "units"]),
            note=data.get("note", ""),
        )
        return cfg
