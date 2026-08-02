"""prompt_gen meta-prompt 必須要求產出端勿重述共同前導。"""

from app.services.prompt_gen_service import _META_SYSTEM_PROMPT


def test_meta_system_prompt_requires_no_restate_of_common_preamble():
    assert "共同前導" in _META_SYSTEM_PROMPT
    assert "請勿重述" in _META_SYSTEM_PROMPT
    assert "身分" in _META_SYSTEM_PROMPT
    assert "語言" in _META_SYSTEM_PROMPT
    assert "國家用語" in _META_SYSTEM_PROMPT
    assert "紀年" in _META_SYSTEM_PROMPT
    assert "領域範圍" in _META_SYSTEM_PROMPT
    assert "grounding" in _META_SYSTEM_PROMPT
