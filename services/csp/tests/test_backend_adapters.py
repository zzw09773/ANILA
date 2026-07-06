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
    # 非 JSON body → 包成 OpenAI error
    out2 = a.from_backend_error("chat", 500, "upstream boom")
    assert out2["error"]["message"] == "upstream boom"

def test_get_adapter_known_and_fallback(caplog):
    assert get_adapter("openai_compatible").name == "openai_compatible"
    # 未知/DB 殘值（如舊 custom_adapter）→ fallback passthrough + warn
    a = get_adapter("custom_adapter")
    assert a.name == "openai_compatible"
    assert any("custom_adapter" in r.message for r in caplog.records)
