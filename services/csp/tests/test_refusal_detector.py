"""拒答偵測器真值表＋非串流 think-strip 掛鉤（量測層）。

推論稽核寫入路徑（舊 main 的 ``inference_audit`` / PR #46）在
``restart/from-redesign`` 重啟樹上**尚未移植**，因此本包 (b) 只交付
純偵測器；掛進 audit metadata 的 ``suspected_refusal`` 欄位等稽核軌跡
落地後再接。此檔仍鎖住偵測器行為，並驗 (c) 非串流剝 think 掛鉤。
"""

from __future__ import annotations

import logging

import pytest

from app.services.refusal_detector import looks_like_refusal
from app.services.proxy import service as proxy_service


# ── looks_like_refusal 真值表 ─────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "我無法協助處理這類請求。",
        "抱歉，我不能提供這方面的細節。",
        "I cannot assist with that request.",
        "Sorry, I can't help with weapons design.",
        "As an AI, I must refuse this request.",
        "此內容違反使用政策，因此無法繼續。",
        "此請求違反使用政策，我無法協助",
        "This violates our content policy.",
    ],
)
def test_refusal_patterns_hit(text: str):
    assert looks_like_refusal(text) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        # grounding 誠實回答——絕不可算拒答
        "資料中查無此參數，目前段落沒有提供這項資訊。",
        "目前段落沒有提供這項資訊，建議查閱原始文件。",
        "依文件所述，推進劑應存放於陰涼乾燥處，並隔離火源。[1]",
        "雷達藉由發射電磁波並接收回波來測距與測向。",
        "民國114年（2025）的預算編列如附件所示。",
        "",
        None,
        "無法確定精確數值，但文件給出的範圍是 10–20。",
        # 合規說明含「違反…政策」但無拒答動詞——不可誤報
        "此作法違反本院資安政策，建議改用加密通道",
    ],
)
def test_non_refusal_and_grounding_miss(text):
    assert looks_like_refusal(text) is False


@pytest.mark.unit
def test_refusal_looks_at_head_not_long_tail():
    # 長文末尾偶爾出現「我無法協助」類字樣不應整篇誤判——偵測器只看前段。
    body = "本段詳述庫房通風規範。" * 80 + "我無法協助"
    assert looks_like_refusal(body) is False


# ── (c) 非串流 think-strip 掛鉤 ───────────────────────────────────────────


@pytest.mark.unit
def test_nonstream_think_strip_cleans_content(caplog):
    payload = {
        "id": "chatcmpl-test",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "可見<think>英文 reasoning secret</think>正文",
                },
                "finish_reason": "stop",
            }
        ],
    }
    with caplog.at_level(logging.INFO, logger="app.services.proxy_service"):
        out = proxy_service.strip_inline_think_from_chat_result(payload)

    content = out["choices"][0]["message"]["content"]
    assert content == "可見正文"
    assert "secret" not in content
    assert "reasoning" not in content
    assert any("think_strip removed_chars=" in r for r in caplog.messages)


@pytest.mark.unit
def test_nonstream_think_strip_absent_noop(caplog):
    payload = {
        "choices": [
            {"message": {"role": "assistant", "content": "乾淨回覆"}, "finish_reason": "stop"}
        ]
    }
    with caplog.at_level(logging.INFO, logger="app.services.proxy_service"):
        out = proxy_service.strip_inline_think_from_chat_result(payload)
    assert out["choices"][0]["message"]["content"] == "乾淨回覆"
    assert not any("think_strip removed_chars=" in r for r in caplog.messages)


@pytest.mark.unit
def test_nonstream_think_strip_fail_open_on_bad_payload():
    # 非 dict / 畸形結構不得 raise。
    assert proxy_service.strip_inline_think_from_chat_result(None) is None
    assert proxy_service.strip_inline_think_from_chat_result("x") == "x"
    odd = {"choices": [None, {"message": None}, {"message": {"content": 123}}]}
    assert proxy_service.strip_inline_think_from_chat_result(odd) is odd


def test_kb_miss_grounding_notice_is_not_a_refusal():
    """csp 在規章庫查無條文時要模型講的那句話（_KB_MISS_NOTICE 形狀）不是拒答；
    新的「僅提供…服務…無法回答」樣式不得把它算進去。"""
    from app.services.refusal_detector import looks_like_refusal
    assert not looks_like_refusal("已查詢院內規章知識庫，沒有找到相關條文。目前段落沒有提供這項資訊，無法回答具體的申訴期限；以下為一般知識。")
    assert not looks_like_refusal("已查詢院內規章知識庫，門檻之上沒有相關條文。目前段落沒有提供這項資訊，無法回答具體的申訴期限；以下為一般知識。")
    assert not looks_like_refusal("目前提供的院內規章檢索結果中，並沒有關於「軍人請假規定」的資訊。")
    assert looks_like_refusal("本系統僅提供法律文件檢索與解釋服務，無法回答與法律無關的問題。")
