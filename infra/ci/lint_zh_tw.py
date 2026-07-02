#!/usr/bin/env python3
"""繁體中文(台灣用語)語言政策 lint — doc 11。

掃描使用者可見字串,偵測 ①簡體字(繁簡相異的高頻字) ②大陸用語詞。
簡體 blocklist 以 unicode code point 表達 → 原始碼本身不含任何簡體字面,
lint 不會誤傷自己,也符合本 repo「禁簡體」政策。

豁免:行內標記 ``zh-exempt``(doc 11 裝飾性豁免清單,如登入 cosplay、
CONFIDENTIAL 浮水印);整檔/路徑豁免見 EXEMPT_PATHS。

用法:python3 infra/ci/lint_zh_tw.py [root]  (exit 1 表示有未豁免命中)
CI:與 infra/ci/lint-boundaries.sh 並列呼叫。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 繁簡相異的高頻簡體字(以 code point 表達,原始碼不含字面);刻意排除在
# 繁體亦合法/他用的字(戶/據/別/後/確/係 等)以降低誤報。
SIMPLIFIED_CP = frozenset({
    0x8FD9, 0x5185, 0x89C6, 0x9891, 0x6D4B, 0x8BD5, 0x7C7B,  # 這內視頻測試類
    0x5355, 0x7F51, 0x7EDC, 0x8F6F, 0x8BBE, 0x5907, 0x5220,  # 單網絡軟設備刪
    0x5173, 0x95ED, 0x8BA4, 0x6570, 0x5904, 0x5E94, 0x663E,  # 關閉認數處應顯
    0x7EDF, 0x68C0, 0x8BCD, 0x7F16, 0x8F91, 0x8F7D, 0x8BE2,  # 統檢詞編輯載詢
    0x65F6, 0x95E8, 0x95EE, 0x9898, 0x534E, 0x6743, 0x7EC4,  # 時門問題華權組
    0x5458, 0x52A1, 0x62A5, 0x8BC1, 0x9875, 0x5F00, 0x6001,  # 員務報證頁開態
    0x4F1A, 0x4E2A, 0x8BB0, 0x5F55, 0x5E93, 0x9009, 0x62E9,  # 會個記錄庫選擇
    0x952E, 0x8BF7, 0x53F7, 0x79F0, 0x56FE, 0x6807, 0x7B7E,  # 鍵請號稱圖標籤
    0x53D1, 0x5BFC, 0x6267, 0x4EA7, 0x8D26, 0x53C2, 0x8FDB,  # 發導執產賬參進
})

# 大陸用語詞 → 台灣用語(key/value 皆繁體字,非簡體;doc 11 術語表)。
MAINLAND_TERMS = {
    "視頻": "影片",
    "屏幕": "螢幕",
    "軟件": "軟體",
    "硬件": "硬體",
    "網絡": "網路",
    "信息": "資訊",
    "用戶": "使用者",
    "默認": "預設",
    "在線": "線上",
    "數據庫": "資料庫",
    "服務器": "伺服器",
    "打印": "列印",
    "登錄": "登入",
    "卸載": "解除安裝",
    "激活": "啟用",
    "緩存": "快取",
    "內存": "記憶體",
    "菜單": "選單",
    "對話框": "對話方塊",
}

EXEMPT_PATHS = (
    "node_modules/", "dist/", "build/", ".venv/", "__pycache__/",
    "/tests/", "test_", ".test.", ".spec.",
    "docs/", "scraps/", "examples/",
)

EXEMPT_MARKER = "zh-exempt"

# 掃描目標:前端使用者字串 + 後端 detail=。
TARGETS = {
    ".vue": None, ".jsx": None, ".tsx": None,
    ".js": None, ".ts": None,
    ".py": re.compile(r"detail\s*="),  # 後端只掃 HTTPException detail
}


def scan_file(path: Path, root: Path) -> list[tuple[int, str]]:
    rel = str(path.relative_to(root))
    if any(seg in f"/{rel}" for seg in EXEMPT_PATHS):
        return []
    py_gate = TARGETS.get(path.suffix)
    hits: list[tuple[int, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError):
        return []
    for i, line in enumerate(lines, 1):
        if EXEMPT_MARKER in line:
            continue
        if py_gate is not None and not py_gate.search(line):
            continue
        tokens: list[str] = [f"U+{ord(c):04X}" for c in line
                             if ord(c) in SIMPLIFIED_CP]
        for term in MAINLAND_TERMS:
            if term in line:
                tokens.append(f"term:{'-'.join(f'{ord(c):04X}' for c in term)}")
        if tokens:
            hits.append((i, " ".join(sorted(set(tokens)))))
    return hits


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    total = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in TARGETS:
            continue
        for lineno, tokens in scan_file(path, root):
            print(f"{path.relative_to(root)}:{lineno}: 疑似簡體/大陸用語 [{tokens}]")
            total += 1
    if total:
        print(f"\n共 {total} 處未豁免命中。修正為台灣用語,"
              f"或加 {EXEMPT_MARKER} 標記(限裝飾性)。")
        return 1
    print("zh-TW lint 通過:無簡體字 / 大陸用語命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
