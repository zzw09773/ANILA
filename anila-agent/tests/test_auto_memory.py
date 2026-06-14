"""自動寫入：absorb_turn 抽取→去敏→寫入 per-tenant store；fail-closed；gate。"""

from __future__ import annotations

from pathlib import Path

import pytest

from anila_agent.memory.memdir import MemoryStore, tenant_slug
from anila_agent.memory.runtime import MemdirRuntime, auto_memory_enabled

pytestmark = pytest.mark.unit


def _runtime(root: Path, extractor) -> MemdirRuntime:
    return MemdirRuntime(store=MemoryStore(root=root), extractor=extractor)


async def _fake_one(transcript, manifest):
    # body 故意夾帶 csk- 祕密，驗證寫入前被去敏。
    return [
        {"name": "fav-color", "description": "使用者喜歡的顏色", "type": "user",
         "body": "藍色（憑證 csk-supersecret123 不該落地）"},
    ]


async def test_absorb_writes_and_redacts(tmp_path):
    rt = _runtime(tmp_path / "memory" / "tenants" / tenant_slug("alice"), _fake_one)
    written = await rt.absorb_turn("我最喜歡藍色", "了解，記住了")
    assert written == ["fav-color"]

    m = rt.store.read("fav-color")
    assert m is not None and m.type == "user"
    # csk- 祕密在寫入前被去敏。
    assert "csk-supersecret123" not in m.body
    assert "[REDACTED]" in m.body


async def test_absorb_is_per_tenant_isolated(tmp_path):
    base = tmp_path / "memory" / "tenants"
    alice = _runtime(base / tenant_slug("alice"), _fake_one)
    bob = _runtime(base / tenant_slug("bob"), _fake_one)

    await alice.absorb_turn("我喜歡藍色", "好")
    assert [m.name for m in alice.store.list()] == ["fav-color"]
    assert bob.store.list() == []  # bob 看不到 alice 寫的


async def test_absorb_fail_closed_on_extractor_error(tmp_path):
    async def boom(transcript, manifest):
        raise RuntimeError("extractor LLM down")

    rt = _runtime(tmp_path / "memory", boom)
    assert await rt.absorb_turn("x", "y") == []   # 不丟例外
    assert rt.store.list() == []                   # 沒弄壞既有記憶


async def test_absorb_no_extractor_is_noop(tmp_path):
    rt = MemdirRuntime(store=MemoryStore(root=tmp_path / "memory"), extractor=None)
    assert await rt.absorb_turn("x", "y") == []


async def test_absorb_skips_invalid_items(tmp_path):
    async def mixed(transcript, manifest):
        return [
            {"name": "ok-mem", "description": "d", "type": "project", "body": "b"},
            {"name": "", "description": "d", "type": "user", "body": "b"},        # 無 name
            {"name": "bad-type", "description": "d", "type": "nonsense", "body": "b"},  # 壞 type
        ]

    rt = _runtime(tmp_path / "memory", mixed)
    written = await rt.absorb_turn("x", "y")
    assert written == ["ok-mem"]


def test_auto_memory_enabled_gate(monkeypatch):
    monkeypatch.delenv("ANILA_AUTO_MEMORY", raising=False)
    assert auto_memory_enabled() is False
    monkeypatch.setenv("ANILA_AUTO_MEMORY", "1")
    assert auto_memory_enabled() is True
    monkeypatch.setenv("ANILA_AUTO_MEMORY", "  1  ")  # strip
    assert auto_memory_enabled() is True
    monkeypatch.setenv("ANILA_AUTO_MEMORY", "0")
    assert auto_memory_enabled() is False
