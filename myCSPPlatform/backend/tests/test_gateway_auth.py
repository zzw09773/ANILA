"""出向模型 gateway API key 注入 (_apply_gateway_auth) 的單元測試。

內網拓撲:模型在 10.53.100.12 My-OpenAI-Frontend gateway 後面,/v1 全
路由要 Authorization: Bearer。MODEL_GATEWAY_API_KEY 空 = 不注入 (同主機
裸 vLLM 情境,行為不變)。
"""
from app.config import settings
from app.services.proxy_service import _apply_gateway_auth


def test_no_key_no_injection(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    result = _apply_gateway_auth(headers)
    assert "Authorization" not in result


def test_whitespace_key_no_injection(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "   ")
    assert "Authorization" not in _apply_gateway_auth({})


def test_key_injected_as_bearer(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "sk-test-123")
    result = _apply_gateway_auth({"Content-Type": "application/json"})
    assert result["Authorization"] == "Bearer sk-test-123"


def test_existing_authorization_not_overwritten(monkeypatch):
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "sk-test-123")
    result = _apply_gateway_auth({"Authorization": "Bearer upstream-key"})
    assert result["Authorization"] == "Bearer upstream-key"


def test_mutates_in_place_and_returns_same_dict(monkeypatch):
    # proxy_request / proxy_stream 都靠 in-place 行為,回傳值只是方便鏈式。
    monkeypatch.setattr(settings, "MODEL_GATEWAY_API_KEY", "sk-test-123")
    headers = {}
    result = _apply_gateway_auth(headers)
    assert result is headers
    assert headers["Authorization"] == "Bearer sk-test-123"
