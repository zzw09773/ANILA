"""多租戶記憶分艙：tenant_slug 路徑安全 + 防碰撞，build_memdir_runtime 各租戶隔離。

中科院場景是多人共用同一個部署 agent；記憶必須按 X-ANILA-User-Id 分艙，A 的記憶
B 不可見/不可寫。本檔以對抗性輸入驗證隔離不被穿越或碰撞繞過。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anila_agent.config import AgentConfig, AppConfig, ModelConfig
from anila_agent.memory.memdir import Memory, tenant_slug
from anila_agent.memory.runtime import build_memdir_runtime
from anila_agent.retrieval.dummy import DummyRetriever
from anila_agent.runtime.agent_factory import build_agent

pytestmark = pytest.mark.unit


def _cfg(tmp_path: Path) -> AppConfig:
    return AppConfig(
        model=ModelConfig(
            base_url="http://x/v1", model="gpt-oss-20b", api_key="EMPTY",
            ssl_verify=True, timeout=30.0,
        ),
        agent=AgentConfig(name="t-agent", max_turns=7),
        home=tmp_path,
        log_level="INFO",
    )


# ---- tenant_slug：路徑安全（對抗性輸入）----


@pytest.mark.parametrize(
    "evil",
    [
        "../../etc/passwd",
        "a/b/c",
        "..",
        "../sibling",
        "x\x00y",
        "/abs/root",
        "..\\..\\win",
        "~/secret",
        "....//....//",
    ],
)
def test_tenant_slug_is_single_safe_component(evil):
    slug = tenant_slug(evil)
    assert "/" not in slug and "\\" not in slug
    assert ".." not in slug
    assert "\x00" not in slug
    assert all(c.isalnum() or c in "-_" for c in slug)
    # 當成 tenants/<slug> 路徑元件不會逃出 base。
    resolved = (Path("/base/tenants") / slug).resolve()
    assert str(resolved).startswith("/base/tenants/")


def test_tenant_slug_collision_free_even_when_prefix_collides():
    # 清洗後可讀前綴相同（'a/b' 與 'a_b' → 'a_b'），但 hash 段不同 → slug 不同。
    # 否則兩個不同 user 共用同一個分艙 = 跨租戶記憶洩漏。
    a, b = tenant_slug("a/b"), tenant_slug("a_b")
    assert a != b
    assert a.rsplit("-", 1)[-1] != b.rsplit("-", 1)[-1]


def test_tenant_slug_case_sensitive():
    # 'Alice' 與 'alice' 是不同身分，不可共用分艙。
    assert tenant_slug("Alice") != tenant_slug("alice")


def test_tenant_slug_deterministic():
    assert tenant_slug("user-42") == tenant_slug("user-42")


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n", " \t\n "])
def test_tenant_slug_rejects_blank(blank):
    # 空白不是合法租戶——否則全部塌縮成單一常數桶 'u-...'＝跨租戶洩漏。
    with pytest.raises(ValueError):
        tenant_slug(blank)


def test_tenant_slug_whitespace_variants_do_not_merge():
    # digest 取自原字串（不先 strip）→ 空白變體不會誤併成同一租戶。
    assert tenant_slug("analyst") != tenant_slug(" analyst")
    assert tenant_slug("analyst") != tenant_slug("analyst\n")


# ---- build_memdir_runtime：租戶隔離 + 向後相容 ----


def test_tenants_get_isolated_stores(tmp_path):
    rt_alice = build_memdir_runtime(_cfg(tmp_path), tenant="alice")
    rt_bob = build_memdir_runtime(_cfg(tmp_path), tenant="bob")

    assert rt_alice.store.root != rt_bob.store.root
    assert "tenants" in rt_alice.store.root.parts

    # 寫 alice 的記憶 → bob 完全看不到。
    rt_alice.store.write(Memory(name="fav-color", description="x", type="user", body="藍色"))
    assert [m.name for m in rt_alice.store.list()] == ["fav-color"]
    assert rt_bob.store.list() == []


def test_no_tenant_is_legacy_shared_root(tmp_path):
    # CLI / 單人：tenant=None 維持舊路徑 ANILA_HOME/memory，不破壞既有記憶。
    rt = build_memdir_runtime(_cfg(tmp_path), tenant=None)
    assert rt.store.root == tmp_path / "memory"
    assert "tenants" not in rt.store.root.parts


def test_same_tenant_same_store(tmp_path):
    r1 = build_memdir_runtime(_cfg(tmp_path), tenant="alice")
    r2 = build_memdir_runtime(_cfg(tmp_path), tenant="alice")
    assert r1.store.root == r2.store.root


def test_traversal_tenant_cannot_escape_or_hit_shared(tmp_path):
    # 惡意 user_id 想逃到上層或撞共用 store，都不行。
    legacy = build_memdir_runtime(_cfg(tmp_path), tenant=None).store.root
    evil = build_memdir_runtime(_cfg(tmp_path), tenant="../../../../etc").store.root
    assert "tenants" in evil.parts      # 被關在 tenants/ 之下
    assert ".." not in evil.parts       # 沒穿越
    assert evil != legacy               # 沒撞到共用 store


# ---- build_agent：把 memory_tenant 串到 store ----


def test_build_agent_threads_tenant_to_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("ANILA_MEMORY", "1")
    a = build_agent(_cfg(tmp_path), retriever=DummyRetriever(), memory_tenant="alice")
    assert a.context.memory is not None
    assert tenant_slug("alice") in a.context.memory.store.root.parts


def test_build_agent_no_tenant_still_works(tmp_path, monkeypatch):
    # CLI 語意（memory_requires_tenant=False）：tenant=None → 共用 store（單人正確）。
    monkeypatch.setenv("ANILA_MEMORY", "1")
    a = build_agent(_cfg(tmp_path), retriever=DummyRetriever(), memory_tenant=None)
    assert a.context.memory is not None
    assert a.context.memory.store.root == tmp_path / "memory"


# ---- 多租戶部署語意（memory_requires_tenant=True）：無身分 → 不給記憶 ----


@pytest.mark.parametrize("blank_tenant", [None, "", "   ", "\t"])
def test_multitenant_blank_identity_gets_no_memory(tmp_path, monkeypatch, blank_tenant):
    # service_wrapper 走這條：缺/空白身分一律關閉記憶，絕不退回共用 store。
    monkeypatch.setenv("ANILA_MEMORY", "1")
    a = build_agent(
        _cfg(tmp_path),
        retriever=DummyRetriever(),
        memory_tenant=blank_tenant,
        memory_requires_tenant=True,
    )
    assert a.context.memory is None


def test_multitenant_valid_identity_gets_isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("ANILA_MEMORY", "1")
    a = build_agent(
        _cfg(tmp_path),
        retriever=DummyRetriever(),
        memory_tenant="user-123",
        memory_requires_tenant=True,
    )
    assert a.context.memory is not None
    assert tenant_slug("user-123") in a.context.memory.store.root.parts


def test_build_agent_normalizes_whitespace_tenant_to_none(tmp_path, monkeypatch):
    # CLI 語意下傳入全空白 → 正規化成 None → 共用 store，不會建出空白桶。
    monkeypatch.setenv("ANILA_MEMORY", "1")
    a = build_agent(_cfg(tmp_path), retriever=DummyRetriever(), memory_tenant="   ")
    assert a.context.memory is not None
    assert a.context.memory.store.root == tmp_path / "memory"
