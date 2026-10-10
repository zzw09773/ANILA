"""升密時刪掉從該對話萃取出來的記憶。

政策模組只需要這一個動作。若改從 ``memory_service`` import，會順著
proxy → task_link 碰到 ``app.modules.tasks``，打破 modules 互不 import。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.user_memory import (
    ConversationMemoryChunk,
    ConversationSummary,
    UserFact,
)


def purge_conversation_memory(db: Session, conversation_id: int) -> dict[str, int]:
    """Delete every piece of memory derived from ``conversation_id``.

    Owner rule (擁有者裁定, 2026-07-30):
    「對話**升密之後,先前萃取的記憶直接刪除**(不是標記不可用)。」
    Real DELETE, not a tombstone or a retrieval-time filter — a filter
    is one forgotten call site away from serving the content again, and
    the ruling says 撤回, not 隱藏.

    Both stores the memory subsystem writes are covered:

    * ``conversation_memory_chunks`` — RAG recall rows. The embedding is
      a column on this table, not a separate store, so the vector dies
      with the row; there is no orphaned index entry to sweep.
    * ``user_facts`` — extracted key/value facts, matched on
      ``source_conversation_id`` (the provenance column, so an upgrade
      can find them again).

    Caller owns the transaction — this stages the DELETEs and does not
    commit, so the purge lands in the same transaction as the
    classification upgrade that triggered it. Half-applied is not a
    state this may end in.

    Returns the per-store row counts for the audit trail.
    """
    chunks = (
        db.query(ConversationMemoryChunk)
        .filter(ConversationMemoryChunk.conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )
    facts = (
        db.query(UserFact)
        .filter(UserFact.source_conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )
    summaries = (
        db.query(ConversationSummary)
        .filter(ConversationSummary.conversation_id == conversation_id)
        .delete(synchronize_session=False)
    )
    return {"chunks": int(chunks), "facts": int(facts), "summaries": int(summaries)}
