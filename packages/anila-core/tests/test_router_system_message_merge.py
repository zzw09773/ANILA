"""Router routing_messages: exactly one leading system message.

Strict vLLM (Qwen via litellm) returns HTTP 400
``System message must be at the beginning`` when the list is
``[router_system, caller_system, user]``. The SPA title generator sends
``[system, user]`` and then falls back to ``（LLM 暫時無法回應…）``.
"""

from __future__ import annotations

from anila_core.api.router_server import _merge_routing_messages

ROUTER = "router system prompt"


def test_no_caller_system_prepends_router_then_user() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "user", "content": "again"},
    ]
    out = _merge_routing_messages(ROUTER, messages)
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert out[0] == {"role": "system", "content": ROUTER}
    assert out[1:] == messages


def test_caller_system_then_user_merges_into_single_leading_system() -> None:
    caller = "you are a title generator"
    messages = [
        {"role": "system", "content": caller},
        {"role": "user", "content": "請取標題"},
    ]
    out = _merge_routing_messages(ROUTER, messages)
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["role"] == "system"
    assert out[0]["content"] == f"{ROUTER}\n\n{caller}"
    assert ROUTER in out[0]["content"]
    assert caller in out[0]["content"]
    assert out[1] == {"role": "user", "content": "請取標題"}
    assert sum(1 for m in out if m.get("role") == "system") == 1


def test_user_only_is_unchanged_except_prepended_router_system() -> None:
    messages = [{"role": "user", "content": "只問一件事"}]
    out = _merge_routing_messages(ROUTER, messages)
    assert out == [
        {"role": "system", "content": ROUTER},
        {"role": "user", "content": "只問一件事"},
    ]


def test_consecutive_leading_systems_all_fold_in() -> None:
    messages = [
        {"role": "system", "content": "first"},
        {"role": "system", "content": "second"},
        {"role": "user", "content": "q"},
    ]
    out = _merge_routing_messages(ROUTER, messages)
    assert [m["role"] for m in out] == ["system", "user"]
    assert out[0]["content"] == f"{ROUTER}\n\nfirst\n\nsecond"


def test_does_not_mutate_inbound_messages() -> None:
    messages = [
        {"role": "system", "content": "caller"},
        {"role": "user", "content": "q"},
    ]
    snapshot = [dict(m) for m in messages]
    _merge_routing_messages(ROUTER, messages)
    assert messages == snapshot
