"""FileChangeTrigger — 監看 file mtime/size 改變的 trigger。

對應上游 antigravity ``on_file_change(path, callback)``,但 **不引 watchfiles
dep** — 用標準函式庫 ``pathlib`` + ``asyncio`` 輪詢檔案 mtime/size。對 sub-agent
use case (監一小撮 prompt 模板 / config 檔) 而言效能完全夠。

Debounce 設計:atomic save (例如 ``tmp -> rename``) 可能在 100ms 內連續觸發
multiple stat 結果,因此本 trigger 內建 1 秒 debounce — 偵測到變動後 sleep
``debounce_seconds`` 再 snapshot 一次,以那次的差異 batch 給 callback。

Use case 範例:

* **prompt 模板熱重載**: ``FileChangeTrigger(["prompts/*.md"], reload_prompts)``
* **config 更新**: ``FileChangeTrigger(["config.yaml"], notify_config_change)``

注意:本實作 **不遞迴掃描目錄**;若給入目錄 path 只會監那個目錄本身的 mtime,
不會自動展開到子檔。要監多檔請把 ``paths`` 展平後傳入。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from anila_agent.triggers.base import Trigger

if TYPE_CHECKING:
    from anila_agent.core.hook_context import HookContext
    from anila_agent.triggers.base import TriggerManager

logger = logging.getLogger(__name__)

# callback 簽章:async (ctx, changed_paths) -> None。
FileChangeCallback = Callable[["HookContext", Sequence[Path]], Awaitable[None]]


class FileChangeTrigger(Trigger):
    """監看一組檔案 path 的 mtime/size,變動時觸發 callback。

    Attributes:
        paths: 要監看的 ``pathlib.Path`` list。檔案不存在也合法 (snapshot
            記為 ``None``),之後被建立會視為 change。
        callback: async callable,sig 為
            ``async (ctx: HookContext, changed_paths: Sequence[Path]) -> None``。
        poll_interval_seconds: 輪詢間隔,預設 0.5 秒。
        debounce_seconds: 偵測到 change 後再 wait 多久才 fire,
            預設 1.0 秒以吸收 atomic save 雙寫入。
        custom_name: 可選 trigger 名稱。

    snapshot 結構:``dict[Path, tuple[mtime_or_None, size_or_None]]``。
    用 ``(None, None)`` 表示「該檔此刻不存在」,等於以「不存在」當一個合法狀態。
    """

    def __init__(
        self,
        paths: Sequence[Path | str],
        callback: FileChangeCallback,
        *,
        poll_interval_seconds: float = 0.5,
        debounce_seconds: float = 1.0,
        name: str | None = None,
    ) -> None:
        """正規化 paths 為 ``pathlib.Path``,初始 snapshot 留到 ``run`` 才抓。

        Args:
            paths: 要監看的 path list (str / Path 皆可,內部轉成 Path)。
            callback: async (ctx, changed_paths) -> None。
            poll_interval_seconds: 輪詢間隔,> 0。
            debounce_seconds: debounce 等待,>= 0 (0 = 不 debounce)。
            name: 自訂 trigger 名;未給用 ``"file_change_<n>files"``。

        Raises:
            ValueError: paths 為空、poll_interval_seconds <= 0、或
                debounce_seconds < 0。
        """
        if not paths:
            raise ValueError("paths must contain at least one path")
        if poll_interval_seconds <= 0:
            raise ValueError(
                f"poll_interval_seconds must be positive, got {poll_interval_seconds!r}"
            )
        if debounce_seconds < 0:
            raise ValueError(
                f"debounce_seconds must be non-negative, got {debounce_seconds!r}"
            )
        self.paths: list[Path] = [Path(p) for p in paths]
        self.callback = callback
        self.poll_interval_seconds = poll_interval_seconds
        self.debounce_seconds = debounce_seconds
        self._name = name or f"file_change_{len(self.paths)}files"

    @property
    def name(self) -> str:
        """trigger 名稱,例如 ``"file_change_3files"``。"""
        return self._name

    # ---- snapshot helpers ---------------------------------------------

    def _snapshot(self) -> dict[Path, tuple[float | None, int | None]]:
        """對所有監看 path 抓一份 (mtime, size) snapshot。

        檔案不存在或 stat 失敗都記成 ``(None, None)``。size 用來捕捉
        「mtime 相同但內容改了」(雖然罕見) + 不存在 ↔ 存在的 transition。
        """
        snapshot: dict[Path, tuple[float | None, int | None]] = {}
        for path in self.paths:
            try:
                st = path.stat()
                snapshot[path] = (st.st_mtime, st.st_size)
            except (FileNotFoundError, OSError):
                snapshot[path] = (None, None)
        return snapshot

    @staticmethod
    def _diff(
        prev: dict[Path, tuple[float | None, int | None]],
        curr: dict[Path, tuple[float | None, int | None]],
    ) -> list[Path]:
        """比較兩份 snapshot,回傳發生變動的 path list (穩定排序)。"""
        changed: list[Path] = []
        for path, curr_state in curr.items():
            if prev.get(path) != curr_state:
                changed.append(path)
        return changed

    # ---- main loop -----------------------------------------------------

    async def run(self, manager: TriggerManager) -> None:
        """long-lived loop:``poll -> diff -> (if changed) debounce -> fire``。

        debounce 邏輯:第一次偵測到差異不直接 fire,而是 sleep
        ``debounce_seconds`` 再抓一次最新 snapshot — 把那段時間內所有變動
        當作同一批給 callback,以避免 atomic save 重複觸發。
        """
        prev = self._snapshot()
        while True:
            await asyncio.sleep(self.poll_interval_seconds)
            curr = self._snapshot()
            changed = self._diff(prev, curr)
            if not changed:
                continue

            # debounce:等一下再抓最新狀態,以這次 diff 為準回傳給 callback。
            if self.debounce_seconds > 0:
                await asyncio.sleep(self.debounce_seconds)
                curr = self._snapshot()
                changed = self._diff(prev, curr)
                if not changed:
                    # debounce 期間又改回去了 — 罕見,但合法。
                    prev = curr
                    continue

            prev = curr
            logger.debug(
                "FileChangeTrigger '%s' detected %d change(s): %s",
                self._name,
                len(changed),
                [str(p) for p in changed],
            )
            await self.fire(manager, changed)


__all__ = ["FileChangeCallback", "FileChangeTrigger"]
