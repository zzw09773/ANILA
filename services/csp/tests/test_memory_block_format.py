from app.services.memory_service import _format_block

#: ``_format_block`` 的切塊上限是**必填**的關鍵字參數（沒有預設值）：真正的值由
#: 握著 session 的 ``build_memory_block`` 當場從 ``memory.max_chunk_chars`` 解出來
#: 傳進去。這一支測的是排版，不是上限，所以給一個大到不會截斷的數字。
_NO_TRUNCATION = 100_000


class _Fact:
    def __init__(self, key, value):
        self.key = key
        self.value = value


def test_preferences_split_existing_headings_preserved():
    facts = [_Fact("preference.tone", "簡潔"), _Fact("role", "工程師")]
    block = _format_block(facts, [], max_chunk_chars=_NO_TRUNCATION)
    assert "### 使用者偏好" in block            # NEW section
    assert "### 已知事實" in block              # existing heading preserved
    assert block.index("### 使用者偏好") < block.index("### 已知事實")
    assert "- **preference.tone**: 簡潔" in block
    assert "- **role**: 工程師" in block


def test_none_when_empty():
    assert _format_block([], [], max_chunk_chars=_NO_TRUNCATION) is None


def test_only_preferences_no_known_facts_section():
    block = _format_block(
        [_Fact("preference.tone", "正式")], [], max_chunk_chars=_NO_TRUNCATION
    )
    assert "### 使用者偏好" in block
    assert "### 已知事實" not in block
