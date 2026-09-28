"""可嵌入列的同一套條件。

計數、完成判斷、批次挑選都用這些片段。空白內容不算。
連續失敗三次的列從 total 排除，改由治理中心顯示「無法嵌入的 N 筆」，
並可從那裡重試。切換仍要求其餘每一筆可嵌入資料都有目標模型的向量。
"""
from __future__ import annotations

PERMANENT_FAILURES = 3


def _failure_sql(subject: str, id_expr: str, model_param: str) -> str:
    return f"""NOT EXISTS (
    SELECT 1 FROM embedding_rebuild_failures fail
     WHERE fail.subject = '{subject}'
       AND fail.subject_id = {id_expr}
       AND fail.model_id = {model_param}
       AND fail.attempts >= {PERMANENT_FAILURES}
)"""


def leaf_predicate(chunk_alias: str = "c", model_param: str = "$1") -> str:
    """非空 leaf，且這次目標模型還沒把這一列判成無法嵌入。"""
    return f"""{chunk_alias}.chunk_type = 'leaf'
AND {chunk_alias}.content IS NOT NULL
AND length(btrim({chunk_alias}.content)) > 0
AND {_failure_sql("chunk", f"{chunk_alias}.id", model_param)}"""


def fact_predicate(alias: str = "user_facts", model_param: str = "$1") -> str:
    """非偏好、有文字，且尚未連續失敗三次。"""
    return f"""COALESCE({alias}.kind, 'fact') <> 'preference'
AND COALESCE({alias}.key, '') NOT LIKE 'preference.%'
AND (
    length(btrim(COALESCE({alias}.key, ''))) > 0
    OR length(btrim(COALESCE({alias}.value, ''))) > 0
)
AND {_failure_sql("fact", f"{alias}.id", model_param)}"""


def summary_predicate(alias: str = "s", model_param: str = "$1") -> str:
    """摘要有文字，且尚未連續失敗三次。"""
    return f"""{alias}.summary IS NOT NULL
AND length(btrim({alias}.summary)) > 0
AND {_failure_sql("summary", f"{alias}.id", model_param)}"""
