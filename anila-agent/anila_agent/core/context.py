"""AnilaToolContext + FileStateCache — 共用的 tool 執行情境結構。

本模組對應 enhancement roadmap P0-3:把「workspace 邊界」「session / turn / tool_call
識別」「檔案狀態快取」這三件事用一個 dataclass 收齊,當作後續 P0-4 / P0-6 / P0-7 / P0-8
共用的注入點。

設計重點:

* `AnilaToolContext` 是 turn-scoped 物件,每個 tool callback 都拿到「同一份」實例,
  以便在同一輪 tool calls 內共享 file_state_cache、避免重複 read 同一個檔。
* `FileStateCache` 紀錄已被 read / write 過的檔案 metadata(mtime + size + sha256),
  讓後續 tool 可以判斷「我 read 過的這個檔有沒有被第三方動過」,避免 write-after-other-write
  race(對應 claude-code-src §4.5)。
* `safe_path` 提供 workspace 邊界保護,是 P0-7 Policy DSL `workspace_only` 的基礎元件。

本檔故意不引入 openai-agents 或 antigravity 依賴,只用 std lib,以保持可單獨測試。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# FileStateCache
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileStateEntry:
    """單一檔案在 cache 中的快照。

    紀錄發生 record_read / record_write 那一刻的 stat 與 sha256,作為事後比對基準。
    """

    path: Path
    mtime_ns: int
    size: int
    sha256: str
    action: str  # 'read' 或 'write',供 debug 用

    def matches_disk(self, current_mtime_ns: int, current_size: int) -> bool:
        """快速比對 mtime + size。完整內容比對請另外讀檔做 sha256。"""
        return self.mtime_ns == current_mtime_ns and self.size == current_size


class FileStateCache:
    """以 path 為 key 的檔案狀態快取。

    用途:讓 tool 鏈知道「我剛 read 過這檔,還沒人動,可以放心 write」。
    避免 Read → (第三方寫入) → Edit 這種 silent race。
    """

    def __init__(self) -> None:
        """建立空 cache。每個 turn / session 建議獨立一份。"""
        self._entries: dict[Path, FileStateEntry] = {}

    # ---- 寫入 ------------------------------------------------------------

    def record_read(self, path: Path) -> FileStateEntry:
        """紀錄一次 read 動作,回存當下的 stat + sha256。

        path 必須已存在於磁碟,呼叫端要先確定檔案存在。
        """
        entry = self._snapshot(path, action="read")
        self._entries[entry.path] = entry
        return entry

    def record_write(self, path: Path) -> FileStateEntry:
        """紀錄一次 write 動作(覆蓋既有 entry)。

        通常在 tool 寫完檔之後呼叫,把 cache 更新到「我剛寫入」的狀態。
        """
        entry = self._snapshot(path, action="write")
        self._entries[entry.path] = entry
        return entry

    # ---- 查詢 ------------------------------------------------------------

    def get(self, path: Path) -> FileStateEntry | None:
        """取出已紀錄的 entry,沒紀錄回傳 None。"""
        return self._entries.get(self._normalize(path))

    def has(self, path: Path) -> bool:
        """檢查 path 是否已被紀錄過(不論 read / write)。"""
        return self._normalize(path) in self._entries

    def is_unchanged_since_read(self, path: Path) -> bool:
        """檢查檔案自上次 record_* 之後是否未被第三方修改。

        判斷邏輯:
        1. cache 內必須有 entry,否則回傳 False(沒讀過就無法保證)。
        2. 磁碟上的 mtime_ns + size 必須完全等於 entry。
        3. 為保險再算一次 sha256 比對,杜絕 mtime 沒變但內容換過的極端情況。
        """
        norm = self._normalize(path)
        entry = self._entries.get(norm)
        if entry is None:
            return False
        try:
            stat = norm.stat()
        except FileNotFoundError:
            return False
        if not entry.matches_disk(stat.st_mtime_ns, stat.st_size):
            return False
        current_hash = _sha256_of(norm)
        return current_hash == entry.sha256

    def clear(self) -> None:
        """清空 cache。turn 切換或測試 teardown 時用。"""
        self._entries.clear()

    def snapshot(self) -> dict[Path, FileStateEntry]:
        """回傳 entries 的淺拷貝供 debug / log 用。"""
        return dict(self._entries)

    # ---- 內部 ------------------------------------------------------------

    @staticmethod
    def _normalize(path: Path | str) -> Path:
        """把任意 path 轉成 resolved absolute Path(供 dict key 用)。"""
        return Path(path).resolve()

    def _snapshot(self, path: Path, *, action: str) -> FileStateEntry:
        """為 path 算 stat + sha256 並產生 entry。"""
        norm = self._normalize(path)
        stat = norm.stat()
        return FileStateEntry(
            path=norm,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            sha256=_sha256_of(norm),
            action=action,
        )


def _sha256_of(path: Path, chunk_size: int = 65536) -> str:
    """串流計算檔案 sha256。對大檔避免一次讀進記憶體。"""
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        while True:
            chunk = fp.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# AnilaToolContext
# ---------------------------------------------------------------------------


class WorkspaceEscapeError(ValueError):
    """嘗試讀寫的 path 跳脫了 workspace 邊界時丟出。

    對應 antigravity policy.py 的 workspace_only deny,但這層是「呼叫端 helper」,
    policy 層再另外擋一次,做縱深防禦。
    """


@dataclass
class AnilaToolContext:
    """每個 tool call 都拿到一份的執行情境。

    欄位設計貼合 claude-code-src §4.5 的概念草案,但縮到 P0-3 一定要的最小集合,
    其它(query_chain / cost_accumulator / append_system_message …)等後續 task 再擴。

    `workspace = None` 表示「不啟用 workspace 邊界」,safe_path 會直接 resolve 給的 path。
    這保留給「我就是要全機掃描的 admin tool」場景。
    """

    session_id: str
    turn_id: int
    tool_call_id: str
    agent_name: str
    workspace: Path | None = None
    file_state_cache: FileStateCache = field(default_factory=FileStateCache)
    user_id: str | None = None
    caller_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """把 workspace 統一 resolve 成絕對路徑,後續比對才不會被相對路徑騙過。"""
        if self.workspace is not None:
            self.workspace = Path(self.workspace).resolve()

    # ---- 邊界保護 --------------------------------------------------------

    def safe_path(self, path: Path | str) -> Path:
        """把 tool 傳進來的 path 解析為「保證在 workspace 內」的絕對路徑。

        規則:
        1. workspace = None → 直接回傳 resolve 後的絕對路徑(無邊界)。
        2. 相對路徑 → anchor 在 workspace 底下再 resolve。
        3. resolve 後的最終 path 必須是 workspace 的後裔(含 workspace 自身)。
        4. 任何嘗試跳脫(`../../etc/passwd` / `/etc/passwd` 等)都丟 WorkspaceEscapeError。

        這層 helper 是 P0-7 workspace_only policy 在「tool 內主動呼叫」這條路徑的基底;
        policy 層會在 tool 外層再擋一次,做縱深防禦。
        """
        raw = Path(path)
        if self.workspace is None:
            return raw.resolve()

        # 相對路徑就 anchor 在 workspace 底下;絕對路徑保持原樣再做後續邊界檢查。
        candidate = raw if raw.is_absolute() else (self.workspace / raw)
        resolved = candidate.resolve()

        if not _is_within(resolved, self.workspace):
            raise WorkspaceEscapeError(
                f"path {resolved!s} escapes workspace {self.workspace!s}",
            )
        return resolved

    def is_inside_workspace(self, path: Path | str) -> bool:
        """非 raising 版的邊界檢查,給 policy / log 用。"""
        if self.workspace is None:
            return True
        try:
            resolved = Path(path)
            if not resolved.is_absolute():
                resolved = (self.workspace / resolved)
            return _is_within(resolved.resolve(), self.workspace)
        except OSError:
            return False


def _is_within(child: Path, parent: Path) -> bool:
    """child 是否等於 parent 或 parent 的後裔。兩邊都假設已 resolve 過。"""
    try:
        # commonpath 在跨檔案系統 / Windows drive 不同時會丟 ValueError,用它判斷最穩。
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:
        return False
