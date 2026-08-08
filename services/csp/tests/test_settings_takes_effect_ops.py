# -*- coding: utf-8 -*-
"""九顆營運鈕：改完**下一個請求**就生效，而且下限守得住平台。

這是 ``test_settings_takes_effect.py`` 的姊妹檔。分成兩支是因為這九顆的探針形狀
與那十顆不同：這裡每一顆的「下游參數」都藏在一次**出向呼叫**裡（httpx client 的
timeout、重試迴圈的次數、退避的秒數、SQL 的 ``LIMIT``），所以探針一律要偽造一個
上游／下游來把那個參數接住，而且多半是 async。硬塞進那張同步的 Knob 表會讓兩邊
都看不懂。**釘法一模一樣，只有探針的做工不同。**

九顆分兩組：

* proxy 組 5 —— ``proxy.llm_timeout`` / ``proxy.embedding_timeout`` /
  ``proxy.max_retries`` / ``proxy.retry_base_delay`` / ``limits.action_invoke_per_min``
* memory 組 4 —— ``memory.retrieve_top_k`` / ``memory.retrieve_min_cosine`` /
  ``memory.max_chunk_chars`` / ``memory.http_timeout``

這一包要消滅的三個形狀（每一個都有專屬的釘）
============================================

1. **模組層擷取**。memory 組四顆改造前是 ``memory_service.py:103-107`` 的
   import 期常數：畫面上改得動、後端到重啟前都不知道。這裡的釘是「同一個行程內
   改完立刻生效」＋「值真的抵達下游參數」，把任何一顆搬回模組層都會紅。
2. **逾時織進 client 的建構**。csp 這邊的 httpx client 是**每次呼叫現建**的
   （不是池化單例），所以逾時落在 ``httpx.AsyncClient(timeout=...)`` 這一層；
   釘的方式是換掉 ``AsyncClient`` 本身、把建構參數接下來比對。
3. **值域下限被繞過**。逾時填 0、重試填負數、cosine 填 1.5 —— 這三種都是
   「從畫面把平台打掛」。釘的不只是「寫入被拒」，還有**生效值仍然是舊的那一個**
   （半套用比被拒更難查）。

⚠ **場上的預設值有哪些**（測試值一律避開全部）：登錄表 120/30/3/0.5/20 與
3/0.4/1200/30.0、``config.py`` 同名欄位的同一組值、compose 的
``LLM_TIMEOUT`` **300**（platform.yml:126）與 ``EMBEDDING_TIMEOUT`` 30
（:133）、以及門檻那顆的 0.3。所以下面的測試值是 77／44／5／0.125／7／
0.625／37／41.5 —— 沒有一個是場上任何一份預設值。這條規則吃過兩次虧：值撞到
預設值時，「有讀到設定」與「凍結在預設值」會一起變綠。
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from types import SimpleNamespace
from typing import Any, Callable

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.platform_setting import PlatformSetting, get_setting, set_setting
from app.services import memory_service, message_action_service
from app.services.proxy import service as proxy_impl
from app.services.proxy.service import ProxyTuning, resolve_proxy_tuning
from app.services.settings_registry import REGISTRY

# ── 測試值：**沒有一個等於場上的任何預設值** ──────────────────────────────────

LLM_TIMEOUT_V = 77          # ≠ 120（登錄表／config）≠ 300（compose）
EMBED_TIMEOUT_V = 44        # ≠ 30
MAX_RETRIES_V = 5           # ≠ 3
RETRY_DELAY_V = 0.125       # ≠ 0.5
INVOKE_PER_MIN_V = 7        # ≠ 20
TOP_K_V = 7                 # ≠ 3
MIN_COSINE_V = 0.625        # ≠ 0.4（記憶）≠ 0.3（院內規章門檻）
MAX_CHUNK_CHARS_V = 37      # ≠ 1200
MEMORY_TIMEOUT_V = 41.5     # ≠ 30.0

NINE_KEYS = (
    "proxy.llm_timeout",
    "proxy.embedding_timeout",
    "proxy.max_retries",
    "proxy.retry_base_delay",
    "limits.action_invoke_per_min",
    "memory.retrieve_top_k",
    "memory.retrieve_min_cosine",
    "memory.max_chunk_chars",
    "memory.http_timeout",
)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """把這九顆的 env 從測試環境裡拔乾淨。

    留著的話，「回退到程式預設」那一輪讀到的是跑測試的人殼裡剛好有的值 ——
    答案取決於誰在哪台機器上跑，那不是基準線。
    """
    for key in NINE_KEYS:
        env_name = REGISTRY[key].env_name
        assert env_name is not None, f"{key} 少了 env 名，回退鏈斷了一層"
        monkeypatch.delenv(env_name, raising=False)


@pytest.fixture(autouse=True)
def _clean_rate_buckets():
    """速率限制的桶是模組層狀態，會跨測試漏。"""
    message_action_service._RATE_BUCKETS.clear()
    yield
    message_action_service._RATE_BUCKETS.clear()


# ── 偽造的 httpx client：把建構時的 timeout 接住 ─────────────────────────────


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}
        self.text = "{}"

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        return None


def _fake_client_factory(
    captured: list[Any],
    *,
    payload: dict | None = None,
    raises: Callable[[], BaseException] | None = None,
    posts: list[Any] | None = None,
):
    """回一個假的 ``httpx.AsyncClient``，把 ``timeout=`` 記進 ``captured``。

    逾時真正落腳的那一層就是這個建構子 —— csp 每一條出向路徑都是**現建 client**
    （沒有池化單例），所以 per-request 的值不需要下沉到 ``client.post`` 的參數。
    這個假貨就是那一層的量測點。
    """

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            captured.append(kwargs.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def post(self, *args, **kwargs):
            if posts is not None:
                posts.append(args[0] if args else kwargs.get("url"))
            if raises is not None:
                raise raises()
            return _FakeResponse(payload or {})

    return _FakeClient


# ── 1. proxy 四顆：解析 → 下游參數 ──────────────────────────────────────────


def _llm_model() -> SimpleNamespace:
    return SimpleNamespace(
        id=901,
        name="ops-knob-llm",
        display_name="ops-knob-llm",
        model_type="llm",
        api_version="v1",
        endpoint_url="http://ops-knob-llm.test/v1",
        protocol="openai_compatible",
        api_key_secret_ref=None,
        is_internal=False,
        is_active=True,
    )


def _embedding_model() -> SimpleNamespace:
    model = _llm_model()
    model.model_type = "embedding"
    model.name = "ops-knob-embed"
    return model


def _run_proxy_request(model, tuning, monkeypatch, **overrides):
    """跑一次 ``proxy_request``，回傳（捕捉到的 timeout 清單、post 次數清單）。"""
    captured: list[Any] = []
    posts: list[Any] = []
    monkeypatch.setattr(proxy_impl, "_guard_outbound", lambda *a, **k: None)
    monkeypatch.setattr(
        proxy_impl.httpx,
        "AsyncClient",
        _fake_client_factory(
            captured,
            payload={"choices": [{"message": {"content": "ok"}}]},
            posts=posts,
            **overrides,
        ),
    )
    coro = proxy_impl.proxy_request(
        model=model,
        api_key_id=None,
        user_id=0,
        department_id=None,
        request_body={"messages": [{"role": "user", "content": "hi"}]},
        endpoint_path="/v1/chat/completions",
        record_usage=False,
        tuning=tuning,
    )
    return captured, posts, coro


@pytest.mark.parametrize("value", [1, LLM_TIMEOUT_V, 3600])
def test_llm_timeout_reaches_the_httpx_client(db, monkeypatch, value):
    """下界／內插／上界三點：存進去的秒數要出現在 client 的建構參數上。"""
    set_setting(db, "proxy.llm_timeout", value)
    assert db.get(PlatformSetting, "proxy.llm_timeout").value == str(value)

    tuning = resolve_proxy_tuning(db)
    assert tuning.llm_timeout == value

    captured, _posts, coro = _run_proxy_request(_llm_model(), tuning, monkeypatch)
    asyncio.run(coro)
    assert captured == [value], "存進 DB 的逾時沒有抵達 httpx client"


@pytest.mark.parametrize("value", [1, EMBED_TIMEOUT_V, 3600])
def test_embedding_timeout_reaches_the_httpx_client(db, monkeypatch, value):
    """嵌入走的是另一顆 —— 兩顆不可以互相頂替。"""
    set_setting(db, "proxy.embedding_timeout", value)
    # ⚠ LLM 那顆同時設成一個**不同**的值：兩顆都用同一份預設時，
    # 「讀錯顆」與「讀對顆」會一起綠。
    set_setting(db, "proxy.llm_timeout", LLM_TIMEOUT_V)

    tuning = resolve_proxy_tuning(db)
    captured, _posts, coro = _run_proxy_request(
        _embedding_model(), tuning, monkeypatch
    )
    asyncio.run(coro)
    assert captured == [value]
    assert proxy_impl._get_timeout("embedding", tuning) == value
    assert proxy_impl._get_timeout("llm", tuning) == LLM_TIMEOUT_V


@pytest.mark.parametrize("value", [0, MAX_RETRIES_V, 10])
def test_max_retries_bounds_the_number_of_outbound_posts(db, monkeypatch, value):
    """重試次數的下游是**真的送出去幾次**，不是某個變數的值。

    上界 10 也一起跑：把迴圈寫成 ``range(min(n, 3))`` 之類的封頂會在這裡紅。
    """
    set_setting(db, "proxy.max_retries", value)
    set_setting(db, "proxy.retry_base_delay", 0.0)
    tuning = resolve_proxy_tuning(db)

    slept: list[float] = []

    async def _no_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(proxy_impl.asyncio, "sleep", _no_sleep)
    _captured, posts, coro = _run_proxy_request(
        _llm_model(),
        tuning,
        monkeypatch,
        raises=lambda: httpx.ConnectError("boom"),
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(coro)

    assert exc.value.status_code == 502
    assert len(posts) == value, f"設定說重試 {value} 次，實際送出 {len(posts)} 次"
    assert f"已重試 {value} 次" in str(exc.value.detail)


def test_retry_base_delay_sets_the_backoff_series(db, monkeypatch):
    """退避是 ``base * 2**attempt`` —— 釘整條級數，不是只釘第一次。"""
    set_setting(db, "proxy.max_retries", 4)
    set_setting(db, "proxy.retry_base_delay", RETRY_DELAY_V)
    tuning = resolve_proxy_tuning(db)

    slept: list[float] = []

    async def _no_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(proxy_impl.asyncio, "sleep", _no_sleep)
    _captured, _posts, coro = _run_proxy_request(
        _llm_model(),
        tuning,
        monkeypatch,
        raises=lambda: httpx.ConnectError("boom"),
    )
    with pytest.raises(HTTPException):
        asyncio.run(coro)

    assert slept == [
        RETRY_DELAY_V,
        RETRY_DELAY_V * 2,
        RETRY_DELAY_V * 4,
    ], "退避級數不是用存進 DB 的基礎秒數算的"


@pytest.mark.parametrize("value", [0.0, RETRY_DELAY_V, 60.0])
def test_retry_base_delay_round_trip(db, value):
    set_setting(db, "proxy.retry_base_delay", value)
    assert resolve_proxy_tuning(db).retry_base_delay == value


def test_proxy_request_refuses_to_run_without_tuning(db):
    """**呼叫端漏傳 = 當場 TypeError**，不是靜默用預設值。

    ⚠ 這一支釘的是本包的核心設計選擇。四顆的解析在**呼叫端**（那裡才知道
    session 什麼時候被 commit 還回池子、串流什麼時候才被抽乾），所以
    ``proxy_request`` / ``proxy_stream`` 的 ``tuning`` 是**必填**的關鍵字參數。
    若哪天有人給它一個預設值「方便一點」，某條 production 路徑就會靜默凍結在
    程式預設值上 —— 管理員從畫面改了、那條路徑永遠不跟 —— 而且沒有任何錯誤
    訊息。必填讓直譯器替我們檢查全部八個呼叫點，永遠。
    """
    with pytest.raises(TypeError):
        asyncio.run(
            proxy_impl.proxy_request(
                model=_llm_model(),
                api_key_id=None,
                user_id=0,
                department_id=None,
                request_body={},
                endpoint_path="/v1/chat/completions",
            )
        )


def test_proxy_stream_refuses_to_run_without_tuning():
    with pytest.raises(TypeError):
        proxy_impl.proxy_stream(
            target_url="http://ops-knob-llm.test/v1/chat/completions",
            api_key_id=None,
            user_id=0,
            department_id=None,
            usage_model_id=1,
            request_body={},
        ).__anext__()


def test_registry_defaults_helper_is_the_registry_not_a_second_copy():
    """``from_registry_defaults`` 只給沒有 session 的呼叫端；值必須來自登錄表。"""
    tuning = ProxyTuning.from_registry_defaults()
    assert tuning.llm_timeout == REGISTRY["proxy.llm_timeout"].default
    assert tuning.embedding_timeout == REGISTRY["proxy.embedding_timeout"].default
    assert tuning.max_retries == REGISTRY["proxy.max_retries"].default
    assert tuning.retry_base_delay == REGISTRY["proxy.retry_base_delay"].default


# ── 2. 速率限制 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [1, INVOKE_PER_MIN_V, 10000])
def test_invoke_limit_round_trip(db, value):
    set_setting(db, "limits.action_invoke_per_min", value)
    assert message_action_service._invoke_limit(db) == value


def test_rate_limit_blocks_by_the_stored_value_and_reacts_in_process(db):
    """擋人的那一行要讀到存進 DB 的值，而且**同一個行程內**放寬就立刻放行。"""
    set_setting(db, "limits.action_invoke_per_min", 2)
    message_action_service._check_rate_limit(db, 4242)
    message_action_service._check_rate_limit(db, 4242)
    with pytest.raises(HTTPException) as exc:
        message_action_service._check_rate_limit(db, 4242)
    assert exc.value.status_code == 429

    # 洪水當下把上限拉高 —— 下一次呼叫就算數，不必等重啟。
    set_setting(db, "limits.action_invoke_per_min", INVOKE_PER_MIN_V)
    message_action_service._check_rate_limit(db, 4242)


# ── 3. memory 四顆 ──────────────────────────────────────────────────────────


def _stub_embed(monkeypatch):
    async def _fake_embed(db, text_input, **kwargs):
        return [0.1] * 8, "ops-knob-embed", 8

    monkeypatch.setattr(memory_service, "_embed", _fake_embed)


def _capture_retrieval_sql(db, monkeypatch, rows: list[Any]):
    """把 ``retrieve_relevant_chunks`` 那句 SQL 的參數接下來。

    ⚠ 測試 DB 是 SQLite，跑不動 ``CAST(:vec AS halfvec)``；能被觀測的下游參數
    就是送進 ``db.execute`` 的那個 ``LIMIT :k``。門檻那一顆的過濾是 Python 端
    的，所以用假的 cosine 分數列去量它的行為。
    """
    captured: dict[str, Any] = {}
    real_execute = db.execute

    def _fake_execute(statement, params=None, *args, **kwargs):
        if params and "vec" in params:
            captured.update(params)
            return SimpleNamespace(fetchall=lambda: rows)
        return real_execute(statement, params, *args, **kwargs)

    monkeypatch.setattr(db, "execute", _fake_execute)
    return captured


def _chunk_row(idx: int, cosine: float) -> SimpleNamespace:
    return SimpleNamespace(
        id=idx,
        conversation_id=1,
        role="user",
        content=f"chunk-{idx}",
        is_encrypted=False,
        cosine=cosine,
    )


@pytest.mark.parametrize("value", [1, TOP_K_V, 100])
def test_retrieve_top_k_reaches_the_sql_limit(db, monkeypatch, value):
    set_setting(db, "memory.retrieve_top_k", value)
    _stub_embed(monkeypatch)
    captured = _capture_retrieval_sql(db, monkeypatch, [])

    asyncio.run(memory_service.retrieve_relevant_chunks(db, 1, "查一下"))

    assert captured["k"] == value, "存進 DB 的筆數沒有變成 SQL 的 LIMIT"


def test_explicit_top_k_argument_still_wins(db, monkeypatch):
    """呼叫端明講的值優先 —— 這是原本就有的契約，改造不可以吃掉它。"""
    set_setting(db, "memory.retrieve_top_k", TOP_K_V)
    _stub_embed(monkeypatch)
    captured = _capture_retrieval_sql(db, monkeypatch, [])

    asyncio.run(memory_service.retrieve_relevant_chunks(db, 1, "查一下", top_k=2))

    assert captured["k"] == 2


#: 四筆固定的相似度分數。門檻的探針＝有幾筆活下來。
_COSINE_ROWS = [
    _chunk_row(1, 0.9),
    _chunk_row(2, 0.7),
    _chunk_row(3, 0.5),
    _chunk_row(4, 0.2),
]


@pytest.mark.parametrize(
    "value, survivors",
    [(0.0, 4), (MIN_COSINE_V, 2), (1.0, 0)],
)
def test_min_cosine_filters_by_the_stored_threshold(db, monkeypatch, value, survivors):
    """下界（全收）／內插（0.625 → 只剩 0.9 與 0.7）／上界（全不收）。"""
    set_setting(db, "memory.retrieve_min_cosine", value)
    _stub_embed(monkeypatch)
    _capture_retrieval_sql(db, monkeypatch, list(_COSINE_ROWS))

    hits = asyncio.run(memory_service.retrieve_relevant_chunks(db, 1, "查一下"))

    assert len(hits) == survivors


@pytest.mark.parametrize("value, expected", [(1, 1), (MAX_CHUNK_CHARS_V, 37), (100_000, 200)])
def test_max_chunk_chars_truncates_inside_the_memory_block(
    db, monkeypatch, value, expected
):
    """切塊上限的下游是**送進系統提示詞的那段字**的長度。

    刻意走 ``build_memory_block``（而不是直接呼叫 ``_format_block``）：要殺的突變
    是「``build_memory_block`` 傳一個常數下去」，那種寫法只有從外面這一層看得見。
    """
    set_setting(db, "memory.max_chunk_chars", value)

    long_chunk = SimpleNamespace(
        id=1,
        conversation_id=1,
        role="user",
        content="字" * 200,
        cosine=0.99,
        is_encrypted=False,
    )

    async def _fake_retrieve(*args, **kwargs):
        return [long_chunk]

    monkeypatch.setattr(memory_service, "retrieve_relevant_chunks", _fake_retrieve)

    result = asyncio.run(memory_service.build_memory_block(db, 1, "問題"))
    body = result.block
    assert body is not None
    kept = body.split("): ", 1)[1].split("\n", 1)[0].rstrip("…")
    assert len(kept) == expected


def test_format_block_refuses_to_guess_the_chunk_cap():
    """``_format_block`` 沒有預設值可用 —— 漏傳是 TypeError，不是靜默用舊上限。"""
    with pytest.raises(TypeError):
        memory_service._format_block([], [])


@pytest.mark.parametrize("value", [1.0, MEMORY_TIMEOUT_V, 3600.0])
def test_memory_http_timeout_reaches_the_httpx_client(db, monkeypatch, value):
    """記憶抽取那一通 LLM 呼叫的逾時，落在 client 的建構參數上。"""
    from app.models.model_registry import ModelRegistry

    set_setting(db, "memory.http_timeout", value)
    db.add(
        ModelRegistry(
            name=memory_service._LLM_MODEL_NAME,
            display_name=memory_service._LLM_MODEL_NAME,
            model_type="llm",
            endpoint_url="http://memory-extract.test/v1",
            api_version="v1",
            classification_ceiling="密",
            is_active=True,
        )
    )
    db.flush()

    captured: list[Any] = []
    monkeypatch.setattr(memory_service, "_guard_outbound", lambda *a, **k: None)
    monkeypatch.setattr(
        memory_service.httpx,
        "AsyncClient",
        _fake_client_factory(
            captured,
            payload={"choices": [{"message": {"content": "[]"}}]},
        ),
    )

    asyncio.run(memory_service._extract_facts(db, "使用者說他在中科院第七所工作"))

    assert captured == [value]


# ── 4. 回退鏈：DB → env → 程式預設 ─────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Fallback:
    key: str
    #: 回傳**真正抵達下游的那個參數**（不是 ``get_setting`` 自己）。
    probe: Callable[[Session, Any], Any]
    env_raw: str
    env_expected: Any
    #: DB 那一輪要寫的值 —— 與 env 那一層解出來的**不可以相同**。
    distinct: Any


def _tuning_probe(field: str) -> Callable[[Session, Any], Any]:
    def _probe(db: Session, monkeypatch) -> Any:
        return getattr(resolve_proxy_tuning(db), field)

    return _probe


def _top_k_probe(db: Session, monkeypatch) -> Any:
    _stub_embed(monkeypatch)
    captured = _capture_retrieval_sql(db, monkeypatch, [])
    asyncio.run(memory_service.retrieve_relevant_chunks(db, 1, "查一下"))
    return captured["k"]


def _min_cosine_probe(db: Session, monkeypatch) -> Any:
    _stub_embed(monkeypatch)
    _capture_retrieval_sql(db, monkeypatch, list(_COSINE_ROWS))
    return len(asyncio.run(memory_service.retrieve_relevant_chunks(db, 1, "查一下")))


def _chunk_chars_probe(db: Session, monkeypatch) -> Any:
    async def _fake_retrieve(*args, **kwargs):
        return [
            SimpleNamespace(
                id=1,
                conversation_id=1,
                role="user",
                content="字" * 5000,
                cosine=0.99,
                is_encrypted=False,
            )
        ]

    monkeypatch.setattr(memory_service, "retrieve_relevant_chunks", _fake_retrieve)
    block = asyncio.run(memory_service.build_memory_block(db, 1, "問題")).block
    return len(block.split("): ", 1)[1].split("\n", 1)[0].rstrip("…"))


def _invoke_limit_probe(db: Session, monkeypatch) -> Any:
    return message_action_service._invoke_limit(db)


def _http_timeout_probe(db: Session, monkeypatch) -> Any:
    from app.models.model_registry import ModelRegistry

    existing = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == memory_service._LLM_MODEL_NAME)
        .first()
    )
    if existing is None:
        db.add(
            ModelRegistry(
                name=memory_service._LLM_MODEL_NAME,
                display_name=memory_service._LLM_MODEL_NAME,
                model_type="llm",
                endpoint_url="http://memory-extract.test/v1",
                api_version="v1",
                classification_ceiling="密",
                is_active=True,
            )
        )
        db.flush()
    captured: list[Any] = []
    monkeypatch.setattr(memory_service, "_guard_outbound", lambda *a, **k: None)
    monkeypatch.setattr(
        memory_service.httpx,
        "AsyncClient",
        _fake_client_factory(
            captured, payload={"choices": [{"message": {"content": "[]"}}]}
        ),
    )
    asyncio.run(memory_service._extract_facts(db, "使用者說他在中科院第七所工作"))
    return captured[0]


FALLBACKS: tuple[Fallback, ...] = (
    Fallback("proxy.llm_timeout", _tuning_probe("llm_timeout"), "211", 211, LLM_TIMEOUT_V),
    Fallback(
        "proxy.embedding_timeout",
        _tuning_probe("embedding_timeout"),
        "212",
        212,
        EMBED_TIMEOUT_V,
    ),
    Fallback("proxy.max_retries", _tuning_probe("max_retries"), "9", 9, MAX_RETRIES_V),
    Fallback(
        "proxy.retry_base_delay",
        _tuning_probe("retry_base_delay"),
        "0.875",
        0.875,
        RETRY_DELAY_V,
    ),
    Fallback(
        "limits.action_invoke_per_min", _invoke_limit_probe, "913", 913, INVOKE_PER_MIN_V
    ),
    Fallback("memory.retrieve_top_k", _top_k_probe, "11", 11, TOP_K_V),
    # env 0.75 → 只有 0.9 那一筆活下來（1 筆）；DB 0.625 → 2 筆。
    Fallback("memory.retrieve_min_cosine", _min_cosine_probe, "0.75", 1, MIN_COSINE_V),
    Fallback("memory.max_chunk_chars", _chunk_chars_probe, "88", 88, MAX_CHUNK_CHARS_V),
    Fallback("memory.http_timeout", _http_timeout_probe, "22.5", 22.5, MEMORY_TIMEOUT_V),
)

_FB_IDS = [f.key for f in FALLBACKS]


@pytest.mark.parametrize("fb", FALLBACKS, ids=_FB_IDS)
def test_env_still_works_when_no_one_has_touched_the_page(db, fb, monkeypatch):
    """氣隙升級的前提：容器裡那個舊 env 在管理員第一次改之前照樣算數。"""
    monkeypatch.setenv(REGISTRY[fb.key].env_name, fb.env_raw)
    assert db.get(PlatformSetting, fb.key) is None
    assert fb.probe(db, monkeypatch) == fb.env_expected


@pytest.mark.parametrize("fb", FALLBACKS, ids=_FB_IDS)
def test_db_row_beats_env(db, fb, monkeypatch):
    """管理員從畫面改過之後，那個舊 env 就不可以再說話。"""
    monkeypatch.setenv(REGISTRY[fb.key].env_name, fb.env_raw)
    set_setting(db, fb.key, fb.distinct)
    result = fb.probe(db, monkeypatch)
    assert result != fb.env_expected, f"{fb.key}：兩層的探針結果一樣，分不出誰贏"


@pytest.mark.parametrize("fb", FALLBACKS, ids=_FB_IDS)
def test_takes_effect_without_restart(db, fb, monkeypatch):
    """同一個行程內改完即生效 —— 專殺「首次呼叫後記憶」與 import 期擷取。"""
    first = fb.probe(db, monkeypatch)
    set_setting(db, fb.key, fb.distinct)
    second = fb.probe(db, monkeypatch)
    assert second != first, f"{fb.key}：第一次讀之後就記住了，改了要等重啟才生效"


# ── 5. 下限：拒絕之後，生效值仍然是舊的那一個（不是半套用） ─────────────────


FLOOR_CASES = (
    ("proxy.llm_timeout", LLM_TIMEOUT_V, (0, -1, 3601)),
    ("proxy.embedding_timeout", EMBED_TIMEOUT_V, (0, -1, 3601)),
    ("proxy.max_retries", MAX_RETRIES_V, (-1, 11)),
    ("proxy.retry_base_delay", RETRY_DELAY_V, (-0.5, 60.5)),
    ("limits.action_invoke_per_min", INVOKE_PER_MIN_V, (0, 10001)),
    ("memory.retrieve_top_k", TOP_K_V, (0, 101)),
    ("memory.retrieve_min_cosine", MIN_COSINE_V, (-0.1, 1.1)),
    ("memory.max_chunk_chars", MAX_CHUNK_CHARS_V, (0, 100_001)),
    ("memory.http_timeout", MEMORY_TIMEOUT_V, (0.0, 0.5, 3600.5)),
)


@pytest.mark.parametrize(
    "key, good, bad_values", FLOOR_CASES, ids=[c[0] for c in FLOOR_CASES]
)
def test_out_of_domain_write_is_refused_and_the_effective_value_survives(
    db, key, good, bad_values
):
    """**下限是命門。** 逾時填 0 等於從畫面把平台打掛，重試填 11 等於放大故障。

    釘兩件事，因為只釘第一件會漏掉最難查的那種壞法：

    1. 寫入被拒（``ValueError``）。
    2. **生效值仍然是上一個好值** —— 不是預設值、也不是半套用的壞值。
       「存進去了、解析端卻判定它不可用」正是門檻那顆吃過的虧
       （``platform_setting.py:82-88``）。
    """
    set_setting(db, key, good)
    assert get_setting(db, key) == good

    for bad in bad_values:
        with pytest.raises(ValueError):
            set_setting(db, key, bad)
        assert get_setting(db, key) == good, (
            f"{key}：拒了 {bad!r} 之後生效值卻不是原本的 {good!r}"
        )
        assert db.get(PlatformSetting, key).value == REGISTRY[key].value_type.format(
            good
        ), f"{key}：被拒的值還是寫進了那一列"


def test_zero_timeout_cannot_reach_the_httpx_client(db, monkeypatch):
    """行為層的同一件事：0 秒逾時到不了 client，平台不會被一個數字打掛。"""
    set_setting(db, "proxy.llm_timeout", LLM_TIMEOUT_V)
    with pytest.raises(ValueError):
        set_setting(db, "proxy.llm_timeout", 0)

    tuning = resolve_proxy_tuning(db)
    captured, _posts, coro = _run_proxy_request(_llm_model(), tuning, monkeypatch)
    asyncio.run(coro)
    assert captured == [LLM_TIMEOUT_V]


# ── 6. 端到端：production 的呼叫點真的把 session 解出來的值傳下去 ─────────────


def test_chat_endpoint_carries_the_stored_timeout_all_the_way_out(
    client, db, monkeypatch
):
    """從 HTTP 請求走到 httpx client 的建構參數 —— 中間沒有一段是凍結的。

    ⚠ 這一支釘的是**呼叫點有沒有把值傳下去**。``proxy_request`` 的 ``tuning`` 是
    必填的，所以「整段漏掉」會是 TypeError；但「傳了一個常數下去」（例如
    ``ProxyTuning.from_registry_defaults()``）不會有任何錯誤訊息 —— 它會安靜地
    讓管理員從畫面改的逾時永遠到不了 chat 這條路。DB 存 77（≠ 登錄表 120、
    ≠ compose 300），出向就必須是 77。
    """
    from app.services.auth_service import create_tokens
    from tests.conftest import make_model, make_user

    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "mock-llm")

    admin = make_user(db, username="ops_knob_e2e", role="admin")
    model = make_model(db, name="ops-knob-e2e-llm")
    set_setting(db, "proxy.llm_timeout", LLM_TIMEOUT_V)
    db.commit()

    captured: list[Any] = []
    monkeypatch.setattr(
        proxy_impl.httpx,
        "AsyncClient",
        _fake_client_factory(
            captured,
            payload={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2},
            },
        ),
    )

    token = create_tokens(admin)["access_token"]
    resp = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": model.name,
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text
    assert captured == [LLM_TIMEOUT_V], (
        "chat 端點沒有把 platform_settings 那一列的逾時傳到出向呼叫"
    )


def test_adapter_path_reads_the_setting_instead_of_its_own_defaults(db, monkeypatch):
    """``PostgresMemoryAdapter`` 那條路也要拿到設定值。

    ⚠ 它的簽章原本是 ``top_k: int = 3, min_cosine: float = 0.4`` —— 兩個字面值
    **剛好等於登錄表的預設值**，所以「有讀到設定」與「用自己那份預設」在這條路徑上
    分不出來，而管理員改的值永遠到不了。這一支就是那個縫的釘子。
    """
    set_setting(db, "memory.retrieve_top_k", TOP_K_V)
    _stub_embed(monkeypatch)
    captured = _capture_retrieval_sql(db, monkeypatch, [])

    adapter = memory_service.PostgresMemoryAdapter(db_factory=lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)
    asyncio.run(adapter.retrieve_relevant_chunks(1, "查一下"))

    assert captured["k"] == TOP_K_V


# ── 7. 解析的**時點**：在把池化連線還回去之前 ──────────────────────────────
#
# 這一段釘的是本包設計的地基。四顆／一顆的值都在 ``db.commit()`` **之前**解析，
# 因為那個 commit 是刻意把池化連線還回池子再走出向 HTTP 的。順序反過來不會有
# 任何錯誤訊息 —— 只會讓每一次出向呼叫都在 HTTP 期間握著一條 DB 連線，而
# ``test_embed_query_releases_pool.py`` 是 stub 掉 ``proxy_request`` 的，抓不到。


def test_memory_timeout_is_resolved_before_the_pool_releasing_commit(db, monkeypatch):
    from app.models.model_registry import ModelRegistry

    db.add(
        ModelRegistry(
            name=memory_service._LLM_MODEL_NAME,
            display_name=memory_service._LLM_MODEL_NAME,
            model_type="llm",
            endpoint_url="http://memory-extract.test/v1",
            api_version="v1",
            classification_ceiling="密",
            is_active=True,
        )
    )
    db.flush()

    order: list[str] = []
    real_get = memory_service.get_setting

    def _spy_get(session, key):
        if key == "memory.http_timeout":
            order.append("resolve")
        return real_get(session, key)

    real_commit = db.commit

    def _spy_commit():
        order.append("commit")
        real_commit()

    monkeypatch.setattr(memory_service, "get_setting", _spy_get)
    monkeypatch.setattr(db, "commit", _spy_commit)
    monkeypatch.setattr(memory_service, "_guard_outbound", lambda *a, **k: None)
    monkeypatch.setattr(
        memory_service.httpx,
        "AsyncClient",
        _fake_client_factory([], payload={"choices": [{"message": {"content": "[]"}}]}),
    )

    asyncio.run(memory_service._extract_facts(db, "使用者說他在中科院第七所工作"))

    assert order == ["resolve", "commit"], (
        "逾時是在連線還回池子之後才解的 —— 每一次抽取都會在 LLM 回應期間握著一條連線"
    )


def test_embed_resolves_the_tuning_before_the_pool_releasing_commit(db, monkeypatch):
    import app.services.proxy.service as proxy_svc
    from app.models.model_registry import ModelRegistry

    db.add(
        ModelRegistry(
            name="ops-knob-platform-embed",
            display_name="ops-knob-platform-embed",
            model_type="embedding",
            endpoint_url="http://embed.test/v1",
            api_version="v1",
            classification_ceiling="密",
            is_active=True,
            is_platform_embedding=True,
            embedding_native_dim=8,
        )
    )
    db.commit()

    order: list[str] = []
    real_resolve = proxy_svc.resolve_proxy_tuning

    def _spy_resolve(session):
        order.append("resolve")
        return real_resolve(session)

    real_commit = db.commit

    def _spy_commit():
        order.append("commit")
        real_commit()

    async def _fake_proxy_request(**kwargs):
        order.append("outbound")
        assert kwargs["tuning"] is not None
        return {"data": [{"embedding": [0.1] * 8}]}

    monkeypatch.setattr(proxy_svc, "resolve_proxy_tuning", _spy_resolve)
    monkeypatch.setattr(proxy_svc, "proxy_request", _fake_proxy_request)
    monkeypatch.setattr(db, "commit", _spy_commit)

    asyncio.run(memory_service._embed(db, "hello", embedding_input_role="query"))

    assert order == ["resolve", "commit", "outbound"]


def test_search_embed_query_carries_the_tuning_it_resolved(db, monkeypatch):
    """search 那個呼叫點也要把 session 解出來的值傳下去（而不是一份預設）。"""
    from app.api.ingestion import search as search_mod
    from app.models.model_registry import ModelRegistry
    from tests.conftest import make_user

    user = make_user(db, username="ops_knob_search")
    db.add(
        ModelRegistry(
            name="ops-knob-search-embed",
            display_name="ops-knob-search-embed",
            model_type="embedding",
            endpoint_url="http://embed.test/v1",
            api_version="v1",
            is_active=True,
        )
    )
    db.commit()
    set_setting(db, "proxy.embedding_timeout", EMBED_TIMEOUT_V)
    db.commit()

    seen: dict[str, Any] = {}

    async def _fake_proxy_request(**kwargs):
        seen["tuning"] = kwargs.get("tuning")
        return {"data": [{"embedding": [0.1, 0.2, 0.3, 0.4]}]}

    monkeypatch.setattr(search_mod, "proxy_request", _fake_proxy_request)

    asyncio.run(
        search_mod._embed_query(db, user, "ops-knob-search-embed", 4, "去年的採購紀錄")
    )

    assert seen["tuning"].embedding_timeout == EMBED_TIMEOUT_V


def test_no_production_call_site_uses_the_registry_defaults_helper():
    """``ProxyTuning.from_registry_defaults()`` 只給測試用。

    ⚠ 這一支守的是「呼叫點凍結」那種突變：``tuning`` 必填擋得住**漏傳**，但擋不住
    「傳一份預設值下去」—— 那不會有錯誤訊息，只會讓管理員改的逾時到不了那條路。
    八個 production 呼叫點裡任何一個改用這個 helper，這裡就會紅。
    （定義它的那個檔案自己除外。）
    """
    import pathlib

    import app

    root = pathlib.Path(app.__file__).parent
    definition = root / "services" / "proxy" / "service.py"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path != definition and "from_registry_defaults" in path.read_text("utf-8")
    ]
    assert offenders == [], (
        f"production 呼叫點用了測試專用的預設值 helper：{offenders}"
    )
