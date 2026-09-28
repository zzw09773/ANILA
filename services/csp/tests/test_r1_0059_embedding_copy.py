"""r1_0059 的向量搬移：新鮮資料庫依每列來源模型，不改寫已套用的線上庫。"""
from __future__ import annotations

from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "r1_0059_dimension_agnostic_embeddings.py"
)


def test_fresh_copy_matches_each_rows_source_model():
    src = _MIGRATION.read_text(encoding="utf-8")
    chunk = src.split("SELECT 'chunk'", 1)[1].split("SELECT 'fact'", 1)[0]
    assert "lower(c.embedding_source_model)" in chunk
    assert "is_platform_embedding" not in chunk
    assert "已套用" in src
    versions = _MIGRATION.parent
    for path in versions.glob("r1_006*.py"):
        text = path.read_text(encoding="utf-8")
        assert "UPDATE embedding_vectors" not in text
