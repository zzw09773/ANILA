import json

import httpx
from app.services.proxy.adapters.base import PassthroughAdapter
from app.services.proxy.adapters import get_adapter

def test_passthrough_is_noop():
    a = PassthroughAdapter()
    assert a.name == "openai_compatible"
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert a.to_backend_request("chat", body) is body
    resp = {"choices": [{"message": {"content": "x"}}]}
    assert a.from_backend_response("chat", resp) is resp
    assert a.from_backend_stream_chunk("chat", "data: {}") == "data: {}"
    assert a.backend_path("chat", "m", "v1") == "/v1/chat/completions"
    assert a.backend_path("embeddings", "m", "v1") == "/v1/embeddings"
    default = httpx.Timeout(30.0)
    assert a.request_timeout("chat", default) is default

def test_passthrough_error_openai_shape():
    a = PassthroughAdapter()
    out = a.from_backend_error("chat", 400, '{"error":{"message":"bad"}}')
    assert out == {"error": {"message": "bad"}}
    # OpenAI-shape message 過長 → 截 300
    long_msg = "x" * 400
    out_long = a.from_backend_error("chat", 400, json.dumps({"error": {"message": long_msg}}))
    assert out_long["error"]["message"] == long_msg[:300]


def test_passthrough_error_non_openai_shape_never_leaks_raw_body():
    """安全行為鎖定：非 OpenAI-shape 的上游 4xx body（非 JSON、或 JSON 但無
    error/error 非 dict/message 非 str）一律回泛用訊息，絕不把 raw_body
    （可能含上游內部細節，如 stack trace、內部路徑）洩漏進 client 可見的
    message。舊 code（Task 5 接入前）就是這個安全語意 —— 這裡把它鎖回來。"""
    a = PassthroughAdapter()

    # 非 JSON 純文字（可能含內部 trace/路徑）→ 泛用訊息，不含原文
    out_text = a.from_backend_error("chat", 500, "upstream boom: internal stack trace at /app/foo.py")
    assert out_text["error"]["message"] == "模型服務拒絕請求 (HTTP 500)"
    assert "upstream boom" not in out_text["error"]["message"]
    assert "/app/foo.py" not in out_text["error"]["message"]

    # JSON dict 但無 error 鍵（如常見的 {"detail": ...}）→ 泛用訊息
    out_detail = a.from_backend_error("chat", 400, '{"detail":"invalid api key for upstream"}')
    assert out_detail["error"]["message"] == "模型服務拒絕請求 (HTTP 400)"
    assert "invalid api key" not in out_detail["error"]["message"]

    # error 存在但不是 dict → 泛用訊息（不因 .get 例外而洩漏原文）
    out_error_str = a.from_backend_error("chat", 400, '{"error":"raw string error"}')
    assert out_error_str["error"]["message"] == "模型服務拒絕請求 (HTTP 400)"
    assert "raw string error" not in out_error_str["error"]["message"]

    # error.message 非 str（如 dict/None）→ 泛用訊息
    out_msg_none = a.from_backend_error("chat", 400, '{"error":{"message": null}}')
    assert out_msg_none["error"]["message"] == "模型服務拒絕請求 (HTTP 400)"

    # error.message 是空字串 → 泛用訊息（避免回空白 detail）
    out_msg_empty = a.from_backend_error("chat", 400, '{"error":{"message": ""}}')
    assert out_msg_empty["error"]["message"] == "模型服務拒絕請求 (HTTP 400)"

def test_get_adapter_known_and_fallback(caplog):
    assert get_adapter("openai_compatible").name == "openai_compatible"
    # 未知/DB 殘值（如舊 custom_adapter）→ fallback passthrough + warn
    a = get_adapter("custom_adapter")
    assert a.name == "openai_compatible"
    assert any("custom_adapter" in r.message for r in caplog.records)
