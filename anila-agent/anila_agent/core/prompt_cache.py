"""Prompt cache prefix helper — P1-1 forkSubagent byte-identical prefix。

本模組對應 enhancement roadmap §4.2「forkSubagent byte-identical prefix」,
主要參考 ``src/tools/AgentTool/forkSubagent.ts``(``buildForkedMessages`` /
``buildChildMessage``)。

# 為什麼需要 byte-identical prefix

ANILA 跑 vLLM gemma4,vLLM 0.6+ 的 ``--enable-prefix-caching`` 旗標**只在
prefix bytes 完全相同時才會命中 KV cache**(沒有 explicit ``cache_control``
API)。對 long-context 部署來說,每個 sub-agent 都 re-prefill 一遍會把 GPU
時間燒光,因此 sub-agent dispatch 時必須讓 parent 已建立的 message stack 與
sub-agent 看到的 message stack 在「前 N tokens」**byte-identical**。

關鍵作法:

1. **共用 prefix 序列化** — 用 deterministic JSON(``sort_keys=True``,
   固定 separators,UTF-8 不 escape ASCII)把 parent message 前 K 條打成
   bytes。同樣的 message stack → 同樣的 bytes → 同樣的 SHA-256 hash → vLLM
   prefix cache 命中。
2. **fork / share 兩種策略**:
   - ``share``:sub-agent 沿用整段 parent context(連 assistant tool_use
     都帶上),只在尾巴追加 fork directive。對應 claude-code 的 implicit
     fork(``buildForkedMessages``)。
   - ``fork``:sub-agent 從一個 fork 截點(``fork_point``)往後重寫,
     parent prefix 仍然 byte-identical,但 sub instruction 不同 →
     hash 不同。
3. **fork directive boilerplate** — 以 ``<fork-subagent-boilerplate>``
   XML tag 包裝,讓 sub-agent 明白「你不是 main agent、不要 chit-chat、
   用 tool 直接做事」,避免 sub-agent 變 conversational mode。

# 模組與 :mod:`agent_tool` 的關係

:func:`build_subagent_prefix` / :func:`compute_prefix_hash` 是 pure helper
(不碰 LLM、不開 span);:mod:`agent_tool` 在 dispatch sub-agent 時呼叫本
模組決定 sub-agent 收到的 prompt,並把 ``prompt_cache.*`` attribute 寫進
trace span。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

# ---------------------------------------------------------------------------
# 常數 — 對齊 claude-code-src/src/tools/AgentTool/forkSubagent.ts
# ---------------------------------------------------------------------------

# fork directive 外框 XML tag。對齊 ``FORK_BOILERPLATE_TAG``;sub-agent 看到
# 此 tag 即知道自己是 fork child(用於防遞迴 fork)。
FORK_BOILERPLATE_TAG: str = "fork-subagent-boilerplate"

# fork directive 主體前綴。對齊 ``FORK_DIRECTIVE_PREFIX``;child 收到 prompt
# 後可從此前綴後面切出真正的任務描述。
FORK_DIRECTIVE_PREFIX: str = "Directive: "

# share 策略下加在 parent prefix 後面的「sub instruction」boilerplate 前綴。
# 比 fork 輕量(不需要強防遞迴,只需提示 sub-agent 接力做事)。
SHARE_INSTRUCTION_PREFIX: str = "You are continuing parent's task. Focus on: "

# 預設取 parent message stack 前幾條當共用 prefix。實務上 system + tool
# description 通常在前 1-2 條,user 對話從 2 開始;K=2 是兼顧 cache 命中
# 與 sub-agent 上下文充足的折衷預設。
DEFAULT_PREFIX_MESSAGE_COUNT: int = 2

# prefix hash 的演算法。SHA-256 對 collision-resistant 需求綽綽有餘,且
# 標準函式庫即可,不引入新 dep。
_HASH_ALGO: str = "sha256"

# ---------------------------------------------------------------------------
# 型別 alias
# ---------------------------------------------------------------------------

# Message 的最小 schema — dict 形式;對齊 OpenAI / Anthropic Messages API,
# 包含 ``role`` 與 ``content``(可為 str / list[dict])。我們不限制其他自訂
# 欄位,但**所有序列化都會 ``sort_keys=True``**,確保同 message → 同 bytes。
Message = Mapping[str, Any]

# fork / share 兩種 prefix 策略。對應 :class:`AgentTool` 上的同名欄位。
PrefixStrategyName = Literal["share", "fork"]


# ---------------------------------------------------------------------------
# 結果 dataclass — sub-agent dispatch 需要的所有 prompt-cache 資訊
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubagentPrefix:
    """:func:`build_subagent_prefix` 的回傳值。

    提供三組資訊:

    * ``prefix_bytes`` — parent 與 sub-agent 共用、byte-identical 的前綴
      bytes。**這段 bytes 在 vLLM 上會被 prefix cache 命中**。
    * ``prefix_hash`` — ``prefix_bytes`` 的 SHA-256 hex digest。同 hash →
      同 cache slot(verify 用)。
    * ``messages`` — sub-agent 實際送 LLM 的 message list,前段與 parent
      的 ``messages[:prefix_message_count]`` 完全一致,尾段為 fork / share
      對應的 sub instruction message。
    * ``fork_directive`` — sub-agent 看到的 directive 字串(已包過
      boilerplate / share 前綴),方便 trace 直接記錄。
    * ``prefix_message_count`` — 真正納入 prefix 的 parent message 條數。
      ``share`` 策略 = 全部 parent messages;``fork`` 策略 = ``fork_point``。

    Attributes:
        strategy: 採用的策略名稱(``"share"`` / ``"fork"``)。
        prefix_bytes: 共用 prefix 的 deterministic JSON bytes。
        prefix_hash: ``prefix_bytes`` 的 SHA-256 hex digest。
        prefix_message_count: 納入 prefix 的 parent message 條數。
        messages: sub-agent 完整 message list(prefix + sub instruction)。
        fork_directive: sub-agent 收到的 directive 字串。
    """

    strategy: PrefixStrategyName
    prefix_bytes: bytes
    prefix_hash: str
    prefix_message_count: int
    messages: tuple[Message, ...]
    fork_directive: str


# ---------------------------------------------------------------------------
# Deterministic serialization — sort_keys + 固定 separators
# ---------------------------------------------------------------------------


def _canonical_dumps(obj: Any) -> str:
    """以 deterministic 設定把 obj 序列化為 JSON 字串。

    關鍵設定:

    * ``sort_keys=True`` — 同 dict 不同插入順序 → 同字串。
    * ``separators=(",", ":")`` — 去掉預設的 ``", "`` / ``": "`` 空白,
      避免 Python 版本或 platform 差異影響 bytes。
    * ``ensure_ascii=False`` — 中文 / Unicode 直接保留原字元(byte 角度
      仍然 deterministic — UTF-8 encoding 對同字元唯一)。

    不使用 ``default=str`` 之類的 fallback;若 obj 內含無法序列化的型別
    (如 datetime)應在呼叫前先正規化,避免 hash 變動。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _serialize_messages(messages: Iterable[Message]) -> bytes:
    """把 message iterable 序列化為 deterministic UTF-8 bytes。

    同樣 messages(內容相同、key 順序不同也可) → 同樣 bytes。
    """
    payload = [dict(m) for m in messages]
    return _canonical_dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# Hash helper — verify cache slot 一致
# ---------------------------------------------------------------------------


def compute_prefix_hash(messages: Sequence[Message]) -> str:
    """計算 message sequence 的 SHA-256 hex digest。

    同 input(內容相同;dict key 順序不影響) → 同 hash;
    任一 message 內容變動 → hash 變。用於:

    * dispatch 時把 ``prompt_cache.prefix_hash`` 寫進 trace span,事後
      聚合即可看出 vLLM cache slot 是否真的被多個 dispatch 共用。
    * 測試 verify deterministic serialization 真的成立。

    Args:
        messages: 要 hash 的 message sequence(任意長度;空也允許)。

    Returns:
        SHA-256 hex digest(64 字 hex 字串)。
    """
    digest = hashlib.new(_HASH_ALGO)
    digest.update(_serialize_messages(messages))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# fork / share child message 組裝
# ---------------------------------------------------------------------------


def build_fork_directive_message(directive: str) -> str:
    """把 sub-agent 的 directive 包成 fork boilerplate 字串。

    對齊 ``forkSubagent.ts::buildChildMessage``。產生內容:

    * 外框 XML tag ``<fork-subagent-boilerplate>``,告訴 child「你是 fork
      worker、不要 chit-chat、不要再 spawn sub-agent、用 tool 直接做事、
      改檔要 commit、output 用結構化標籤」。
    * 接 ``Directive: <真正的任務描述>``。

    本字串會被 child agent 當第一個 user message;tag 也用於
    :func:`is_in_fork_child` 偵測「我已經是 fork 了,不要再 fork」。

    Args:
        directive: 來自 parent 的任務描述(已 strip)。

    Returns:
        完整 fork child prompt 字串。
    """
    return (
        f"<{FORK_BOILERPLATE_TAG}>\n"
        "STOP. READ THIS FIRST.\n\n"
        "You are a forked worker process. You are NOT the main agent.\n\n"
        "RULES (non-negotiable):\n"
        "1. Do NOT spawn sub-agents; execute the directive directly.\n"
        "2. Do NOT converse, ask questions, or suggest next steps.\n"
        "3. Do NOT editorialize or add meta-commentary.\n"
        "4. USE your tools directly (Read, Bash, Write, etc.).\n"
        "5. If you modify files, commit and include the commit hash in the report.\n"
        "6. Do NOT emit text between tool calls; report once at the end.\n"
        "7. Stay strictly within scope; mention adjacent systems in one sentence max.\n"
        "8. Keep the report concise and factual.\n"
        "9. Your response MUST begin with 'Scope:'.\n"
        "10. Report structured facts, then stop.\n\n"
        "Output format (plain text labels, not markdown headers):\n"
        "  Scope: <one-sentence echo of your assigned scope>\n"
        "  Result: <the answer or key findings>\n"
        "  Key files: <relevant paths — for research tasks>\n"
        "  Files changed: <list with commit hash — only if you modified files>\n"
        "  Issues: <list — only if there are issues to flag>\n"
        f"</{FORK_BOILERPLATE_TAG}>\n\n"
        f"{FORK_DIRECTIVE_PREFIX}{directive}"
    )


def build_share_directive_message(directive: str) -> str:
    """share 策略下的 sub instruction prompt(輕量版,無 boilerplate)。

    share 策略沿用 parent context,sub-agent 本來就看得到 parent 的整條
    對話歷史,因此只要一句 instruction 告訴 sub-agent 焦點即可。

    Args:
        directive: 來自 parent 的任務描述。

    Returns:
        share 模式的 sub instruction 字串。
    """
    return f"{SHARE_INSTRUCTION_PREFIX}{directive}"


def is_in_fork_child(messages: Iterable[Message]) -> bool:
    """偵測 messages 內是否已含 fork boilerplate(代表已在 fork child 內)。

    用於防遞迴 fork — 對齊 ``forkSubagent.ts::isInForkChild``。
    fork child 收到的 user message 會帶 ``<fork-subagent-boilerplate>``
    tag,只要任一 user message 的 text content 含此 tag 就視為已在 fork。

    支援兩種 content 形式:

    * ``content`` 為 str(OpenAI legacy):直接 substring 比對。
    * ``content`` 為 list[dict](Anthropic Messages):iterate 每個 block,
      若 ``type == "text"`` 就比對 ``text`` 欄位。

    Args:
        messages: 要檢查的 message iterable。

    Returns:
        True 若任一 user message 含 fork boilerplate tag。
    """
    target = f"<{FORK_BOILERPLATE_TAG}>"
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if target in content:
                return True
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, Mapping):
                    continue
                if block.get("type") == "text" and target in str(block.get("text", "")):
                    return True
    return False


# ---------------------------------------------------------------------------
# 主入口 — build_subagent_prefix
# ---------------------------------------------------------------------------


def build_subagent_prefix(
    parent_messages: Sequence[Message],
    *,
    strategy: PrefixStrategyName,
    directive: str,
    fork_point: int | None = None,
) -> SubagentPrefix:
    """從 parent message stack 建一份 sub-agent 用的 byte-identical prefix。

    這是 P1-1 的核心 entry point。流程:

    1. 依 ``strategy`` 決定納入 prefix 的 parent message 條數
       (``share`` = 全部、``fork`` = ``fork_point``;
       後者預設用 :data:`DEFAULT_PREFIX_MESSAGE_COUNT`)。
    2. 用 :func:`_serialize_messages` deterministic 序列化前綴 → bytes。
    3. 算 SHA-256 hex digest → ``prefix_hash``。
    4. 組 sub instruction(fork 用 boilerplate、share 用輕量 prompt)。
    5. 把「prefix messages + sub instruction message」拼成完整 ``messages``。

    Args:
        parent_messages: parent 已建立的 message list(任意長度;空也允許)。
        strategy: ``"share"`` 或 ``"fork"``。
        directive: sub-agent 的任務描述(會被組進 sub instruction message)。
        fork_point: ``fork`` 策略下截斷 parent prefix 的 index。None 則用
            :data:`DEFAULT_PREFIX_MESSAGE_COUNT`,且若大於 parent 長度則
            自動 clamp 到 parent 長度;``share`` 策略下此參數被忽略。

    Returns:
        :class:`SubagentPrefix` — 包含 prefix bytes / hash / sub-agent
        messages / directive。

    Raises:
        ValueError: 若 ``strategy`` 不是 ``"share"`` / ``"fork"``,或
            ``directive`` 為空字串。
    """
    if strategy not in ("share", "fork"):
        raise ValueError(f"unknown prefix strategy: {strategy!r}")
    if not directive or not directive.strip():
        raise ValueError("directive must be a non-empty string")

    parent_count = len(parent_messages)

    if strategy == "share":
        # share — 沿用整段 parent context。
        prefix_count = parent_count
        sub_instruction_text = build_share_directive_message(directive)
    else:
        # fork — 在 fork_point 切斷;預設 DEFAULT_PREFIX_MESSAGE_COUNT。
        effective_fork_point = (
            fork_point if fork_point is not None else DEFAULT_PREFIX_MESSAGE_COUNT
        )
        if effective_fork_point < 0:
            raise ValueError(
                f"fork_point must be non-negative, got {effective_fork_point}"
            )
        # clamp 到 parent_count(allow ≤ parent_count;> 則 fallback)
        prefix_count = min(effective_fork_point, parent_count)
        sub_instruction_text = build_fork_directive_message(directive)

    prefix_messages: tuple[Message, ...] = tuple(parent_messages[:prefix_count])
    prefix_bytes = _serialize_messages(prefix_messages)
    prefix_hash = hashlib.new(_HASH_ALGO, prefix_bytes).hexdigest()

    sub_instruction_message: Message = {
        "role": "user",
        "content": sub_instruction_text,
    }

    return SubagentPrefix(
        strategy=strategy,
        prefix_bytes=prefix_bytes,
        prefix_hash=prefix_hash,
        prefix_message_count=prefix_count,
        messages=prefix_messages + (sub_instruction_message,),
        fork_directive=sub_instruction_text,
    )


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

__all__ = [
    "DEFAULT_PREFIX_MESSAGE_COUNT",
    "FORK_BOILERPLATE_TAG",
    "FORK_DIRECTIVE_PREFIX",
    "Message",
    "PrefixStrategyName",
    "SHARE_INSTRUCTION_PREFIX",
    "SubagentPrefix",
    "build_fork_directive_message",
    "build_share_directive_message",
    "build_subagent_prefix",
    "compute_prefix_hash",
    "is_in_fork_child",
]
