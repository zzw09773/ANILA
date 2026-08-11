"""Focused consumer tests for the twelve immediate settings."""

from __future__ import annotations

from app.models.platform_setting import PlatformSetting, get_setting, set_setting
from app.services import attachment_context, department_tree


def test_department_depth_reads_the_db_value_each_call(db):
    assert department_tree.max_depth(db) == 3
    set_setting(db, "limits.department_max_depth", 7)
    db.commit()
    assert department_tree.max_depth(db) == 7


def test_attachment_budget_ratio_changes_admission_budget(db):
    assert attachment_context.attachment_budget_tokens(db, 1000) == 700
    set_setting(db, "limits.attachment_budget_ratio", 0.25)
    db.commit()
    assert attachment_context.attachment_budget_tokens(db, 1000) == 250


def test_memory_retrieval_knobs_are_db_backed(db):
    set_setting(db, "memory.retrieve_top_k", 9)
    set_setting(db, "memory.retrieve_min_cosine", 0.81)
    db.commit()
    assert get_setting(db, "memory.retrieve_top_k") == 9
    assert get_setting(db, "memory.retrieve_min_cosine") == 0.81


def test_all_retained_keys_can_round_trip(db):
    values = {
        "institutional_kb.score_threshold": 0.55,
        "memory.retrieve_min_cosine": 0.65,
        "memory.retrieve_top_k": 8,
        "proxy.llm_timeout": 81,
        "proxy.embedding_timeout": 82,
        "auth.access_token_expire_minutes": 83,
        "auth.refresh_token_expire_days": 84,
        "limits.department_max_depth": 4,
        "limits.action_invoke_per_min": 85,
        "limits.attachment_budget_ratio": 0.86,
        "intl.zh_normalize": False,
        "intl.query_expansion": False,
    }
    for key, value in values.items():
        set_setting(db, key, value)
    db.commit()
    assert {key: get_setting(db, key) for key in values} == values
