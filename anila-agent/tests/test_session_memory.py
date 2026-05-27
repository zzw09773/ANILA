"""P2-6 SessionMemory + autoDream tests.

- ``SessionMemory`` dataclass + JSON round-trip
- ``JsonFileSessionMemoryStore`` save / load / find_by_topic / list_all / delete
- ``AutoDreamer.dream`` 用 mock LLM 驗證 prompt shape + 解析 + dedup
- ``SessionMemoryRetriever.recall_for_current`` 找相關 session
- integration with P1-10 ``UserContextBuilder.add_memory``
- 背景 ``dream_in_background`` task lifecycle(成功 / 失敗)
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from anila_agent.memory import (
    DREAM_PROMPT_TEMPLATE,
    AutoDreamer,
    JsonFileSessionMemoryStore,
    SessionMemory,
    SessionMemoryRetriever,
    SessionMemoryStore,
    compute_conversation_hash,
)
from anila_agent.prompts.prompt_builder import UserContextBuilder

# ---------------------------------------------------------------------------
# SessionMemory dataclass + JSON
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_memory_is_frozen() -> None:
    m = SessionMemory(session_id="s1", summary="hello")
    with pytest.raises(FrozenInstanceError):
        m.session_id = "s2"  # type: ignore[misc]


@pytest.mark.unit
def test_session_memory_to_from_json_roundtrip() -> None:
    m = SessionMemory(
        session_id="s-42",
        summary="處理使用者問 retriever 設定的問題",
        topics=("retriever", "config"),
        lessons=("先確認 yaml 路徑",),
        started_at="2026-05-27T01:00:00Z",
        ended_at="2026-05-27T01:30:00Z",
        message_count=12,
        conversation_hash="deadbeef",
        model="gpt-test",
    )
    raw = m.to_json()
    restored = SessionMemory.from_json(raw)
    assert restored == m
    # tuple round-trip 不變成 list
    assert isinstance(restored.topics, tuple)
    assert isinstance(restored.lessons, tuple)


@pytest.mark.unit
def test_session_memory_from_dict_missing_required() -> None:
    with pytest.raises(ValueError):
        SessionMemory.from_dict({"summary": "no id"})
    with pytest.raises(ValueError):
        SessionMemory.from_dict({"session_id": "s1"})


@pytest.mark.unit
def test_session_memory_from_json_bad_json() -> None:
    with pytest.raises(ValueError):
        SessionMemory.from_json("not json")
    with pytest.raises(ValueError):
        SessionMemory.from_json('["array not object"]')


@pytest.mark.unit
def test_to_json_is_deterministic() -> None:
    m = SessionMemory(
        session_id="s1",
        summary="x",
        topics=("a", "b"),
        lessons=("y",),
    )
    assert m.to_json() == m.to_json()


@pytest.mark.unit
def test_compute_conversation_hash_stable_across_extra_keys() -> None:
    msgs_a = [
        {"role": "user", "content": "hi", "timestamp": 1},
        {"role": "assistant", "content": "hello", "id": "msg-1"},
    ]
    msgs_b = [
        {"role": "user", "content": "hi", "timestamp": 999},
        {"role": "assistant", "content": "hello", "id": "msg-2"},
    ]
    # 額外 key(timestamp / id)不影響 hash
    assert compute_conversation_hash(msgs_a) == compute_conversation_hash(msgs_b)


@pytest.mark.unit
def test_compute_conversation_hash_differs_on_content() -> None:
    a = [{"role": "user", "content": "x"}]
    b = [{"role": "user", "content": "y"}]
    assert compute_conversation_hash(a) != compute_conversation_hash(b)


# ---------------------------------------------------------------------------
# JsonFileSessionMemoryStore
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_store(tmp_path: Path) -> JsonFileSessionMemoryStore:
    return JsonFileSessionMemoryStore(tmp_path / "session_memory")


@pytest.mark.unit
def test_store_save_load_roundtrip(session_store: JsonFileSessionMemoryStore) -> None:
    m = SessionMemory(
        session_id="s-100",
        summary="debug retriever pgvector",
        topics=("pgvector", "retriever"),
        lessons=("確認 schema 先",),
        ended_at="2026-05-27T02:00:00Z",
        conversation_hash="abc",
    )
    path = session_store.save(m)
    assert path.exists()
    assert path.name == "s-100.json"

    loaded = session_store.load("s-100")
    assert loaded == m


@pytest.mark.unit
def test_store_load_missing_returns_none(session_store: JsonFileSessionMemoryStore) -> None:
    assert session_store.load("not-there") is None


@pytest.mark.unit
def test_store_load_bad_file_returns_none(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    bad = session_store.path_for("garbage")
    bad.write_text("{not json", encoding="utf-8")
    assert session_store.load("garbage") is None


@pytest.mark.unit
def test_store_rejects_path_traversal(session_store: JsonFileSessionMemoryStore) -> None:
    with pytest.raises(ValueError):
        session_store.path_for("../escape")
    with pytest.raises(ValueError):
        session_store.path_for("")


@pytest.mark.unit
def test_store_save_overwrites_same_id(session_store: JsonFileSessionMemoryStore) -> None:
    m1 = SessionMemory(session_id="s1", summary="v1")
    m2 = SessionMemory(session_id="s1", summary="v2")
    session_store.save(m1)
    session_store.save(m2)
    loaded = session_store.load("s1")
    assert loaded is not None
    assert loaded.summary == "v2"


@pytest.mark.unit
def test_store_find_by_topic_orders_by_overlap(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    session_store.save(
        SessionMemory(
            session_id="s-retriever",
            summary="retriever pgvector debug",
            topics=("retriever", "pgvector"),
            ended_at="2026-05-27T01:00:00Z",
        )
    )
    session_store.save(
        SessionMemory(
            session_id="s-ui",
            summary="frontend ui tweak",
            topics=("frontend", "css"),
            ended_at="2026-05-27T02:00:00Z",
        )
    )
    session_store.save(
        SessionMemory(
            session_id="s-mixed",
            summary="add retriever css fix",
            topics=("retriever", "css"),
            ended_at="2026-05-27T00:30:00Z",
        )
    )
    hits = session_store.find_by_topic("retriever pgvector", top_k=3)
    assert hits[0].session_id == "s-retriever"  # overlap 2 wins
    ids = [h.session_id for h in hits]
    assert "s-mixed" in ids
    assert "s-ui" not in ids  # 0 overlap


@pytest.mark.unit
def test_store_find_by_topic_empty_topic(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    session_store.save(SessionMemory(session_id="s1", summary="x", topics=("a",)))
    assert session_store.find_by_topic("", top_k=3) == []
    assert session_store.find_by_topic("   ", top_k=3) == []


@pytest.mark.unit
def test_store_find_by_topic_zero_top_k(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    session_store.save(SessionMemory(session_id="s1", summary="retriever"))
    assert session_store.find_by_topic("retriever", top_k=0) == []


@pytest.mark.unit
def test_store_list_all_orders_by_ended_at_desc(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    session_store.save(
        SessionMemory(session_id="old", summary="x", ended_at="2026-01-01T00:00:00Z")
    )
    session_store.save(
        SessionMemory(session_id="new", summary="x", ended_at="2026-05-27T00:00:00Z")
    )
    session_store.save(SessionMemory(session_id="none", summary="x"))
    ids = [m.session_id for m in session_store.list_all()]
    assert ids[0] == "new"
    assert ids[1] == "old"
    assert ids[2] == "none"


@pytest.mark.unit
def test_store_list_all_with_limit(session_store: JsonFileSessionMemoryStore) -> None:
    for i in range(5):
        session_store.save(SessionMemory(session_id=f"s{i}", summary="x"))
    assert len(session_store.list_all(limit=2)) == 2
    assert len(session_store.list_all(limit=0)) == 0


@pytest.mark.unit
def test_store_delete(session_store: JsonFileSessionMemoryStore) -> None:
    session_store.save(SessionMemory(session_id="s1", summary="x"))
    assert session_store.delete("s1") is True
    assert session_store.delete("s1") is False
    assert session_store.load("s1") is None


@pytest.mark.unit
def test_store_satisfies_protocol(session_store: JsonFileSessionMemoryStore) -> None:
    assert isinstance(session_store, SessionMemoryStore)


@pytest.mark.unit
def test_store_skips_bad_file_in_iter(session_store: JsonFileSessionMemoryStore) -> None:
    session_store.save(SessionMemory(session_id="good", summary="x"))
    (session_store.dir / "bad.json").write_text("{not json", encoding="utf-8")
    ids = [m.session_id for m in session_store.list_all()]
    assert ids == ["good"]


# ---------------------------------------------------------------------------
# AutoDreamer
# ---------------------------------------------------------------------------


class _FakeLlm:
    """記錄收到的 prompt,回固定 response。"""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.response


@pytest.mark.unit
async def test_dream_parses_strict_json_and_saves(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    response = json.dumps(
        {
            "summary": "處理 retriever 問題",
            "topics": ["retriever", "pgvector"],
            "lessons": ["先 check schema"],
        }
    )
    llm = _FakeLlm(response)
    dreamer = AutoDreamer(llm, session_store, model_name="gpt-test")

    messages = [
        {"role": "user", "content": "retriever 怎麼設?"},
        {"role": "assistant", "content": "看 yaml"},
    ]
    memory = await dreamer.dream(messages, session_id="s-dream", started_at="2026-05-27T00:00:00Z")

    assert memory.summary == "處理 retriever 問題"
    assert memory.topics == ("retriever", "pgvector")
    assert memory.lessons == ("先 check schema",)
    assert memory.session_id == "s-dream"
    assert memory.message_count == 2
    assert memory.model == "gpt-test"
    assert memory.conversation_hash  # 非空

    # 持久化了
    assert session_store.load("s-dream") == memory


@pytest.mark.unit
async def test_dream_prompt_shape_contains_required_fields(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    llm = _FakeLlm('{"summary": "x", "topics": [], "lessons": []}')
    dreamer = AutoDreamer(llm, session_store)
    messages = [
        {"role": "user", "content": "問題 A"},
        {"role": "assistant", "content": "答案 B"},
    ]
    await dreamer.dream(messages, session_id="probe")

    assert len(llm.calls) == 1
    prompt = llm.calls[0]
    # session id / message count / transcript role 都進 prompt
    assert "probe" in prompt
    assert "Message count: 2" in prompt
    assert "[user]" in prompt
    assert "[assistant]" in prompt
    assert "問題 A" in prompt
    assert "答案 B" in prompt
    # 預設 template 要求嚴格 JSON
    assert "STRICT JSON" in prompt


@pytest.mark.unit
async def test_dream_handles_markdown_fenced_json(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    response = (
        "Sure, here is the result:\n"
        "```json\n"
        '{"summary": "s", "topics": ["t1"], "lessons": []}\n'
        "```\n"
    )
    dreamer = AutoDreamer(_FakeLlm(response), session_store)
    memory = await dreamer.dream(
        [{"role": "user", "content": "hi"}], session_id="fence"
    )
    assert memory.summary == "s"
    assert memory.topics == ("t1",)


@pytest.mark.unit
async def test_dream_handles_broken_json(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    dreamer = AutoDreamer(_FakeLlm("totally not json"), session_store)
    memory = await dreamer.dream(
        [{"role": "user", "content": "hi"}], session_id="broken"
    )
    # 不 crash;memory 落地,只是空欄位
    assert memory.summary == ""
    assert memory.topics == ()
    assert memory.lessons == ()
    assert session_store.load("broken") == memory


@pytest.mark.unit
async def test_dream_handles_llm_exception(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    async def boom(prompt: str) -> str:
        raise RuntimeError("upstream down")

    dreamer = AutoDreamer(boom, session_store)
    memory = await dreamer.dream(
        [{"role": "user", "content": "hi"}], session_id="boom"
    )
    # LLM 炸了仍寫一筆空 memory(避免重複 retry 無限 dream)
    assert memory.summary == ""
    assert session_store.load("boom") is not None


@pytest.mark.unit
async def test_dream_dedup_same_hash_skips_llm(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    llm = _FakeLlm('{"summary": "first", "topics": [], "lessons": []}')
    dreamer = AutoDreamer(llm, session_store)
    msgs = [{"role": "user", "content": "same"}]
    m1 = await dreamer.dream(msgs, session_id="dedup")
    m2 = await dreamer.dream(msgs, session_id="dedup")
    assert m1 == m2
    assert len(llm.calls) == 1  # 第二次沒 call LLM


@pytest.mark.unit
async def test_dream_re_runs_when_messages_changed(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    llm = _FakeLlm('{"summary": "v", "topics": [], "lessons": []}')
    dreamer = AutoDreamer(llm, session_store)
    await dreamer.dream([{"role": "user", "content": "v1"}], session_id="chg")
    await dreamer.dream([{"role": "user", "content": "v2"}], session_id="chg")
    assert len(llm.calls) == 2  # hash 不同,要重跑


@pytest.mark.unit
async def test_dream_topics_wrong_type_ignored(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    response = json.dumps(
        {"summary": "s", "topics": "not a list", "lessons": 123}
    )
    dreamer = AutoDreamer(_FakeLlm(response), session_store)
    memory = await dreamer.dream(
        [{"role": "user", "content": "hi"}], session_id="bad-types"
    )
    assert memory.summary == "s"
    assert memory.topics == ()
    assert memory.lessons == ()


@pytest.mark.unit
async def test_dream_renders_list_content_blocks(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    llm = _FakeLlm('{"summary": "x", "topics": [], "lessons": []}')
    dreamer = AutoDreamer(llm, session_store)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "first block"},
                {"type": "text", "text": "second block"},
            ],
        }
    ]
    await dreamer.dream(messages, session_id="blocks")
    prompt = llm.calls[0]
    assert "first block" in prompt
    assert "second block" in prompt


@pytest.mark.unit
async def test_dream_truncates_huge_message_in_prompt(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    llm = _FakeLlm('{"summary": "x", "topics": [], "lessons": []}')
    dreamer = AutoDreamer(llm, session_store)
    huge = "x" * 10_000
    await dreamer.dream(
        [{"role": "user", "content": huge}], session_id="huge"
    )
    prompt = llm.calls[0]
    assert "(truncated)" in prompt


@pytest.mark.unit
async def test_dream_custom_prompt_template(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    custom = (
        "CUSTOM PROMPT for {session_id} with {message_count} msgs.\n"
        "Transcript:\n{transcript}\n"
        '{{"summary": "x", "topics": [], "lessons": []}}'
    )
    llm = _FakeLlm('{"summary": "ok", "topics": [], "lessons": []}')
    dreamer = AutoDreamer(llm, session_store, prompt_template=custom)
    await dreamer.dream(
        [{"role": "user", "content": "hi"}], session_id="custom-id"
    )
    assert "CUSTOM PROMPT for custom-id" in llm.calls[0]


@pytest.mark.unit
def test_default_prompt_template_has_placeholders() -> None:
    # 確保 caller 改 template 時知道有哪些 placeholder
    assert "{session_id}" in DREAM_PROMPT_TEMPLATE
    assert "{message_count}" in DREAM_PROMPT_TEMPLATE
    assert "{transcript}" in DREAM_PROMPT_TEMPLATE


# ---------------------------------------------------------------------------
# 背景 dream task lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_dream_in_background_returns_task_and_saves(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    llm = _FakeLlm('{"summary": "bg", "topics": [], "lessons": []}')
    dreamer = AutoDreamer(llm, session_store)
    task = dreamer.dream_in_background(
        [{"role": "user", "content": "hi"}], session_id="bg-1"
    )
    assert isinstance(task, asyncio.Task)
    memory = await task
    assert memory.summary == "bg"
    assert session_store.load("bg-1") == memory


@pytest.mark.unit
async def test_dream_in_background_failure_does_not_propagate(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    # 給一個會 raise 的 store(save 階段失敗)模擬 task internal 例外
    class BoomStore:
        def save(self, memory: SessionMemory) -> None:
            raise RuntimeError("disk full")

        def load(self, session_id: str) -> SessionMemory | None:
            return None

        def find_by_topic(self, topic: str, top_k: int = 3) -> list[SessionMemory]:
            return []

        def list_all(self, limit: int | None = None) -> list[SessionMemory]:
            return []

        def delete(self, session_id: str) -> bool:
            return False

    llm = _FakeLlm('{"summary": "x", "topics": [], "lessons": []}')
    dreamer = AutoDreamer(llm, BoomStore())
    task = dreamer.dream_in_background(
        [{"role": "user", "content": "hi"}], session_id="bg-fail"
    )
    # task 拋例外但被 done_callback log,不影響 caller
    with pytest.raises(RuntimeError):
        await task


# ---------------------------------------------------------------------------
# SessionMemoryRetriever
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_retriever_recall_for_current_finds_relevant(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    session_store.save(
        SessionMemory(
            session_id="prev",
            summary="retriever pgvector 設定問題",
            topics=("retriever", "pgvector"),
        )
    )
    session_store.save(
        SessionMemory(
            session_id="other",
            summary="frontend",
            topics=("frontend",),
        )
    )
    retriever = SessionMemoryRetriever(session_store)
    current = [
        {"role": "user", "content": "我要問 retriever 怎麼接 pgvector"},
        {"role": "assistant", "content": "看 docs"},
    ]
    hits = retriever.recall_for_current(current, top_k=3)
    assert [h.session_id for h in hits] == ["prev"]


@pytest.mark.unit
def test_retriever_recall_zero_top_k(session_store: JsonFileSessionMemoryStore) -> None:
    session_store.save(SessionMemory(session_id="x", summary="retriever"))
    retriever = SessionMemoryRetriever(session_store)
    assert retriever.recall_for_current(
        [{"role": "user", "content": "retriever"}], top_k=0
    ) == []


@pytest.mark.unit
def test_retriever_recall_empty_when_no_user_msg(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    session_store.save(SessionMemory(session_id="x", summary="retriever"))
    retriever = SessionMemoryRetriever(session_store)
    # 只有 assistant — 沒 query
    assert retriever.recall_for_current(
        [{"role": "assistant", "content": "hi"}], top_k=3
    ) == []


@pytest.mark.unit
def test_retriever_handles_list_content_blocks(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    session_store.save(
        SessionMemory(session_id="s1", summary="retriever debug", topics=("retriever",))
    )
    retriever = SessionMemoryRetriever(session_store)
    current = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "retriever issue"}],
        }
    ]
    hits = retriever.recall_for_current(current, top_k=3)
    assert hits and hits[0].session_id == "s1"


@pytest.mark.unit
def test_retriever_to_memory_items_shape() -> None:
    memories = [
        SessionMemory(
            session_id="s1",
            summary="retriever 問題",
            topics=("retriever", "config"),
            lessons=("看 yaml", "再 check schema"),
        ),
        SessionMemory(session_id="s2", summary="只有 summary"),
    ]
    items = SessionMemoryRetriever.to_memory_items(memories)
    assert len(items) == 2
    first = items[0]
    assert first["kind"] == "session_memory"
    assert first["session_id"] == "s1"
    assert "retriever 問題" in first["text"]
    assert "topics: retriever, config" in first["text"]
    assert "lessons: 看 yaml // 再 check schema" in first["text"]
    # 沒 topics / lessons 的 memory 不會塞那兩段
    second = items[1]
    assert "topics:" not in second["text"]
    assert "lessons:" not in second["text"]


# ---------------------------------------------------------------------------
# integration with P1-10 UserContextBuilder
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_retriever_feeds_user_context_builder(
    session_store: JsonFileSessionMemoryStore,
) -> None:
    session_store.save(
        SessionMemory(
            session_id="hist-1",
            summary="設定 retriever pgvector",
            topics=("retriever", "pgvector"),
            lessons=("先看 schema",),
        )
    )
    retriever = SessionMemoryRetriever(session_store)
    current = [{"role": "user", "content": "retriever 又掛了"}]
    recalled = retriever.recall_for_current(current, top_k=3)

    builder = UserContextBuilder()
    builder.add_memory(SessionMemoryRetriever.to_memory_items(recalled))
    builder.add_input("我 retriever 又掛了,有人遇過嗎?")
    messages = builder.build()

    # 至少要有 system-reminder + 最後 user input
    assert messages[-1]["role"] == "user"
    assert "retriever 又掛了" in messages[-1]["content"]
    # memory 段該包含 summary 與 topics
    reminder = "\n".join(
        m["content"] for m in messages if m["role"] == "user" and m is not messages[-1]
    )
    assert "hist-1" in reminder
    assert "retriever" in reminder
