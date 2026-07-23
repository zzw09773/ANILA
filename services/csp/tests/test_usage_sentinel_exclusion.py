"""anila-router 哨兵的計量不得出現在用量統計。

寫入端已用 ``suppress_usage_accounting`` 抑制;統計層(``_apply_usage_filters``)
再擋一層,讓部署空窗期或歷史殘留的哨兵列也不會滲進圖表/報表,
且 NULL model_id 的列(agent 類用量)不得被 NOT IN 的 NULL 語意誤殺。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.models.token_usage import TokenUsage
from app.services.usage_service import (
    get_top_models,
    get_usage_summary,
)

from .conftest import make_model, make_user


def _seed(db):
    user = make_user(db, username="usage_sentinel_user")
    real = make_model(db, name="gpt-oss-20b")
    sentinel = make_model(db, name="anila-router")
    now = datetime.now(timezone.utc)
    rows = [
        TokenUsage(
            user_id=user.id, model_id=real.id,
            prompt_tokens=10, completion_tokens=20, total_tokens=30,
            request_timestamp=now,
        ),
        TokenUsage(
            user_id=user.id, model_id=sentinel.id,
            prompt_tokens=100, completion_tokens=200, total_tokens=300,
            request_timestamp=now,
        ),
        # NULL model_id(agent 類)必須存活於統計。
        TokenUsage(
            user_id=user.id, model_id=None,
            prompt_tokens=1, completion_tokens=1, total_tokens=2,
            request_timestamp=now,
        ),
    ]
    db.add_all(rows)
    db.commit()
    return user, real, sentinel


def test_top_models_excludes_sentinel(db):
    _seed(db)
    names = [m["model_name"] if isinstance(m, dict) else m.model_name
             for m in get_top_models(db, limit=10)]
    joined = " ".join(str(n) for n in names)
    assert "anila-router" not in joined
    assert "gpt-oss-20b" in joined


def test_summary_excludes_sentinel_but_keeps_null_model_rows(db):
    _seed(db)
    summary = get_usage_summary(db, range_key="24h")
    total = summary["total_tokens"] if isinstance(summary, dict) else summary.total_tokens
    # 30(真模型) + 2(NULL model_id) — 哨兵的 300 不得計入。
    assert int(total) == 32


# get_chart_data 用 PG 專屬 SQL(date_trunc),SQLite 測試庫跑不了;它與
# 上面兩個測試走同一個 _apply_usage_filters 漏斗,排除行為由前兩者釘住。
