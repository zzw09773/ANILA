"""把開頭是 thought／thinking 的外漏推理從可見回答裡分開。

Gemma 一類模型有時不聽「不要把思考寫進 content」的規則，把分析直接
寫在回答最前面。這裡的切法與 Router 原本的 ``_sanitize_leaked_thought``
相同，CSP 與 Router 共用，避免兩套規則把同一段文字切出不同結果。
"""

from __future__ import annotations

import re

# Matches a "thought" / "thinking" line at the very start of content.
THOUGHT_PREFIX_RE = re.compile(
    r"^\s*(?:\*{0,2}|`)?(?:thought|thinking)(?:\*{0,2}|`)?\s*[:：]?\s*(?:\n|$)",
    re.IGNORECASE,
)

CJK_RE = re.compile(r"[一-鿿]")

# 沒有穩定的中文回答可切時，整段外漏推理都不進可見文字。
LEAKED_THOUGHT_PLACEHOLDER = (
    "（Router 已完成分析但未能自動萃取最終回覆，請展開上方「思考過程」檢視。）"
)


def sanitize_leaked_thought(content: str, reasoning: str | None) -> tuple[str, str]:
    """Split leaked thought-prefixed content into (answer, reasoning).

    Observed structure for gemma-class models that ignore the no-CoT rule:
      ``thought\\n<English-dominant analysis, possibly with blank lines>\\n
      <optional handoff marker>\\n<long CJK answer block>``

    The thought/answer boundary is unreliable when approached as a single
    marker (models vary: some leave a blank line, some glue ``.aggression.首先``
    directly). The one stable invariant across all observed samples is:
      - thought is English-dominant
      - the final answer is a sustained CJK block

    Algorithm:
      1. If content doesn't start with "thought/thinking", passthrough — this
         covers gpt-oss (reasoning already in its own field) and any
         well-behaved model.
      2. Scan forward for the first CJK character whose 80-char lookahead
         contains ≥ 20 CJK characters. That's the start of the sustained
         answer block.
      3. Rewind to the nearest clean break before it: previous blank line,
         newline, or sentence-terminator — whichever is closest. This pulls
         the final handoff sentence (``Decision: Reply directly.`` or the
         English concluding sentence) out of the user-visible answer.
      4. If no sustained CJK block is found, dump the entire leak into
         reasoning with a placeholder answer so the UI isn't empty.
    """
    reasoning = (reasoning or "").strip()
    if not content or not THOUGHT_PREFIX_RE.match(content):
        return content, reasoning

    window = 80
    split_at = -1
    for match in CJK_RE.finditer(content):
        index = match.start()
        if index < 10:  # still inside the "thought" header
            continue
        lookahead = content[index : index + window]
        cjk_count = len(CJK_RE.findall(lookahead))
        # Require both ≥50% density *and* ≥20 absolute CJK chars. The
        # minimum count rejects short CJK tails — e.g. Gemma echoing the
        # user's 5-char query ("顯示參數表") after a broken DISPATCH line.
        if cjk_count >= 20 and cjk_count * 2 >= len(lookahead):
            split_at = index
            break

    if split_at > 0:
        # Pull leading markdown markers (bold/heading/list) back into answer.
        cursor = split_at
        while cursor > 0 and content[cursor - 1] in "*#":
            cursor -= 1
        # A hyphen list marker needs a trailing space to qualify.
        if cursor >= 2 and content[cursor - 2 : cursor] in ("- ", "+ "):
            cursor -= 2
        split_at = cursor
        thought = content[:split_at].rstrip()
        answer = content[split_at:].strip()
        if answer and thought:
            merged = (reasoning + "\n\n" + thought).strip() if reasoning else thought
            return answer, merged

    merged = (reasoning + "\n\n" + content).strip() if reasoning else content
    return LEAKED_THOUGHT_PLACEHOLDER, merged
