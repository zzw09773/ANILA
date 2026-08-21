"""把 memdir store + 混合 recall 綁成一個可注入 run-context 的執行單元。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from anila_agent.memory.extract import Extractor, extract_memories, make_extractor
from anila_agent.memory.memdir import Memory, MemoryStore, tenant_slug
from anila_agent.memory.recall import (
    EmbedFn,
    SelectFn,
    make_embed_fn,
    make_llm_select_fn,
    recall,
)

if TYPE_CHECKING:
    from anila_agent.config import AppConfig


def auto_memory_enabled() -> bool:
    """ANILA_AUTO_MEMORY=1 才在 turn 結束自動抽取寫入（多一次 LLM 呼叫）。"""
    return os.getenv("ANILA_AUTO_MEMORY", "").strip() == "1"


@dataclass
class MemdirRuntime:
    """memdir 的執行期門面：store + recall 後端 + 自動抽取器。"""

    store: MemoryStore
    embed_fn: EmbedFn | None = None
    select_fn: SelectFn | None = None
    extractor: Extractor | None = None

    async def recall_bodies(self, query: str, k: int = 5) -> list[Memory]:
        names = await recall(
            query, self.store.manifest(), embed_fn=self.embed_fn, select_fn=self.select_fn, k=k
        )
        out: list[Memory] = []
        for name in names:
            mem = self.store.read(name)
            if mem is not None:
                out.append(mem)
        return out

    async def absorb_turn(self, user_text: str, assistant_text: str) -> list[str]:
        """從一輪對話抽取長期記憶、**去敏後**寫入本租戶 store。

        fail-closed：抽取器失敗或無可抽 → 回 []，絕不丟例外、不弄壞既有記憶。
        寫入時 store.write 會自動重建索引。回傳寫入的 memory name 清單。
        去敏在 extract_memories 內完成（剝 csk-/sk-/JWT/X-ANILA-User-*/email），
        故祕密與 PII 不會落地到長期儲存（air-gap / 中科院內網要求）。
        """
        if self.extractor is None:
            return []
        transcript = f"使用者：{user_text}\n\n助理：{assistant_text}"
        mems = await extract_memories(transcript, self.store.manifest(), extractor=self.extractor)
        written: list[str] = []
        for m in mems:
            try:
                self.store.write(m)
                written.append(m.name)
            except Exception:
                continue
        return written


def build_memdir_runtime(cfg: AppConfig, *, tenant: str | None = None) -> MemdirRuntime:
    """由設定建 memdir runtime（recall 走 embed + LLM 端點）。

    多租戶分艙（中科院多人共用部署的關鍵）：

      * ``tenant=None``  → 單租戶，store 在 ``ANILA_HOME/memory``（CLI / 單人用）。
      * ``tenant`` 非空  → 多租戶，store 在 ``ANILA_HOME/memory/tenants/<tenant_slug>``，
        各租戶記憶**完全隔離**。service_wrapper 以 CSP 轉發的 X-ANILA-User-Id 當 tenant，
        故同一個共用 agent 上，A 使用者的記憶不會被 B 看到/寫到。

    tenant_slug 以 hash 防碰撞、清洗防路徑穿越；MemoryStore 內的 validate_memory_dir
    為第二層防護。
    """
    base = cfg.home / "memory"
    root = base / "tenants" / tenant_slug(tenant) if tenant else base
    store = MemoryStore(root=root)
    embed_base = os.getenv("ANILA_EMBED_BASE_URL") or cfg.model.base_url
    embed_model = os.getenv("ANILA_EMBED_MODEL", "nvidia/NV-embed-V2")
    embed_key = os.getenv("ANILA_EMBED_API_KEY") or cfg.model.api_key
    embed_fn = make_embed_fn(
        base_url=embed_base, model=embed_model, api_key=embed_key, verify_ssl=cfg.model.ssl_verify
    )
    select_fn = make_llm_select_fn(
        base_url=cfg.model.base_url,
        model=cfg.model.model,
        api_key=cfg.model.api_key,
        verify_ssl=cfg.model.ssl_verify,
    )
    extractor = make_extractor(
        base_url=cfg.model.base_url,
        model=cfg.model.model,
        api_key=cfg.model.api_key,
        verify_ssl=cfg.model.ssl_verify,
    )
    return MemdirRuntime(
        store=store, embed_fn=embed_fn, select_fn=select_fn, extractor=extractor
    )
