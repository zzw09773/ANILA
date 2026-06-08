"""Lenient JSON extraction from noisy LLM responses.

Extracted from ``api/studio.py`` as part of the god-module split. This is
the canonical version (studio.py's, the most complete + documented). The
studio / datatables / infographics / mindmaps API modules each grew their
own divergent copy; they can migrate to import from here to converge.

Mirrors ANILALM's frontend ``extractJsonObject``.
"""

from __future__ import annotations

import json
import re
from typing import Any

_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", re.DOTALL | re.IGNORECASE,
)


def extract_json_object(raw: str) -> str:
    """Slice the *last* balanced JSON object out of a noisy LLM response.

    Why "last balanced" and not "first { to last }":
      - gemma4 / qwen / oss models often emit a "thought" preamble that
        contains literal JSON examples like ``{"title": "..."}`` — the
        naive ``find('{')`` lands inside that example, the naive
        ``rfind('}')`` lands at the end of the real answer, and the slice
        glues two unrelated regions together.
      - Walking braces from the end finds the FINAL top-level ``{...}``
        which is virtually always the actual answer (LLMs put their
        decision at the end, after reasoning).

    Implementation: skip ``<think>``/```` ``` `` blocks first to remove
    the most common forms of structured noise, then scan from the right
    counting brace nesting until we hit depth 0.
    """
    de_thought = _THINK_BLOCK_RE.sub("", raw)
    no_fences = (
        de_thought.replace("```json", "")
        .replace("```JSON", "")
        .replace("```", "")
        .strip()
    )

    end = no_fences.rfind("}")
    if end == -1:
        raise ValueError(
            f"Model response contained no closing brace. First 80: "
            f"{raw[:80]!r}".replace("\n", "⏎")
        )

    # Walk leftward from the closing brace, counting nesting. We respect
    # JSON string delimiters so braces inside `"..."` don't fool the
    # depth counter. Escape sequences (\\, \") are handled with a
    # one-position lookahead.
    depth = 0
    in_string = False
    i = end
    while i >= 0:
        ch = no_fences[i]
        if in_string:
            if ch == '"' and (i == 0 or no_fences[i - 1] != "\\"):
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "}":
                depth += 1
            elif ch == "{":
                depth -= 1
                if depth == 0:
                    return no_fences[i : end + 1]
        i -= 1
    raise ValueError(
        f"Model response had unbalanced braces. First 80: "
        f"{raw[:80]!r}".replace("\n", "⏎")
    )


def loads_lenient(text: str) -> Any:
    """``json.loads`` plus a one-shot single-quote-to-double-quote repair.

    gemma4 (and friends) sometimes emit Python-dict-style output:
        {'title': "x", 'slides': []}
    which strict ``json.loads`` rejects (line 1 col 2 error). The repair
    only flips quote characters that look like JSON delimiters
    (preceded by ``[``, ``{``, ``,``, ``:`` or whitespace) so apostrophes
    inside values aren't accidentally converted. If even that fails,
    we let json.JSONDecodeError propagate so the correction pass can
    re-prompt.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Replace `'` only at delimiter positions. Limited regex pass:
    # opening `'` after [{,:\s, closing `'` before ]},:\s.
    repaired = re.sub(r"(?<=[\[\{,:\s])'", '"', text)
    repaired = re.sub(r"'(?=[\]\},:\s]|$)", '"', repaired)
    return json.loads(repaired)
