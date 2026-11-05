"""
provenance.py — 參數溯源（v3.0 新增，NPV 與 DCF 共用）
=========================================================
這個模組只做一件事：讓每一個假設都能回答三個問題——

    這個數字是多少？    → Param.value
    它從哪裡來？        → Param.source（財報擷取／模型推導／人工覆寫／宏觀模型／預設）
    誰在什麼時候改過？  → Param.note + Param.updated

設計約定
--------
1. **鎖定即保護**：欄位一旦被人工覆寫，`locked=True`；
   之後任何 `merge_auto()`（重新抓財報、重新整理）都不會覆蓋它。
2. **只記錄、不計算**：本模組不參與任何估值運算，避免污染既有引擎。
3. **可序列化**：`snapshot()` 直接進 JSON 報告與 HTML 報告的附錄，
   讓報告的每一個數字都能被追回到來源。

來源代碼（source）
------------------
    auto      財報自動擷取（Parser 從年報／CSV 抓到的原始數）
    derived   模型推導（例如由 收入 − 成本 − 費用 推出的 EBIT、各項佔收入比）
    manual    人工覆寫（使用者改過，已鎖定）
    macro     宏觀模型提供（例如「由宏觀模型取得 WACC」）
    default   系統預設值（未被任何人改動）
    input     使用者直接輸入（非覆寫既有值，例如新增的年度假設）
    scenario  情景偏移（樂觀／保守的加減碼）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

# ---------------------------------------------------------------- 來源字典
SOURCE_LABELS: Dict[str, str] = {
    "auto": "財報擷取",
    "derived": "模型推導",
    "manual": "人工覆寫",
    "macro": "宏觀模型",
    "default": "系統預設",
    "input": "使用者輸入",
    "scenario": "情景偏移",
}

#: 前端徽章配色語義（與圖表／GUI 共用）
SOURCE_TONES: Dict[str, str] = {
    "auto": "info",
    "derived": "muted",
    "manual": "lock",
    "macro": "accent",
    "default": "muted",
    "input": "info",
    "scenario": "warn",
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


@dataclass
class Param:
    """單一參數的「值 + 身世」。"""

    key: str
    label: str
    value: Any
    unit: str = ""
    source: str = "default"
    note: str = ""
    locked: bool = False
    updated: str = field(default_factory=now_text)
    group: str = ""

    # -- 衍生 ---------------------------------------------------------
    @property
    def source_label(self) -> str:
        return source_label(self.source)

    @property
    def tone(self) -> str:
        return SOURCE_TONES.get(self.source, "muted")

    @property
    def badge(self) -> str:
        """給 UI 用的一行徽章文字，例如「人工覆寫 · 已鎖定」。"""
        if self.locked and self.source == "manual":
            return "人工覆寫 · 已鎖定"
        if self.locked:
            return f"{self.source_label} · 已鎖定"
        return self.source_label

    # -- 異動 ---------------------------------------------------------
    def override(self, value: Any, note: str = "") -> "Param":
        self.value = value
        self.source = "manual"
        self.locked = True
        self.note = note or f"使用者於 {now_text()} 覆寫（原值不再自動更新）"
        self.updated = now_text()
        return self

    def unlock(self) -> "Param":
        self.locked = False
        if self.source == "manual":
            self.source = "default"
            self.note = "已解除鎖定，下次自動整理可被覆蓋"
        self.updated = now_text()
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "source_label": self.source_label,
            "note": self.note,
            "locked": self.locked,
            "updated": self.updated,
            "group": self.group,
        }


class ParamBook:
    """一本參數帳：維持插入順序，支援鎖定與自動合併。"""

    def __init__(self, namespace: str = "") -> None:
        self.namespace = namespace
        self._params: Dict[str, Param] = {}

    # -- 寫入 ---------------------------------------------------------
    def set(self, key: str, label: str, value: Any, unit: str = "",
            source: str = "default", note: str = "", group: str = "",
            locked: bool = False) -> Param:
        p = Param(key=key, label=label, value=value, unit=unit, source=source,
                  note=note, locked=locked, group=group)
        self._params[key] = p
        return p

    def override(self, key: str, value: Any, note: str = "") -> Param:
        p = self._params.get(key)
        if p is None:
            raise KeyError(f"參數不存在：{key}")
        return p.override(value, note)

    def lock(self, key: str, value: Any = None, note: str = "") -> Param:
        p = self._params[key]
        return p.override(p.value if value is None else value, note)

    def unlock(self, key: str) -> Param:
        p = self._params.get(key)
        if p is None:
            raise KeyError(f"參數不存在：{key}")
        return p.unlock()

    # -- 自動整理（尊重鎖定） -----------------------------------------
    def merge_auto(self, values: Dict[str, Any],
                   labels: Optional[Dict[str, str]] = None,
                   units: Optional[Dict[str, str]] = None,
                   notes: Optional[Dict[str, str]] = None,
                   sources: Optional[Dict[str, str]] = None,
                   group: str = "") -> Dict[str, List[str]]:
        """
        把「重新解析出來的財報數字」併進來。

        回傳 {"updated": [...], "kept": [...], "added": [...]}，
        UI 可直接顯示「3 項因人工覆寫而保留」。
        """
        labels = labels or {}
        units = units or {}
        notes = notes or {}
        sources = sources or {}
        updated: List[str] = []
        kept: List[str] = []
        added: List[str] = []
        for key, value in values.items():
            p = self._params.get(key)
            if p is None:
                self.set(key, labels.get(key, key), value, units.get(key, ""),
                         sources.get(key, "auto"), notes.get(key, ""), group)
                added.append(key)
                continue
            if p.locked:
                kept.append(key)
                continue
            p.value = value
            p.source = sources.get(key, "auto")
            p.note = notes.get(key, "")
            p.updated = now_text()
            updated.append(key)
        return {"updated": updated, "kept": kept, "added": added}

    # -- 讀取 ---------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        p = self._params.get(key)
        return default if p is None else p.value

    def param(self, key: str) -> Optional[Param]:
        return self._params.get(key)

    def __contains__(self, key: str) -> bool:
        return key in self._params

    def __len__(self) -> int:
        return len(self._params)

    def items(self) -> Iterable[Param]:
        return self._params.values()

    def snapshot(self) -> List[Dict[str, Any]]:
        return [p.to_dict() for p in self._params.values()]

    def summary(self) -> Dict[str, int]:
        out = {k: 0 for k in SOURCE_LABELS}
        for p in self._params.values():
            out[p.source] = out.get(p.source, 0) + 1
        out["locked"] = sum(1 for p in self._params.values() if p.locked)
        return out

    def manual_keys(self) -> List[str]:
        return [k for k, p in self._params.items() if p.locked]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "namespace": self.namespace,
            "generated_at": now_text(),
            "summary": self.summary(),
            "params": self.snapshot(),
        }
