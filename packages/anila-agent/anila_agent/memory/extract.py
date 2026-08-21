"""端末自動抽取長期記憶（Stop-hook 風格）+ 去敏。

從對話 transcript 抽出值得長期記住的事實，存進 memdir。**存檔前去敏**：剝除
service token / X-ANILA-User-* 身分 / csk-/sk- token / Bearer JWT，避免祕密或 PII
落地到長期儲存（air-gap/中科院內網強化）。

這是抽取「引擎」；是否在端末自動觸發由操作者決定（例如以 triggers 在 turn_end 註冊，
gated by ANILA_AUTO_MEMORY=1）——預設不自動跑（多一次 LLM 呼叫）。抽取器以注入方式
解耦，失敗一律 fail-closed（回 []，不丟例外、不弄壞既有記憶）。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from anila_agent.memory.memdir import Memory
from anila_agent.memory.taxonomy import VALID_TYPES
from anila_agent.util.structured import parse_json_object

# 去敏樣式：祕密與身分，存進長期記憶前一律遮蔽。
# 容忍 JSON 引號（"X-ANILA-User-Email": "..."），並對 email / JWT 以獨立樣式兜底
# （不論是否有 Authorization/Bearer 框架，bare token 也遮）。
_SECRET_PATTERNS = [
    re.compile(r"\bcsk-[A-Za-z0-9_\-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]+"),
    re.compile(r"(?im)^.*x-anila-user-[a-z]+[\"']?\s*[:=].*$"),
    re.compile(r"(?im)^.*x-csp-service-token[\"']?\s*[:=].*$"),
    re.compile(r"(?i)authorization:\s*bearer\s+\S+"),
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),  # bare email（PII）
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),  # bare JWT
]

# extractor(redacted_transcript, existing_manifest) -> list[dict{name,description,type,body}]
Extractor = Callable[[str, dict[str, str]], Awaitable[list[dict]]]


def redact(text: str) -> str:
    """剝除祕密/身分。"""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


async def extract_memories(
    transcript: str,
    existing_manifest: dict[str, str],
    *,
    extractor: Extractor,
) -> list[Memory]:
    """去敏後抽取候選記憶；fail-closed 回 []。"""
    redacted = redact(transcript)
    try:
        raw = await extractor(redacted, existing_manifest)
    except Exception:
        return []
    out: list[Memory] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        mtype = str(item.get("type", "")).strip().lower()
        if not name or mtype not in VALID_TYPES:
            continue
        out.append(
            Memory(
                name=name,
                description=str(item.get("description", "")).strip(),
                type=mtype,
                body=redact(str(item.get("body", "")).strip()),  # body 也去敏
            )
        )
    return out


_EXTRACT_PROMPT = """你是記憶抽取器。從以下對話 transcript 抽出值得「長期記住」的事實。

只抽：使用者是誰(user)、工作方式指引(feedback)、進行中專案/目標(project)、外部資源(reference)。
不要抽：程式結構、過往修復、git history、只與本次對話相關的瑣事。
已存在的記憶（避免重複）：
{existing}

Transcript：
{transcript}

只輸出 JSON：{{"memories": [{{"name": "kebab-slug", "description": "一行摘要", "type": "user|feedback|project|reference", "body": "內文"}}]}}。
沒有值得記的就回 {{"memories": []}}。"""


def make_extractor(
    *, base_url: str, model: str, api_key: str = "EMPTY", verify_ssl: bool = True, timeout: float = 90.0
) -> Extractor:
    """建構打 ``/chat/completions`` 的抽取器（reasoning 模型：給足 max_tokens）。"""

    async def _extract(redacted_transcript: str, existing_manifest: dict[str, str]) -> list[dict]:
        import httpx

        existing = "\n".join(f"- {n}: {d}" for n, d in existing_manifest.items()) or "（無）"
        prompt = _EXTRACT_PROMPT.format(existing=existing, transcript=redacted_transcript)
        async with httpx.AsyncClient(verify=verify_ssl, timeout=timeout) as client:
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 2048,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"].get("content")
        return parse_json_object(content).get("memories", [])

    return _extract
