"""Real search schema/API/helper -> runtime final-message context budgeting."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.api.ingestion import search
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.services import quick_flow as flow
from app.services.quick_retrieval import MAX_CONTEXT_BYTES, _size, retrieve_collections
from tests.conftest import make_user


def node(ident, kind, **config):
    return {"id": ident, "kind": kind, "config": config}


def chain(*nodes):
    rows = [
        node("input", "input"),
        *nodes,
        node("output", "output", include_sources=False),
    ]
    return {
        "nodes": rows,
        "edges": [
            {"source": a["id"], "target": b["id"]} for a, b in zip(rows, rows[1:])
        ],
    }


@pytest.fixture
def corpus(db, monkeypatch):
    user = make_user(db, username="rag-budget-reader", role="user")
    outsider = make_user(db, username="rag-budget-outsider", role="user")
    ids, docs = [], {}
    for number in range(3):
        coll = IngestionCollection(
            name=f"budget-{number}",
            chunking_config={},
            embedding_dim=2,
            embedding_model=None,
            status="active",
            created_by=user.id,
        )
        db.add(coll)
        db.commit()
        doc = IngestionDocument(
            collection_id=coll.id,
            filename=f"policy-{number}.txt",
            sha256=str(number) * 64,
            mime_type="text/plain",
            status="indexed",
        )
        db.add(doc)
        db.commit()
        ids.append(coll.id)
        docs[coll.id] = doc.id
    batches, calls, results = {}, [], []

    def hit(coll, index, content):
        return SimpleNamespace(
            chunk=SimpleNamespace(
                id=coll * 1000 + index,
                document_id=docs[coll],
                chunk_key=f"line:{index}",
                content=content,
                metadata={
                    "source_filename": f"policy-{coll}.txt",
                    "line_start": index,
                    "line_end": index,
                    "parser": "text",
                    "strategy": "recursive",
                },
            ),
            score=0.9,
        )

    class Store:
        def __init__(self, pool, *, collection_id):
            self.collection_id = collection_id

        async def bounded_fulltext_search(self, *, document_ids):
            calls.append((self.collection_id, "fulltext"))
            return [
                hit(self.collection_id, i, content)
                for i, content in enumerate(
                    batches.get((self.collection_id, "fulltext"), []), 1
                )
            ]

        async def keyword_search(self, query, top_k):
            calls.append((self.collection_id, "hybrid"))
            return [
                hit(self.collection_id, i, content)
                for i, content in enumerate(
                    batches.get(
                        (self.collection_id, "hybrid"), ["complete relevant passage"]
                    ),
                    1,
                )
            ][:top_k]

        async def keyword_fallback_search(self, query, top_k):
            return await self.keyword_search(query, top_k)

    monkeypatch.setattr(search, "get_pool", lambda: object())
    monkeypatch.setattr(search, "CollectionScopedPgVectorStore", Store)

    async def rag(collection_ids, query, top_k, *, mode="hybrid"):
        result = await retrieve_collections(
            db, user, collection_ids, query, top_k, mode=mode
        )
        assert _size(result) <= MAX_CONTEXT_BYTES
        results.append((mode, deepcopy(result)))
        return result

    return SimpleNamespace(
        user=user,
        outsider=outsider,
        ids=ids,
        batches=batches,
        calls=calls,
        results=results,
        rag=rag,
    )


def execute(graph, model, rag, *, message="short question", history=None):
    return asyncio.run(
        flow.execute_graph(
            graph,
            message,
            history=history,
            model_call=model,
            rag_call=rag,
            tool_call=None,
            document_call=None,
        )
    )


def assert_fallback(rows):
    assert rows
    for row in rows:
        assert row["metadata"]["requested_mode"] == "fulltext"
        assert row["metadata"]["retrieval_mode"] == "hybrid"
        assert row["metadata"]["fulltext_fallback"] is True
        assert row["metadata"]["fallback_reason"] == "fulltext_model_context_limit"


def test_two_legal_fulltext_calls_fallback_before_final_model(corpus):
    a, b = corpus.ids[:2]
    corpus.batches.update(
        {(a, "fulltext"): ["A" * 70000], (b, "fulltext"): ["B" * 70000]}
    )
    seen = []

    async def model(messages, temperature):
        seen.append(messages)
        flow._json_data(
            [[messages, temperature], {}],
            "callback_arguments",
            maximum=flow.MAX_GRAPH_BYTES,
        )
        return "answer"

    result = execute(
        chain(
            node("a", "rag", collection_ids=[a], mode="fulltext"),
            node("b", "rag", collection_ids=[b], mode="fulltext"),
            node("model", "model"),
        ),
        model,
        corpus.rag,
    )
    assert result["status"] == "completed"
    assert len(seen) == 1
    assert corpus.calls[:2] == [(a, "fulltext"), (b, "fulltext")]
    assert {ident for ident, mode in corpus.calls if mode == "hybrid"} == {a, b}
    assert all(
        len(rows[0]["content"]) == 70000
        for mode, rows in corpus.results
        if mode == "fulltext"
    )
    for entry in json.loads(seen[0][-1]["content"])["external_data"]:
        assert_fallback(entry["retrieval"])
        assert entry["retrieval"][0]["content"] == "complete relevant passage"
    for step in result["steps"]:
        if step["kind"] == "rag":
            assert_fallback(step["retrieval"])
            assert all(len(row["content"]) < 70000 for row in step["output"]["sources"])


def test_long_original_question_shortened_to_legal_search_query(corpus):
    a = corpus.ids[0]
    corpus.batches[(a, "fulltext")] = ["A" * 1500 for _ in range(60)]
    seen = []

    async def model(messages, temperature):
        seen.append(messages)
        return "q" * 4000 if len(seen) == 1 else "answer"

    result = execute(
        chain(
            node("shorten", "model"),
            node("rag", "rag", collection_ids=[a], mode="fulltext"),
            node("answer", "model"),
        ),
        model,
        corpus.rag,
        message="Q" * 16000,
    )
    assert result["status"] == "completed"
    assert len(corpus.results[0][1]) == 60
    assert_fallback(
        json.loads(seen[-1][-1]["content"])["external_data"][0]["retrieval"]
    )


def test_late_prompts_and_history_count_in_exact_callback_envelope(corpus):
    a = corpus.ids[0]
    corpus.batches[(a, "fulltext")] = ["A" * 100000]
    seen = []

    async def model(messages, temperature):
        seen.append(messages)
        flow._json_data(
            [[messages, temperature], {}],
            "callback_arguments",
            maximum=flow.MAX_GRAPH_BYTES,
        )
        return "answer"

    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "史" * 8000}
        for i in range(4)
    ]
    prompts = [node(f"prompt{i}", "prompt", instruction="P" * 32000) for i in range(11)]
    result = execute(
        chain(
            node("rag", "rag", collection_ids=[a], mode="fulltext"),
            *prompts,
            node("model", "model"),
        ),
        model,
        corpus.rag,
        history=history,
    )
    assert result["status"] == "completed"
    assert seen[0][1:5] == history
    assert len([row for row in seen[0] if row["role"] == "assistant"]) == 13
    assert_fallback(json.loads(seen[0][-1]["content"])["external_data"][0]["retrieval"])


def test_context_fallback_removes_whole_ranked_chunks(corpus):
    a, b = corpus.ids[:2]
    for ident in (a, b):
        corpus.batches[(ident, "fulltext")] = ["F" * 70000]
        corpus.batches[(ident, "hybrid")] = [letter * 25000 for letter in "abcd"]
    seen = []

    async def model(messages, temperature):
        seen.append(messages)
        return "answer"

    result = execute(
        chain(
            node("a", "rag", collection_ids=[a], mode="fulltext", top_k=4),
            node("b", "rag", collection_ids=[b], mode="fulltext", top_k=4),
            node("model", "model"),
        ),
        model,
        corpus.rag,
    )
    assert result["status"] == "completed"
    rows = [
        row
        for entry in json.loads(seen[0][-1]["content"])["external_data"]
        for row in entry["retrieval"]
    ]
    assert 2 <= len(rows) < 8
    assert all(len(row["content"]) == 25000 for row in rows)
    assert all(row["metadata"]["context_limited"] for row in rows)
    assert_fallback(rows)


@pytest.mark.parametrize("empty", [False, True])
def test_no_fitting_complete_candidate_fails_without_final_model(corpus, empty):
    a, b = corpus.ids[:2]
    for ident in (a, b):
        corpus.batches[(ident, "fulltext")] = ["F" * 70000]
        corpus.batches[(ident, "hybrid")] = [] if empty else ["H" * 120000]
    seen = []

    async def model(*args):
        seen.append(args)
        return "q" * 4000 if len(seen) == 1 else "must not run"

    result = execute(
        chain(
            node("shorten", "model"),
            node("a", "rag", collection_ids=[a], mode="fulltext"),
            node("b", "rag", collection_ids=[b], mode="fulltext"),
            node("model", "model"),
        ),
        model,
        corpus.rag,
        message="Q" * 16000,
    )
    assert result["status"] == "failed"
    assert result["error"]["type"] == "RAG_CONTEXT_LIMIT"
    assert len(seen) == 1


def test_can_omit_one_whole_source_when_other_source_fits(corpus):
    a, b = corpus.ids[:2]
    for ident in (a, b):
        corpus.batches[(ident, "fulltext")] = ["F" * 70000]
        corpus.batches[(ident, "hybrid")] = ["H" * 70000]
    seen = []

    async def model(messages, temperature):
        seen.append(messages)
        return "answer"

    result = execute(
        chain(
            node("a", "rag", collection_ids=[a], mode="fulltext"),
            node("b", "rag", collection_ids=[b], mode="fulltext"),
            node("model", "model"),
        ),
        model,
        corpus.rag,
    )
    assert result["status"] == "completed"
    rows = [
        row
        for entry in json.loads(seen[0][-1]["content"])["external_data"]
        for row in entry["retrieval"]
    ]
    assert len(rows) == 1
    assert len(rows[0]["content"]) == 70000
    assert_fallback(rows)
    traces = [step for step in result["steps"] if step["kind"] == "rag"]
    assert all(
        step["retrieval_context"]["fallback_reason"] == "fulltext_model_context_limit"
        for step in traces
    )


def test_fallback_rechecks_live_collection_acl(db, corpus):
    a, b = corpus.ids[:2]
    for ident in (a, b):
        corpus.batches[(ident, "fulltext")] = ["F" * 70000]
    count, seen = 0, []

    async def rag(ids, query, top_k, **kwargs):
        nonlocal count
        rows = await corpus.rag(ids, query, top_k, **kwargs)
        count += 1
        if count == 2:
            db.get(IngestionCollection, a).created_by = corpus.outsider.id
            db.commit()
        return rows

    async def model(*args):
        seen.append(args)
        return "must not run"

    result = execute(
        chain(
            node("a", "rag", collection_ids=[a], mode="fulltext"),
            node("b", "rag", collection_ids=[b], mode="fulltext"),
            node("model", "model"),
        ),
        model,
        rag,
    )
    assert result["status"] == "failed"
    assert seen == []
    assert corpus.calls == [(a, "fulltext"), (b, "fulltext")]


def test_equal_fulltext_on_two_branches_keeps_each_original_query(corpus):
    a = corpus.ids[0]
    corpus.batches[(a, "fulltext")] = ["A" * 70000]
    queries, final = [], []

    async def rag(ids, query, top_k, **kwargs):
        result = await corpus.rag(ids, query, top_k, **kwargs)
        if not kwargs:
            queries.append(query)
            result[0]["content"] = "complete passage for " + query
        return result

    async def model(messages, temperature):
        if len(final) < 2:
            response = "query-left" if not final else "query-right"
            final.append(messages)
            return response
        final.append(messages)
        return "answer"

    nodes = [
        node("input", "input"),
        node("left", "model"),
        node("right", "model"),
        node("ra", "rag", collection_ids=[a], mode="fulltext"),
        node("rb", "rag", collection_ids=[a], mode="fulltext"),
        node("join", "model"),
        node("output", "output", include_sources=False),
    ]
    edges = [
        ("input", "left"),
        ("input", "right"),
        ("left", "ra"),
        ("right", "rb"),
        ("ra", "join"),
        ("rb", "join"),
        ("join", "output"),
    ]
    result = execute(
        {"nodes": nodes, "edges": [{"source": a, "target": b} for a, b in edges]},
        model,
        rag,
    )
    assert result["status"] == "completed"
    assert queries == ["query-left", "query-right"]
    rows = json.loads(final[-1][-1]["content"])["external_data"]
    assert [entry["retrieval"][0]["content"] for entry in rows] == [
        "complete passage for query-left",
        "complete passage for query-right",
    ]
    traces = {step["node_id"]: step for step in result["steps"]}
    assert traces["ra"]["retrieval"][0]["content"].endswith("query-left")
    assert traces["rb"]["retrieval"][0]["content"].endswith("query-right")
    assert len(traces["ra"]["output"]["sources"]) == 1
    assert len(traces["rb"]["output"]["sources"]) == 1


@pytest.mark.parametrize("kind", ["format", "extract", "agent"])
def test_shared_preflight_covers_other_model_modules(corpus, kind):
    a, b = corpus.ids[:2]
    for ident in (a, b):
        corpus.batches[(ident, "fulltext")] = ["A" * 70000]
    seen = []

    async def model(messages, temperature):
        seen.append(messages)
        return {
            "format": "formatted",
            "extract": '{"field": "value"}',
            "agent": '{"action":"final","output":"answer"}',
        }[kind]

    config = {"extract": {"fields": ["field"]}, "agent": {"tool_ids": [1]}}.get(
        kind, {}
    )
    result = execute(
        chain(
            node("a", "rag", collection_ids=[a], mode="fulltext"),
            node("b", "rag", collection_ids=[b], mode="fulltext"),
            node("model", kind, **config),
        ),
        model,
        corpus.rag,
    )
    assert result["status"] == "completed"
    assert len(seen) == 1
    for entry in json.loads(seen[0][-1]["content"])["external_data"]:
        assert_fallback(entry["retrieval"])


def test_small_fulltext_stays_complete_without_extra_search(corpus):
    a = corpus.ids[0]
    corpus.batches[(a, "fulltext")] = ["A" * 1500 for _ in range(30)]
    seen = []

    async def model(messages, temperature):
        seen.append(messages)
        return "answer"

    result = execute(
        chain(
            node("rag", "rag", collection_ids=[a], mode="fulltext", top_k=1),
            node("model", "model"),
        ),
        model,
        corpus.rag,
    )
    assert result["status"] == "completed"
    rows = json.loads(seen[0][-1]["content"])["external_data"][0]["retrieval"]
    assert len(rows) == 30
    assert all(row["metadata"]["retrieval_mode"] == "fulltext" for row in rows)
    assert corpus.calls == [(a, "fulltext")]
