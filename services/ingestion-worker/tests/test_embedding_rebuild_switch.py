"""重建有缺向量時不得把搜尋切到新模型。"""
from __future__ import annotations

from pathlib import Path

from anila_core.storage.embeddable import (
    fact_predicate,
    leaf_predicate,
    summary_predicate,
)
from ingestion_worker.embedding_rebuild import rebuild_ready_to_switch

_REBUILD = Path(__file__).resolve().parents[1] / "src" / "ingestion_worker" / "embedding_rebuild.py"
_STORE = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "anila-core"
    / "src"
    / "anila_core"
    / "storage"
    / "adapters"
    / "pgvector_store.py"
)


def test_switch_only_when_every_embeddable_row_has_the_target_vector():
    assert rebuild_ready_to_switch(done=3, total=3) is True
    assert rebuild_ready_to_switch(done=0, total=0) is True
    assert rebuild_ready_to_switch(done=2, total=4) is False
    assert rebuild_ready_to_switch(done=0, total=4) is False


def test_total_done_and_batch_share_one_embeddable_predicate():
    """空白列與三次失敗列用同一段 SQL 排除，done 不能靠空白向量墊過 total。"""
    rebuild = _REBUILD.read_text(encoding="utf-8")
    store = _STORE.read_text(encoding="utf-8")
    assert "leaf_predicate" in store
    assert "fact_predicate" in rebuild
    assert "summary_predicate" in rebuild
    leaf = leaf_predicate("c")
    assert "chunk_type = 'leaf'" in leaf
    assert "length(btrim(c.content)) > 0" in leaf
    assert "attempts >= 3" in leaf
    assert "attempts >= 3" in fact_predicate("user_facts")
    assert "length(btrim(s.summary)) > 0" in summary_predicate("s")
    assert rebuild.count("fact_predicate") >= 2
    assert rebuild.count("summary_predicate") >= 2
    assert store.count("leaf_predicate") >= 2
