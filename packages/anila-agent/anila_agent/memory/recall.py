"""memdir recall：embed 粗篩 + LLM 精選，fail-closed 退場鏈（依 live 實測設計）。

實測（gpt-oss-20b / NV-embed-V2 / gemma4，25 真實記憶 + 12 查詢）：
  gpt-oss 選擇器 recall@5=1.0、precision 高；NV-embed embedding recall@5=0.96、最快但雜訊多；
  gemma4 選擇器 0.5 墊底。故採兩階段：
    stage1 = NV-embed 粗篩（快、召回高）→ top-N 候選
    stage2 = gpt-oss 精選（小清單 precision 高）→ 最終 <=k
退場鏈：select 掛 → 退 embed 粗篩；embed 掛 → 退 frontmatter 關鍵字掃描。
所有失敗 fail-closed（回保守子集，永不丟例外）。

引擎以注入的 ``embed_fn`` / ``select_fn`` 解耦（可測）；live 後端工廠見下方。
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable

from anila_agent.util.structured import parse_json_object

# embed_fn(query, manifest, n) -> 候選 name 清單（已依相關度排序）
EmbedFn = Callable[[str, dict[str, str], int], Awaitable[list[str]]]
# select_fn(query, sub_manifest, k) -> 最終 name 清單（<=k）
SelectFn = Callable[[str, dict[str, str], int], Awaitable[list[str]]]

_CJK = re.compile(r"[一-鿿぀-ヿ]")  # 中日韓統一表意 + 假名
_WORD = re.compile(r"[A-Za-z0-9]+")


def _tokens(text: str) -> set[str]:
    """CJK 逐字 + ASCII 詞。整串 CJK 當單一 token 會讓子字串重疊失效，故逐字切。"""
    chars = set(_CJK.findall(text))
    words = {w.lower() for w in _WORD.findall(text)}
    return chars | words


def keyword_shortlist(query: str, manifest: dict[str, str], n: int) -> list[str]:
    """關鍵字 token 重疊排序（最終退場）。"""
    q = _tokens(query)
    if not q:
        return list(manifest)[:n]
    scored = sorted(
        manifest.items(),
        key=lambda kv: len(q & _tokens(f"{kv[0]} {kv[1]}")),
        reverse=True,
    )
    return [name for name, _ in scored[:n]]


async def recall(
    query: str,
    manifest: dict[str, str],
    *,
    embed_fn: EmbedFn | None = None,
    select_fn: SelectFn | None = None,
    k: int = 5,
    shortlist: int = 8,
) -> list[str]:
    """回傳與 query 最相關的記憶 name（<=k），fail-closed。"""
    if not manifest:
        return []

    # stage1 粗篩
    names: list[str] = []
    if embed_fn is not None:
        try:
            names = [n for n in await embed_fn(query, manifest, shortlist) if n in manifest]
        except Exception:
            names = []
    if not names:
        names = keyword_shortlist(query, manifest, shortlist)

    # stage2 精選
    if select_fn is not None and names:
        sub = {n: manifest[n] for n in names}
        try:
            picked = [n for n in await select_fn(query, sub, k) if n in sub]
            if picked:
                return picked[:k]
        except Exception:
            pass
    return names[:k]


# ---- live 後端工廠（hit NV-embed + gpt-oss 端點）----

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


def _sorted_by_index(items: list) -> list:
    """依 ``data[].index`` 排序，使回應對齊回請求 ``input`` 的原始位置。

    OpenAI 相容的批次 embeddings 端點不保證 ``data[]`` 陣列順序等於 ``input``
    順序，只保證每個 item 帶的 ``index`` 欄位指回原始位置。缺 ``index``
    （非標準/舊端點）時退回原陣列順序（``sorted`` 為穩定排序，此時每個 key
    即原始位置本身，等同不動）。
    """
    return [
        item
        for _, item in sorted(
            enumerate(items),
            key=lambda pair: (
                pair[1]["index"]
                if isinstance(pair[1], dict) and "index" in pair[1]
                else pair[0]
            ),
        )
    ]


def make_embed_fn(
    *, base_url: str, model: str, api_key: str = "EMPTY", verify_ssl: bool = True, timeout: float = 30.0
) -> EmbedFn:
    """建構打 ``/embeddings`` 的粗篩 embed_fn。"""

    async def _embed(query: str, manifest: dict[str, str], n: int) -> list[str]:
        import httpx

        names = list(manifest)
        inputs = [query, *[manifest[name] for name in names]]
        async with httpx.AsyncClient(verify=verify_ssl, timeout=timeout) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "input": inputs},
            )
            resp.raise_for_status()
            # data[] 陣列順序不保證與 input 對齊；依 index 排序後再對齊
            # （缺 index 時退回陣列順序）。
            vectors = [d["embedding"] for d in _sorted_by_index(resp.json()["data"])]
        q_vec, doc_vecs = vectors[0], vectors[1:]
        ranked = sorted(
            zip(names, doc_vecs, strict=False), key=lambda nv: _cosine(q_vec, nv[1]), reverse=True
        )
        return [name for name, _ in ranked[:n]]

    return _embed


_SELECT_PROMPT = """你是記憶檢索選擇器。下面是候選記憶（name + 描述）。
依使用者查詢，挑出最多 {k} 條最相關的記憶。

候選：
{manifest}

查詢：{query}

只輸出 JSON：{{"names": ["name1", ...]}}。names 只能是候選裡出現過的 name，最多 {k} 個。"""


def make_llm_select_fn(
    *, base_url: str, model: str, api_key: str = "EMPTY", verify_ssl: bool = True, timeout: float = 60.0
) -> SelectFn:
    """建構打 ``/chat/completions`` 的精選 select_fn（reasoning 模型：給足 max_tokens）。"""

    async def _select(query: str, sub_manifest: dict[str, str], k: int) -> list[str]:
        import httpx

        manifest_text = "\n".join(f"- {n}: {d}" for n, d in sub_manifest.items())
        prompt = _SELECT_PROMPT.format(k=k, manifest=manifest_text, query=query)
        async with httpx.AsyncClient(verify=verify_ssl, timeout=timeout) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 1024,  # reasoning 模型下限
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"].get("content")
        names = parse_json_object(content).get("names", [])
        return [n for n in names if isinstance(n, str)][:k]

    return _select
