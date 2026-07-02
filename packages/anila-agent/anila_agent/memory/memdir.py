"""檔案式長期記憶（移植自 Claude Code memdir）—— 樣板的旗艦差異化。

每則記憶是一個帶 YAML frontmatter 的 ``<name>.md``；``MEMORY.md`` 是恆載入的索引
（一行一則）。SDK 沒有等價物；Sessions 是短期/append-only/untyped，memdir 是長期/
typed/distilled，可當第二層檢索索引。

安全：``<name>`` 必須是 kebab/snake 安全字元（防路徑穿越）；記憶根目錄經 ``validate_memory_dir``
驗證（拒相對/root/UNC/null-byte/裸-~）。索引有大小上限（200 行 / 25KB）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from anila_agent.memory.taxonomy import VALID_TYPES, coerce_type

INDEX_FILE = "MEMORY.md"
MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25_600

_NAME_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def validate_name(name: str) -> str:
    """記憶名須為 kebab/snake 安全字元（防路徑穿越）。"""
    if not _NAME_RE.match(name):
        raise ValueError(f"記憶名 {name!r} 不合法（只允許小寫英數與 -/_，不可含路徑分隔）")
    return name


def validate_memory_dir(raw: str | Path) -> Path:
    """驗證記憶根目錄；拒絕可疑路徑（防 autoMemoryDirectory override 被惡意重導）。

    拒：空 / null byte / 裸-~ / root / UNC / 含 ``..`` 穿越。允許操作者指定的絕對或
    一般相對目錄（預設就是相對的 .anila/memory）。
    """
    s = str(raw)
    if not s or "\x00" in s:
        raise ValueError("記憶目錄不可為空或含 null byte")
    if s in ("~", "/") or s.startswith("\\\\"):
        raise ValueError(f"記憶目錄 {s!r} 不被允許（裸-~ / root / UNC）")
    expanded = Path(s).expanduser()
    if ".." in expanded.parts:
        raise ValueError(f"記憶目錄 {s!r} 不可含 .. 穿越")
    return expanded


_TENANT_UNSAFE_RE = re.compile(r"[^a-z0-9_-]+")


def tenant_slug(user_id: str) -> str:
    """把不可信的 user id 轉成「單一路徑元件」的租戶分艙 key（多租戶記憶用）。

    安全要求：
      * 無路徑穿越——``/`` ``\\`` ``..`` null 等全被正規式換成 ``_``，產物保證是單一
        路徑元件，逃不出 ``tenants/`` 之下（``validate_memory_dir`` 為第二層防護）。
      * 不碰撞——slug 撞了就等於跨租戶記憶洩漏，正是要防的事。digest 取自**傳入的原
        字串本身**（不在雜湊前 strip），故 ``Alice``/``alice``、``a``/`` a`` 都得到不同
        分艙；即使可讀前綴相同（如清洗後 ``a/b`` 與 ``a_b``）hash 仍唯一。
      * 空白拒絕——全空白 / 空字串**不是合法租戶**，直接 raise。否則它會塌縮成單一
        常數桶（``u-e3b0c4…``），讓所有「空白 id」的請求共用記憶＝跨租戶洩漏。呼叫端
        必須先把空白身分正規化成 None（走「無記憶」），不可塞空白進來。

    產出形如 ``alice-3f2a9c8b7d6e5f40``。
    """
    if not user_id or not user_id.strip():
        raise ValueError("tenant_slug：空白 user_id 不是合法租戶 key（呼叫端應正規化成 None）")
    digest = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:16]
    prefix = _TENANT_UNSAFE_RE.sub("_", user_id.strip().lower())[:40].strip("_") or "u"
    return f"{prefix}-{digest}"


@dataclass
class Memory:
    name: str
    description: str
    type: str
    body: str = ""

    def to_markdown(self) -> str:
        coerce_type(self.type)  # 驗證 type
        return (
            "---\n"
            f"name: {self.name}\n"
            f"description: {self.description}\n"
            "metadata:\n"
            f"  type: {self.type}\n"
            "---\n\n"
            f"{self.body.strip()}\n"
        )


def _parse_markdown(text: str) -> Memory | None:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None
    import yaml

    fm = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    name = str(fm.get("name", "")).strip()
    description = str(fm.get("description", "")).strip()
    meta = fm.get("metadata") or {}
    mtype = str((meta.get("type") if isinstance(meta, dict) else "") or "").strip()
    if not name or mtype not in VALID_TYPES:
        return None
    return Memory(name=name, description=description, type=mtype, body=body)


@dataclass
class MemoryStore:
    """管理一個記憶目錄：per-topic *.md + MEMORY.md 索引。"""

    root: Path = field(default_factory=lambda: Path(".anila/memory"))

    def __post_init__(self) -> None:
        self.root = validate_memory_dir(self.root)

    def _path(self, name: str) -> Path:
        return self.root / f"{validate_name(name)}.md"

    def write(self, memory: Memory) -> Path:
        validate_name(memory.name)
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(memory.name)
        path.write_text(memory.to_markdown(), encoding="utf-8")
        self._rebuild_index()
        return path

    def read(self, name: str) -> Memory | None:
        path = self._path(name)
        if not path.is_file():
            return None
        return _parse_markdown(path.read_text(encoding="utf-8"))

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.is_file():
            return False
        path.unlink()
        self._rebuild_index()
        return True

    def list(self) -> list[Memory]:
        """回傳所有記憶的 frontmatter（manifest；body 也帶回但 recall 多半只用 description）。"""
        if not self.root.is_dir():
            return []
        out: list[Memory] = []
        for path in sorted(self.root.glob("*.md")):
            if path.name == INDEX_FILE:
                continue
            mem = _parse_markdown(path.read_text(encoding="utf-8"))
            if mem is not None:
                out.append(mem)
        return out

    def manifest(self) -> dict[str, str]:
        """name → description 的對照（給 recall 選擇器用）。"""
        return {m.name: m.description for m in self.list()}

    def index_text(self) -> str:
        """讀 MEMORY.md；不存在則由 manifest 即時組一份。"""
        index = self.root / INDEX_FILE
        if index.is_file():
            return index.read_text(encoding="utf-8")
        return self._render_index(self.list())

    def _render_index(self, memories: list[Memory]) -> str:
        lines = ["# MEMORY.md", ""]
        for m in memories:
            lines.append(f"- [{m.name}]({m.name}.md) — {m.description}")
        text = "\n".join(lines) + "\n"
        # 套上限：超過則截斷並標註。
        if len(text.encode("utf-8")) > MAX_INDEX_BYTES or text.count("\n") > MAX_INDEX_LINES:
            kept = lines[: MAX_INDEX_LINES]
            kept.append(f"- …（索引超過上限，已截斷；共 {len(memories)} 則）")
            text = "\n".join(kept) + "\n"
        return text

    def _rebuild_index(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / INDEX_FILE).write_text(self._render_index(self.list()), encoding="utf-8")
