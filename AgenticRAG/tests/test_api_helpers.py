"""Unit tests for AgenticRAG's pure helpers in ``api.py``.

Scope: functions that do **no** I/O — no DB, no LLM, no HTTP. The hybrid-search
functions (``_vector_search`` / ``_keyword_search`` / ``retrieve_context``) need
a live pgvector + embedding endpoint to exercise meaningfully, so they're left
to real integration runs in forks.

Covered here:
  - ``_expand_tokens``   — 中文法條字間空格變體展開
  - ``_rrf_merge``       — vector + keyword rank fusion 排序
  - ``inject_context``   — str / list-of-parts content 兩種格式注入
  - ``_last_user_text``  — 反向搜尋最後一則 user message
"""

from __future__ import annotations

import pytest

import api as rag_api


# ── _expand_tokens ───────────────────────────────────────────────────────────


class TestExpandTokens:
    def test_single_token_produces_spaced_variant(self) -> None:
        out = set(rag_api._expand_tokens("第8條"))
        # original + character-spaced form always present
        assert "第8條" in out
        assert "第 8 條" in out

    def test_multi_token_keeps_individual_tokens(self) -> None:
        out = set(rag_api._expand_tokens("刑法 第8條"))
        assert "刑法 第8條" in out              # original whole query
        assert "刑法" in out                     # individual token
        assert "第8條" in out                    # individual token
        assert "第 8 條" in out                  # spaced variant of multi-char token

    def test_empty_query_has_empty_string_variant(self) -> None:
        out = rag_api._expand_tokens("")
        # empty input returns [""] (only the base, stripped)
        assert out == [""]

    def test_single_char_tokens_not_spaced(self) -> None:
        # 1-char tokens shouldn't get a " ".join applied (nothing to space)
        out = set(rag_api._expand_tokens("A B"))
        assert "A B" in out
        # "A" alone is 1 char — not added as individual (len > 1 filter)
        assert "A" not in out
        assert "B" not in out


# ── _rrf_merge ───────────────────────────────────────────────────────────────


def _vec_row(chunk_id: str, score: float, content: str = "v", meta: dict | None = None) -> dict:
    return {
        "chunk_id": chunk_id,
        "content": content,
        "metadata": meta or {},
        "score": score,
    }


def _kw_row(chunk_id: str, content: str = "k", meta: dict | None = None) -> dict:
    return {
        "chunk_id": chunk_id,
        "content": content,
        "metadata": meta or {},
    }


class TestRrfMerge:
    def test_empty_inputs_return_empty(self) -> None:
        assert rag_api._rrf_merge([], [], top_k=5) == []

    def test_vector_only_preserves_order(self) -> None:
        rows = [_vec_row(f"c{i}", 0.9 - i * 0.1) for i in range(3)]
        out = rag_api._rrf_merge(rows, [], top_k=5)
        assert [r["vec_score"] for r in out] == pytest.approx([0.9, 0.8, 0.7])
        # vector-only → kw_match is False
        assert all(r["kw_match"] is False for r in out)

    def test_keyword_only_marks_kw_match_true(self) -> None:
        rows = [_kw_row(f"c{i}") for i in range(2)]
        out = rag_api._rrf_merge([], rows, top_k=5)
        assert len(out) == 2
        assert all(r["kw_match"] is True for r in out)
        assert all(r["vec_score"] is None for r in out)

    def test_overlap_boosts_rrf_score(self) -> None:
        """A chunk appearing in both lists should outrank one appearing in only one."""
        vec = [_vec_row("shared", 0.5), _vec_row("vec_only", 0.9)]
        kw = [_kw_row("shared"), _kw_row("kw_only")]
        out = rag_api._rrf_merge(vec, kw, top_k=5)
        top = out[0]
        assert top["content"]  # sanity
        # The "shared" chunk picks up contributions from rank 1 vec + rank 1 kw
        # → 2 * 1/(60+1) = ~0.0328, vs vec_only 1/61 ~ 0.0164 alone.
        assert top["kw_match"] is True
        assert top["vec_score"] == pytest.approx(0.5)

    def test_top_k_truncation(self) -> None:
        vec = [_vec_row(f"c{i}", 0.9 - i * 0.01) for i in range(10)]
        out = rag_api._rrf_merge(vec, [], top_k=3)
        assert len(out) == 3


# ── inject_context ───────────────────────────────────────────────────────────


class TestInjectContext:
    def test_injects_into_last_user_string(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "q2"},
        ]
        out = rag_api.inject_context(msgs, "CTX")
        # only the LAST user message is modified
        assert out[1]["content"] == "hi"
        assert out[3]["content"] == "CTX\n\nq2"

    def test_injects_into_list_content(self) -> None:
        msgs = [
            {"role": "user", "content": [
                {"type": "text", "text": "part1"},
                {"type": "image_url", "image_url": {"url": "x"}},
            ]},
        ]
        out = rag_api.inject_context(msgs, "CTX")
        parts = out[0]["content"]
        assert parts[0] == {"type": "text", "text": "CTX\n\n"}
        assert parts[1]["type"] == "text"          # original part moved
        assert parts[2]["type"] == "image_url"

    def test_no_user_message_returns_copy_unchanged(self) -> None:
        msgs = [{"role": "system", "content": "s"}, {"role": "assistant", "content": "a"}]
        out = rag_api.inject_context(msgs, "CTX")
        assert out == msgs
        assert out is not msgs  # defensive copy

    def test_does_not_mutate_input(self) -> None:
        msgs = [{"role": "user", "content": "orig"}]
        snapshot = {"role": "user", "content": "orig"}
        rag_api.inject_context(msgs, "CTX")
        assert msgs[0] == snapshot


# ── _last_user_text ──────────────────────────────────────────────────────────


class TestLastUserText:
    def test_returns_last_user_string(self) -> None:
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "..."},
            {"role": "user", "content": "latest"},
        ]
        assert rag_api._last_user_text(msgs) == "latest"

    def test_extracts_text_from_list_parts(self) -> None:
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "text", "text": "world"},
        ]}]
        assert rag_api._last_user_text(msgs) == "hello world"

    def test_no_user_returns_empty(self) -> None:
        msgs = [{"role": "system", "content": "s"}, {"role": "assistant", "content": "a"}]
        assert rag_api._last_user_text(msgs) == ""

    def test_strips_whitespace(self) -> None:
        msgs = [{"role": "user", "content": "  spaced  "}]
        assert rag_api._last_user_text(msgs) == "spaced"

    def test_ignores_non_text_parts(self) -> None:
        msgs = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "x"}},
        ]}]
        assert rag_api._last_user_text(msgs) == ""
