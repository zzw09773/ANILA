"""Tests for compact/strip_images — data-URL peeling before compact."""

from __future__ import annotations

from anila_core.compact.openai_history import estimate_openai_tokens
from anila_core.compact.strip_images import (
    IMAGE_MIN_TOKENS,
    strip_images_messages,
    strip_images_openai,
)
from anila_core.models.message import AssistantMessage, UserMessage


def _png_data_url(payload: str) -> str:
    return f"data:image/png;base64,{payload}"


def _image_user(text: str, payload: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": _png_data_url(payload)}},
        ],
    }


def test_old_turn_data_url_replaced_recent_kept() -> None:
    big = "A" * 4000
    messages = [
        {"role": "system", "content": "sys"},
        _image_user("old question", big),
        {"role": "assistant", "content": "old answer"},
        _image_user("new question", big),
    ]
    out, saved = strip_images_openai(messages, keep_recent_turns=1)
    assert saved > 0
    old_parts = out[1]["content"]
    assert any(
        isinstance(p, dict)
        and p.get("type") == "text"
        and "圖片已省略" in str(p.get("text"))
        and "image/png" in str(p.get("text"))
        for p in old_parts
    )
    assert not any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in old_parts
    )
    recent_parts = out[-1]["content"]
    assert any(
        isinstance(p, dict)
        and p.get("type") == "image_url"
        and "data:image/png" in str(p.get("image_url"))
        for p in recent_parts
    )


def test_embedded_base64_in_string_replaced() -> None:
    big = "B" * 5000
    messages = [
        {"role": "user", "content": f"see {_png_data_url(big)} please"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    out, saved = strip_images_openai(messages, keep_recent_turns=1)
    assert saved > 0
    assert "data:image" not in out[0]["content"]
    assert "圖片已省略：image/png" in out[0]["content"]
    assert "KB]" in out[0]["content"]


def test_token_estimate_drops_after_strip() -> None:
    big = "C" * 8000
    messages = [
        _image_user("old", big),
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "now"},
    ]
    before = estimate_openai_tokens(messages)
    out, saved = strip_images_openai(messages, keep_recent_turns=1)
    after = estimate_openai_tokens(out)
    assert saved > 0
    assert after < before
    assert IMAGE_MIN_TOKENS <= before - after or after < before


def test_does_not_mutate_input() -> None:
    big = "D" * 4000
    messages = [
        _image_user("old", big),
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "now"},
    ]
    snapshot = [
        messages[0]["content"][1]["image_url"]["url"],
        messages[1]["content"],
    ]
    strip_images_openai(messages, keep_recent_turns=1)
    assert messages[0]["content"][1]["image_url"]["url"] == snapshot[0]
    assert messages[1]["content"] == snapshot[1]
    assert "data:image/png" in messages[0]["content"][1]["image_url"]["url"]


def test_small_image_under_2kb_untouched() -> None:
    small = "E" * 100
    text = f"icon {_png_data_url(small)} end"
    messages = [
        {"role": "user", "content": text},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    out, _saved = strip_images_openai(messages, keep_recent_turns=1)
    assert out[0]["content"] == text
    assert "data:image/png;base64,EEE" in out[0]["content"]


def test_strip_images_messages_tool_result_and_image_block() -> None:
    big = "F" * 4000
    messages = [
        UserMessage(
            content=[
                {"type": "text", "text": "old"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": big}},
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": f"shot {_png_data_url(big)}",
                },
            ]
        ),
        UserMessage(content="latest"),
    ]
    out, saved = strip_images_messages(messages, keep_recent_turns=1)
    assert saved > 0
    assert messages[0].content[1]["type"] == "image"
    blocks = out[0].content
    assert isinstance(blocks, list)
    assert any(
        isinstance(b, dict) and b.get("type") == "text" and "圖片已省略" in str(b.get("text"))
        for b in blocks
    )
    tool = next(b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result")
    assert "data:image" not in str(tool.get("content"))
    assert "圖片已省略" in str(tool.get("content"))


def _four_image_turns_openai(payload: str = "P" * 3000) -> list[dict]:
    out: list[dict] = []
    for i in range(4):
        out.append(_image_user(f"u{i}", payload))
        out.append({"role": "assistant", "content": f"a{i}"})
    return out


def _four_image_turns_messages(payload: str = "P" * 3000) -> list:
    out = []
    for i in range(4):
        out.append(
            UserMessage(
                content=[
                    {"type": "text", "text": f"u{i}"},
                    {"type": "image_url", "image_url": {"url": _png_data_url(payload)}},
                ]
            )
        )
        out.append(AssistantMessage(content=f"a{i}", tool_calls=[]))
    return out


def _user_has_image_url(content) -> bool:
    if not isinstance(content, list):
        return False
    return any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)


def _user_has_placeholder(content) -> bool:
    return "圖片已省略" in str(content)


def test_keep_recent_turns_two_keeps_second_newest_openai() -> None:
    messages = _four_image_turns_openai()
    out, saved = strip_images_openai(messages, keep_recent_turns=2)
    assert saved > 0
    users = [m for m in out if m.get("role") == "user"]
    assert _user_has_placeholder(users[0]["content"])
    assert _user_has_placeholder(users[1]["content"])
    assert _user_has_image_url(users[2]["content"])
    assert _user_has_image_url(users[3]["content"])


def test_keep_recent_turns_one_keeps_only_last_openai() -> None:
    messages = _four_image_turns_openai()
    out, saved = strip_images_openai(messages, keep_recent_turns=1)
    assert saved > 0
    users = [m for m in out if m.get("role") == "user"]
    assert all(_user_has_placeholder(u["content"]) for u in users[:-1])
    assert _user_has_image_url(users[-1]["content"])


def test_keep_recent_turns_two_keeps_second_newest_messages() -> None:
    messages = _four_image_turns_messages()
    out, saved = strip_images_messages(messages, keep_recent_turns=2)
    assert saved > 0
    users = [m for m in out if isinstance(m, UserMessage)]
    assert _user_has_placeholder(users[0].content)
    assert _user_has_placeholder(users[1].content)
    assert _user_has_image_url(users[2].content)
    assert _user_has_image_url(users[3].content)


def test_keep_recent_turns_one_keeps_only_last_messages() -> None:
    messages = _four_image_turns_messages()
    out, saved = strip_images_messages(messages, keep_recent_turns=1)
    assert saved > 0
    users = [m for m in out if isinstance(m, UserMessage)]
    assert all(_user_has_placeholder(u.content) for u in users[:-1])
    assert _user_has_image_url(users[-1].content)
