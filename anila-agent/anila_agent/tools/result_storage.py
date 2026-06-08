"""P2-15 / claude-code §4.23 — ``maxResultSizeChars`` + tool result storage。

Tool 回傳的內容若過大(例如讀 100k 行的 log、整份 PDF text dump、SQL query
回 10 萬筆),整段塞回 LLM context 會撐爆 token budget 與 prompt cache。對應
claude-code-src ``src/utils/toolResultStorage.ts`` 的策略是:

1. 對 raw tool result 量大小(``char`` 計);
2. 超過閾值就把整段寫到一個 ``.anila/tool-results/<uuid>.txt`` 檔;
3. 給 LLM 看「preview(head 一截)+ truncation note + 檔案路徑」;
4. 後續 turn 內,LLM 若真的要看完整內容可以用 file-read tool 撈該檔。

本模組為 pure-function helpers + 一個輕量 ``ToolResultStore`` class,**不引新
dep**(只用 ``pathlib`` / ``uuid`` / stdlib hash)。Runtime 整合是 callsite
決定 — 可由 tool wrapper、`AnilaRunner` 後處理或 hook 系統呼叫,本模組保持
單純可組合。

設計取捨
--------
- ``store`` 預設不 enabled — 必須由呼叫端顯式構造 ``ToolResultStore`` 才會落檔。
  好處:test 環境不會留下垃圾檔案,prod 走 DI 注入。
- char count 用 ``len(str)``,不嘗試估算 token。理由跟 ``compaction.py`` 一致 ——
  本模組只關心「大小級別」決策,token 精度給 cost_tracker / token_budget。
- preview 取 ``head`` 為主,因 LLM 對 list / log 的判讀通常 head 訊息量最高。
  若呼叫端想要 head+tail(像 ``snip_compactor``),可改傳 ``preview_strategy=
  "head_and_tail"``。
- 不直接 mutate tool result wire 物件 —— 回傳一個新的 ``TruncatedResult``
  dataclass,呼叫端決定要把哪個欄位塞回 wire format(例如 OpenAI tool_output
  content)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# 預設大小門檻(字元數)。對齊 claude-code-src 預設 30000 chars(約 7500
# tokens)的同量級;呼叫端可在 store 構造時覆寫。
DEFAULT_MAX_RESULT_SIZE_CHARS: int = 30_000

# preview 預設長度(字元)。head-only 模式下取 head 此長度;head_and_tail
# 模式下各取一半。
DEFAULT_PREVIEW_CHARS: int = 2_000

# 落檔目錄(相對於 cwd 的 default;``ToolResultStore`` 構造時可改 absolute)。
DEFAULT_STORAGE_DIR: str = ".anila/tool-results"


PreviewStrategy = Literal["head", "head_and_tail"]


@dataclass(frozen=True)
class TruncatedResult:
    """tool result 經 truncation 後的呈現。

    Attributes:
        preview: 給 LLM 看的精簡內容(含 truncation note)。
        storage_path: 完整 raw content 落檔位置;若未啟用 storage 則為
            ``None``(此時 preview 就是 full content 或 in-memory truncated)。
        original_size: raw content 的 char 數,留作 hook / cost_tracker 參考。
        truncated: 是否真的有被截。``False`` 表 raw 沒超過門檻,``preview``
            等同 raw content;此情境呼叫端可選擇直接用 raw,但本模組仍給一致
            的物件以利上層 unconditional pipeline。
    """

    preview: str
    storage_path: Path | None
    original_size: int
    truncated: bool

    def to_dict(self) -> dict[str, str | int | bool | None]:
        """序列化(供 trace / hook 訊息使用)。"""
        return {
            "preview": self.preview,
            "storage_path": str(self.storage_path) if self.storage_path else None,
            "original_size": self.original_size,
            "truncated": self.truncated,
        }


def _build_preview(
    content: str,
    *,
    preview_chars: int,
    strategy: PreviewStrategy,
    storage_path: Path | None,
    original_size: int,
) -> str:
    """組 preview 字串(head / head_and_tail)+ truncation note。

    Note 寫成可被 LLM 理解的單行,並提示「完整內容在 ``storage_path``」(若有)。
    """
    if strategy == "head":
        head = content[:preview_chars]
        tail = ""
    else:
        side = max(1, preview_chars // 2)
        head = content[:side]
        tail = content[-side:]

    omitted = original_size - len(head) - len(tail)
    parts: list[str] = [head]
    if omitted > 0:
        if storage_path is not None:
            note = (
                f"\n\n[...truncated {omitted} chars — full result saved to "
                f"{storage_path}]\n\n"
            )
        else:
            note = f"\n\n[...truncated {omitted} chars]\n\n"
        parts.append(note)
    if tail:
        parts.append(tail)
    return "".join(parts)


class ToolResultStore:
    """落檔 helper —— 把過大的 tool result 寫到 ``storage_dir`` 並回 preview。

    一個 store 通常對應一個 agent run(或 session);``storage_dir`` 預設用
    cwd 下的 ``.anila/tool-results/``,呼叫端可改 absolute path 或 in-memory
    (傳 ``persist=False``)。

    用法:

        store = ToolResultStore(max_size_chars=30_000)
        result = store.process(tool_name="bash", content=huge_log_str)
        # result.preview → 塞回 wire format
        # result.storage_path → 後續 tool 可讀此 path 撈完整內容

    Thread-safety:落檔走 ``Path.write_text`` (atomic on POSIX for small files
    via filename uniqueness),`uuid.uuid4()` 保證 filename 不撞;此 class 本身
    無 mutable state,thread-safe。
    """

    def __init__(
        self,
        *,
        max_size_chars: int = DEFAULT_MAX_RESULT_SIZE_CHARS,
        preview_chars: int = DEFAULT_PREVIEW_CHARS,
        storage_dir: Path | str = DEFAULT_STORAGE_DIR,
        preview_strategy: PreviewStrategy = "head",
        persist: bool = True,
    ) -> None:
        if max_size_chars <= 0:
            raise ValueError("max_size_chars must be positive")
        if preview_chars <= 0:
            raise ValueError("preview_chars must be positive")
        if preview_chars >= max_size_chars:
            raise ValueError(
                "preview_chars must be smaller than max_size_chars; "
                "otherwise truncation is pointless"
            )
        self._max_size = max_size_chars
        self._preview_chars = preview_chars
        self._storage_dir = Path(storage_dir)
        self._strategy: PreviewStrategy = preview_strategy
        self._persist = persist

    @property
    def max_size_chars(self) -> int:
        return self._max_size

    @property
    def storage_dir(self) -> Path:
        return self._storage_dir

    def process(self, *, tool_name: str, content: str) -> TruncatedResult:
        """處理單筆 tool result。

        - content ≤ max → 回 ``TruncatedResult(preview=content, truncated=False)``
        - content > max → 截 preview;若 ``persist=True`` 落檔到
          ``storage_dir/<tool_name>-<uuid>.txt``;回 ``TruncatedResult``。
        """
        size = len(content)
        if size <= self._max_size:
            return TruncatedResult(
                preview=content,
                storage_path=None,
                original_size=size,
                truncated=False,
            )

        storage_path: Path | None = None
        if self._persist:
            self._storage_dir.mkdir(parents=True, exist_ok=True)
            # 用 tool_name + uuid 作為 filename — tool_name 讓人工 debug 較
            # 直覺,uuid 保證 uniqueness。tool_name 走 conservative sanitize
            # 防止路徑穿越。
            safe_name = _sanitize_filename_segment(tool_name)
            filename = f"{safe_name}-{uuid.uuid4().hex}.txt"
            storage_path = self._storage_dir / filename
            storage_path.write_text(content, encoding="utf-8")

        preview = _build_preview(
            content,
            preview_chars=self._preview_chars,
            strategy=self._strategy,
            storage_path=storage_path,
            original_size=size,
        )
        return TruncatedResult(
            preview=preview,
            storage_path=storage_path,
            original_size=size,
            truncated=True,
        )


_FILENAME_SAFE_CHARS: frozenset[str] = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _sanitize_filename_segment(name: str) -> str:
    """把 tool_name 變成 filesystem-safe 的 filename segment。

    非白名單字元一律換 ``_``,空字串退化成 ``tool``。長度上限 40 防止過長。
    """
    if not name:
        return "tool"
    cleaned = "".join(c if c in _FILENAME_SAFE_CHARS else "_" for c in name)
    cleaned = cleaned.strip("_") or "tool"
    return cleaned[:40]


__all__ = [
    "DEFAULT_MAX_RESULT_SIZE_CHARS",
    "DEFAULT_PREVIEW_CHARS",
    "DEFAULT_STORAGE_DIR",
    "PreviewStrategy",
    "ToolResultStore",
    "TruncatedResult",
]
