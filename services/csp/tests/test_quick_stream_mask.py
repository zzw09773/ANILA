"""No already-emitted credential prefix can become sensitive retrospectively."""
from __future__ import annotations

import json

import pytest

from app.services.quick_stream_mask import (
    MAX_BUFFER_CHARS,
    MAX_TOTAL_CHARS,
    MAX_VISIBLE_CHARS,
    OutputMaskError,
    OutputMaskLimitError,
    OutputMasker,
)


CASES = [
    ("可公開的說明。Bearer vault-super-secret 維護資訊", "vault-super-secret"),
    ("可公開的說明。bEaReR vault-super-secret", "vault-super-secret"),
    ("憑證資料 eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.signatureABC", "eyJhbGciOiJSUzI1NiJ9"),
    ("憑證資料 abcde.fghij.klmnop", "abcde.fghij"),
    ("個人資料 A123456789。", "A123456789"),
    ("聯絡方式 0912345678。", "0912345678"),
    ("聯絡方式 09١٢٣٤٥٦٧٨。", "09١٢٣٤٥٦٧٨"),
    ("password=vault-super-secret，其他內容", "vault-super-secret"),
    ('password = "vault-super-secret with spaces" 後續說明', "vault-super-secret"),
    ("password='vault-super-secret 尚未閉合", "vault-super-secret"),
    ("token : vault-super-secret", "vault-super-secret"),
    ("api_key=vault-super-secret", "vault-super-secret"),
    ("API-KEY = vault-super-secret", "vault-super-secret"),
    ("api_Key=vault-super-secret", "vault-super-secret"),
    ("employee_id:vault-employee-999999", "vault-employee-999999"),
    ("員工編號：employee-999999", "employee-999999"),
    ("密碼：vault-super-secret", "vault-super-secret"),
    ("憑證=vault-super-secret", "vault-super-secret"),
    ("Authorization: Bearer vault-super-secret", "vault-super-secret"),
    ('{"outer":[{"password":"vault-super-secret"}],"ok":"可讀"}', "vault-super-secret"),
    ('說明後的資料：{"token":"vault-super-secret"}', "vault-super-secret"),
    ('{"outer":"{\\"employee_id\\":\\"employee-999999\\"}"}', "employee-999999"),
    ('"\\u007b\\"password\\":\\"vault-super-secret\\"\\u007d"', "vault-super-secret"),
    ('{"password":"vault-super-secret', "vault-super-secret"),
]


def collect(parts):
    masker = OutputMasker()
    emitted = [masker.feed(piece) for piece in parts]
    emitted.append(masker.finish())
    return "".join(emitted)


@pytest.mark.parametrize("text,secret", CASES)
def test_every_character_cut_never_releases_sensitive_values(text, secret):
    masker = OutputMasker()
    output = ""
    for char in text:
        output += masker.feed(char)
        assert secret not in output
        assert secret[:6] not in output
    output += masker.finish()
    assert secret not in output
    assert secret[:6] not in output
    assert "遮罩" in output


@pytest.mark.parametrize("text,secret", CASES)
def test_every_two_chunk_boundary_is_safe_and_partition_independent(text, secret):
    expected = collect([text])
    for boundary in range(len(text) + 1):
        result = collect([text[:boundary], text[boundary:]])
        assert result == expected
        assert secret not in result


def test_plain_traditional_chinese_is_live_and_exact():
    text = "請先確認申請範圍，再依院內規章辦理。完成後請保留核准紀錄。"
    masker = OutputMasker()
    pieces = [masker.feed(char) for char in text]
    assert "".join(pieces) == text
    assert masker.finish() == ""


def test_normal_ascii_words_only_wait_for_their_boundary():
    masker = OutputMasker()
    assert masker.feed("Hello") == ""
    assert masker.feed(" world。").startswith("Hello world")
    assert masker.finish() == ""


@pytest.mark.parametrize("prefix", ["password=", "password = '", "Bearer ", "密碼："])
def test_long_secret_never_leaks_a_fixed_suffix_window(prefix):
    secret = "UNIQUEVAULT" + "z" * 12000
    masker = OutputMasker()
    assert masker.feed("一般可公開內容。" + prefix) == "一般可公開內容。"
    published = ""
    for offset in range(0, len(secret), 7):
        published += masker.feed(secret[offset : offset + 7])
        assert "UNIQUEVAULT" not in published
        assert masker.buffered_chars <= MAX_BUFFER_CHARS
    published += masker.finish()
    assert "UNIQUEVAULT" not in published
    assert "zzzzzz" not in published
    assert "遮罩" in published


def test_arbitrarily_nested_json_is_quarantined_without_recursion():
    text = "[" * 1000 + '{"password":"vault-super-secret"}' + "]" * 1000
    masker = OutputMasker()
    assert masker.feed(text) == ""
    assert "vault-super-secret" not in masker.finish()


def test_json_string_nested_json_string_is_also_quarantined():
    text = json.dumps(json.dumps({"outer": {"api_key": "vault-super-secret"}}))
    assert "vault-super-secret" not in collect(list(text))


def test_potential_key_prefix_is_not_emitted_until_it_is_disambiguated():
    masker = OutputMasker()
    assert masker.feed("p") == ""
    assert masker.feed("ass") == ""
    assert masker.feed("word") == ""
    assert masker.feed("   ") == ""
    assert masker.feed("=") == ""
    assert masker.feed("vault-super-secret") == ""
    assert masker.finish() == "password   =[已遮罩]"


def test_harmless_key_mentions_can_resume_without_secret_assignment():
    text = "請確認密碼政策並檢視 token 用量。"
    masker = OutputMasker()
    output = "".join(masker.feed(char) for char in text)
    assert output == text
    assert masker.finish() == ""


@pytest.mark.parametrize(
    "value,marker",
    [
        ("A123456789", "[身分證字號已遮罩]"),
        ("0912345678", "[手機已遮罩]"),
        ("eyJabc.def.ghi", "[權杖已遮罩]"),
    ],
)
def test_no_partial_identity_or_token_prefix_is_published(value, marker):
    masker = OutputMasker()
    assert all(masker.feed(char) == "" for char in value)
    assert masker.feed("。") == marker + "。"
    assert masker.finish() == ""


def test_total_and_pending_limits_fail_typed_without_truncation_or_flush():
    masker = OutputMasker()
    assert masker.feed("{" + "z" * (MAX_TOTAL_CHARS - 1)) == ""
    assert masker.total_chars == MAX_TOTAL_CHARS
    assert masker.buffered_chars == MAX_BUFFER_CHARS
    with pytest.raises(OutputMaskLimitError) as exc:
        masker.feed("z")
    assert exc.value.code == "OUTPUT_TOO_LARGE"
    assert "zzzz" not in str(exc.value)
    assert masker.buffered_chars == 0
    with pytest.raises(OutputMaskError):
        masker.finish()


def test_visible_limit_matches_batch_disclosure_ceiling_and_does_not_truncate():
    masker = OutputMasker()
    assert masker.feed("中" * MAX_VISIBLE_CHARS) == "中" * MAX_VISIBLE_CHARS
    with pytest.raises(OutputMaskLimitError) as exc:
        masker.feed("文")
    assert exc.value.code == "OUTPUT_TOO_LARGE"
    with pytest.raises(OutputMaskError):
        masker.finish()


def test_closed_and_invalid_input_have_typed_failures():
    masker = OutputMasker()
    with pytest.raises(OutputMaskError) as exc:
        masker.feed(None)
    assert exc.value.code == "OUTPUT_MASK_INVALID_INPUT"
    with pytest.raises(OutputMaskError):
        masker.feed("不能繼續")
    masker = OutputMasker()
    assert masker.feed("正常") == "正常"
    assert masker.finish() == masker.finish() == ""
    with pytest.raises(OutputMaskError) as exc:
        masker.feed("不能繼續")
    assert exc.value.code == "OUTPUT_MASK_CLOSED"


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "credential",
        "cookie",
        "api_key",
        "api-key",
        "apikey",
        "employee_id",
        "employee-id",
        "員工編號",
        "密碼",
        "憑證",
    ],
)
def test_all_sensitive_assignment_labels_hold_arbitrary_values(key):
    value = "VAULTCREDENTIAL" + "q" * 4096
    masker = OutputMasker()
    output = ""
    for character in "前言。" + key + "  ：  " + value:
        output += masker.feed(character)
        assert "VAULT" not in output
    output += masker.finish()
    assert "VAULT" not in output
    assert "qqqqqq" not in output
    assert output.startswith("前言。")


def test_exact_input_limit_is_accepted_for_quarantined_data():
    masker = OutputMasker()
    assert masker.feed("{" + "z" * (MAX_TOTAL_CHARS - 1)) == ""
    assert "遮罩" in masker.finish()


def test_total_limit_does_not_reset_after_plaintext_was_emitted():
    masker = OutputMasker()
    assert masker.feed("中" * 10000) == "中" * 10000
    assert masker.feed("{" + "z" * (MAX_TOTAL_CHARS - 10001)) == ""
    assert masker.total_chars == MAX_TOTAL_CHARS
    assert masker.buffered_chars < MAX_BUFFER_CHARS
    with pytest.raises(OutputMaskLimitError):
        masker.feed("z")


@pytest.mark.parametrize(
    "text",
    [
        "院內說明與 Hello world。",
        "本人資料 A123456789 與 0912345678。",
        "Bearer vault-super-secret",
        "password='vault-super-secret'",
    ],
)
def test_standard_cases_match_existing_batch_mask(text):
    from app.services.quick_execution import mask

    assert collect(list(text)) == mask(text)
