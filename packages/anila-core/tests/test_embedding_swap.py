"""Embeddings survive a model change of any dimension, and an outage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from anila_core.embeddings.dims import HALFVEC_HNSW_MAX, ann_index_sql, fit_stored_vector
from anila_core.embeddings.keyword import (
    KEYWORD_FALLBACK_NOTICE,
    cjk_search_units,
    order_by_indexes,
    parse_rerank_indexes,
    search_path,
)
from anila_core.embeddings.swap import (
    OLD_VECTOR_RETENTION_DAYS,
    ActivationState,
    InMemoryCorpus,
    WorkItem,
    cleanup_model_id,
    eta_seconds,
    plan_designation,
    plan_write_targets,
    rollback_state,
    run_rebuild_pass,
)


NOW = datetime(2026, 9, 28, 4, 0, tzinfo=timezone.utc)


def test_native_width_is_stored_until_the_hnsw_ceiling():
    short = fit_stored_vector([0.2] * 768, declared_native=768)
    assert short.dims == 768
    assert short.native_dims == 768
    assert short.truncated is False

    wide = fit_stored_vector([float(i) for i in range(4096)], declared_native=4096)
    assert wide.dims == HALFVEC_HNSW_MAX
    assert wide.native_dims == 4096
    assert wide.values == [float(i) for i in range(HALFVEC_HNSW_MAX)]


def test_fit_refuses_a_width_the_model_did_not_declare():
    with pytest.raises(ValueError, match="1536"):
        fit_stored_vector([0.1] * 1536, declared_native=768)


def test_ann_index_is_partial_on_model_and_width():
    sql = ann_index_sql(model_id=12, dims=768, subject="chunk")
    assert "embedding::halfvec(768)" in sql
    assert "WHERE model_id = 12 AND subject = 'chunk' AND dims = 768" in sql
    assert "CREATE INDEX IF NOT EXISTS ix_embvec_hnsw_chunk_12_768" in sql


def test_ann_index_rejects_widths_hnsw_cannot_hold():
    with pytest.raises(ValueError):
        ann_index_sql(model_id=1, dims=4001, subject="chunk")


def test_designation_does_not_switch_search():
    state = plan_designation(
        ActivationState(active_model_id=1),
        target_model_id=2,
        keep_model_id=1,
        now=NOW,
    )
    assert state.active_model_id == 1
    assert state.rebuild_target_model_id == 2
    assert state.rebuild_status == "pending"


def test_first_designation_with_nothing_embedded_is_active_immediately():
    state = plan_designation(
        ActivationState(),
        target_model_id=5,
        keep_model_id=None,
        now=NOW,
    )
    assert state.active_model_id == 5
    assert state.rebuild_status is None


def test_two_widths_coexist_and_search_uses_only_the_active_model():
    corpus = InMemoryCorpus(
        items=[
            WorkItem("chunk", 1, "甲"),
            WorkItem("chunk", 2, "乙"),
        ],
        state=ActivationState(
            active_model_id=1,
            rebuild_target_model_id=2,
            rebuild_status="running",
            rebuild_started_at=NOW,
        ),
    )
    # Old model already embedded these rows at 4 dims.
    corpus.write(1, corpus.items, [[0.1] * 4, [0.2] * 4])

    def embed(texts: list[str]) -> list[list[float]]:
        return [[0.5] * 8 for _ in texts]

    done = run_rebuild_pass(corpus, embed, max_batches=5, batch_size=10, now=NOW)
    assert done.active_model_id == 2
    assert done.previous_model_id == 1
    assert done.rebuild_status == "complete"
    assert corpus.search(2) == [1, 2]
    assert corpus.search(1) == [1, 2]
    assert len(corpus.vectors[("chunk", 1, 1)]) == 4
    assert len(corpus.vectors[("chunk", 1, 2)]) == 8
    # A reader that asks for the active model does not see the other width.
    assert corpus.search(done.active_model_id) == [1, 2]
    assert all(len(corpus.vectors[("chunk", sid, 2)]) == 8 for sid in corpus.search(2))


def test_rebuild_resumes_after_interruption_then_switches_once():
    items = [WorkItem("chunk", i, f"段{i}") for i in range(5)]
    corpus = InMemoryCorpus(
        items=items,
        state=ActivationState(
            active_model_id=7,
            rebuild_target_model_id=8,
            rebuild_status="pending",
        ),
    )
    calls = {"n": 0}

    def embed(texts: list[str]) -> list[list[float]]:
        calls["n"] += 1
        return [[1.0, 0.0] for _ in texts]

    mid = run_rebuild_pass(corpus, embed, max_batches=1, batch_size=2, now=NOW)
    assert mid.rebuild_status == "running"
    assert mid.active_model_id == 7
    assert mid.rebuild_done == 2

    switched_at = NOW + timedelta(minutes=5)
    done = run_rebuild_pass(
        corpus, embed, max_batches=10, batch_size=2, now=switched_at
    )
    assert done.active_model_id == 8
    assert done.previous_model_id == 7
    assert done.switched_at == switched_at
    assert done.rebuild_target_model_id is None
    assert corpus.counts(8) == (5, 5, 0)
    # The second pass did not re-embed the rows already written.
    assert calls["n"] == 3


def test_rollback_cancels_a_running_rebuild():
    state = rollback_state(
        ActivationState(
            active_model_id=1,
            rebuild_target_model_id=2,
            rebuild_status="running",
        ),
        now=NOW,
        previous_vectors_exist=True,
    )
    assert state.active_model_id == 1
    assert state.rebuild_status == "cancelled"
    assert state.rebuild_target_model_id is None


def test_rollback_restores_the_previous_model_while_its_vectors_exist():
    state = rollback_state(
        ActivationState(
            active_model_id=2,
            previous_model_id=1,
            switched_at=NOW,
            rebuild_status="complete",
        ),
        now=NOW + timedelta(days=1),
        previous_vectors_exist=True,
    )
    assert state.active_model_id == 1
    assert state.previous_model_id == 2


def test_rollback_refuses_once_the_previous_vectors_are_gone():
    with pytest.raises(ValueError):
        rollback_state(
            ActivationState(
                active_model_id=2,
                previous_model_id=1,
                switched_at=NOW,
                rebuild_status="complete",
            ),
            now=NOW,
            previous_vectors_exist=False,
        )


def test_previous_vectors_are_deleted_seven_days_after_the_switch():
    state = ActivationState(
        active_model_id=2,
        previous_model_id=1,
        switched_at=NOW,
        rebuild_status="complete",
    )
    assert cleanup_model_id(state, now=NOW + timedelta(days=6, hours=23)) is None
    assert (
        cleanup_model_id(
            state, now=NOW + timedelta(days=OLD_VECTOR_RETENTION_DAYS)
        )
        == 1
    )
    # A rebuild that might still return to the old model freezes cleanup.
    running = ActivationState(
        active_model_id=2,
        previous_model_id=1,
        switched_at=NOW - timedelta(days=30),
        rebuild_status="running",
        rebuild_target_model_id=3,
    )
    assert cleanup_model_id(running, now=NOW) is None


def test_ingest_during_rebuild_writes_both_models_and_the_column_stays_on_the_active_one():
    targets = plan_write_targets(
        role_id=2,
        role_name="new",
        role_native=8,
        active_id=1,
        active_name="old",
        active_native=4,
        rebuild_status="running",
    )
    assert [t.model_id for t in targets] == [1, 2]
    assert [t.for_column for t in targets] == [True, False]


def test_keyword_fallback_notice_and_path():
    assert "嵌入模型暫時無法使用" in KEYWORD_FALLBACK_NOTICE
    assert search_path(embed_ok=False, active_vectors_exist=True, corpus_exists=True) == "keyword"
    assert search_path(embed_ok=True, active_vectors_exist=False, corpus_exists=True) == "keyword"
    assert search_path(embed_ok=True, active_vectors_exist=True, corpus_exists=True) == "vector"
    assert search_path(embed_ok=False, active_vectors_exist=False, corpus_exists=False) == "vector"


def test_cjk_bigrams_split_traditional_chinese():
    units = cjk_search_units("差勤管理")
    assert "差勤" in units
    assert "勤管" in units
    assert "差勤管" in units


def test_rerank_order_is_applied_only_when_it_mentions_every_hit():
    assert parse_rerank_indexes("3, 1, 2", 3) == [2, 0, 1]
    assert parse_rerank_indexes("只要 1", 3) is None
    assert order_by_indexes(["a", "b", "c"], [2, 0, 1]) == ["c", "a", "b"]


def test_eta_uses_the_observed_rate():
    started = NOW
    # 10 done in 10 seconds → 90 left takes 90 seconds.
    assert eta_seconds(done=10, total=100, started_at=started, now=NOW + timedelta(seconds=10)) == 90
