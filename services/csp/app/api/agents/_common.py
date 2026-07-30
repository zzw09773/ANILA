"""Shared helpers for the ``app.api.agents`` package.

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor). Only helpers used by 2+ submodules
live here.
"""
from fastapi import Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.user import User
from app.schemas.contracts.agents import AgentManifest
from app.schemas.contracts.classification import ClassificationLevel
from app.services.auth_service import get_current_user, is_admin_tier

# Rank CASE shared by the race-safe raise-only UPDATE (X.6). Must stay
# aligned with ClassificationLevel member order / ``_RANKS``.
_STORED_LEVEL_RANK_SQL = """
CASE default_classification_level
  WHEN '無機密' THEN 0
  WHEN '營業秘密' THEN 1
  WHEN '密' THEN 2
  WHEN '機密' THEN 3
  ELSE -1
END
""".strip()

# Effective policy rank = max(stored, 密) when legacy requires_encryption
# is set — same rule as ``effective_agent_policy_level``.
_EFFECTIVE_LEVEL_RANK_SQL = f"""
CASE
  WHEN requires_encryption AND ({_STORED_LEVEL_RANK_SQL}) < 2 THEN 2
  ELSE ({_STORED_LEVEL_RANK_SQL})
END
""".strip()


def _classification_write_barrier() -> None:
    """Test hook between the stale-row check and the conditional UPDATE.

    Default no-op. The PostgreSQL race harness replaces this with
    ``threading.Barrier.wait`` so both connections SELECT before either
    writes — a naive parallel-start harness is not enough to prove the fix.
    """
    return


def validate_agent_manifest(payload: dict) -> dict:
    """Validate a submitted Agent manifest against the doc 05 §4 contract.

    Fail-closed: unknown fields / wrong types / invalid enum values (e.g. a
    non-四級 classification) raise ``422`` with a zh-TW detail. Returns the
    normalized manifest dict (JSON-mode) suitable for ``Agent.manifest_json``.
    """
    try:
        manifest = AgentManifest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ())) or "(root)"
        msg = first.get("msg", "格式不符")
        raise HTTPException(
            status_code=422,
            detail=f"Agent manifest 驗證失敗:欄位「{loc}」{msg}",
        ) from exc
    return manifest.model_dump(mode="json")


def _require_developer_or_admin(current_user: User = Depends(get_current_user)) -> User:
    # Sprint owner-tier: owner 是 admin 之上的最高權限,凡 admin 能做的事 owner 也能做。
    # 漏掉 owner 會讓系統最高權限者反而拿不到開發者頁面 (download template、register
    # agent 等),這是 RBAC 新增層級時典型的回歸坑。和 auth_service._ADMIN_TIER_ROLES
    # 保持一致。
    if current_user.role not in ("admin", "developer", "owner"):
        raise HTTPException(status_code=403, detail="需要開發者或管理員權限")
    return current_user


def _client_ip(request: Request | None) -> str | None:
    """Extract caller IP from the Request — tolerant of reverse-proxy
    setups (reads X-Forwarded-For first hop) and of None so endpoints
    that don't inject a Request stay safe."""
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _resolve_agent(db: Session, agent_id: int) -> Agent:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent


def _require_agent_editor(agent: Agent, current_user: User) -> None:
    """Same gate as ``PUT /api/agents/{id}``: admin-tier or owner."""
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限編輯此 Agent")


def parse_stored_classification_level(raw: str | None) -> ClassificationLevel:
    """Parse a stored level; illegal DB values refuse with 422 (not 500)."""
    try:
        return ClassificationLevel.from_storage(raw or "無機密")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "預設分類等級無效，請改設為合法四級之一："
                "無機密、營業秘密、密、機密"
            ),
        ) from exc


def requires_controlled_access(level: ClassificationLevel) -> bool:
    """Derive the legacy ``requires_encryption`` boolean from a level.

    Uses the conversation mirror threshold (``level >= 密`` / RESTRICTED;
    SYSTEM-MAP §8 / ``_mirror_legacy_boolean``) so boolean readers stay
    aligned with a single source of truth — the default classification level.
    """
    return level >= ClassificationLevel.RESTRICTED


def effective_agent_policy_level(agent: Agent) -> ClassificationLevel:
    """Effective policy level — same rule as ``proxy._agent_policy_level``.

    Stored ``default_classification_level``, floored at RESTRICTED(密) when
    the legacy ``requires_encryption`` flag is set. Writers that compare
    "current vs requested" must use this, not the raw stored column alone.
    """
    level = parse_stored_classification_level(
        getattr(agent, "default_classification_level", None)
    )
    if bool(getattr(agent, "requires_encryption", False)):
        level = ClassificationLevel.max_of(
            [level, ClassificationLevel.RESTRICTED]
        )
    return level


def refuse_classification_downgrade(
    agent: Agent, new_level: ClassificationLevel, current_user: User
) -> None:
    """Block non-admin writes that would lower the effective policy level.

    Developers may raise (or re-save at the same effective level); only
    administrator-tier callers may lower. Matches the one-way classification
    convention elsewhere — sanctioned lowering is supervisor-approved.

    This is the fast UX check against the session-local row. Non-admin
    persists still go through :func:`apply_default_classification_level`'s
    conditional UPDATE so a concurrent raise cannot be overwritten.
    """
    current = effective_agent_policy_level(agent)
    if new_level < current and not is_admin_tier(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                f"開發者不得降低 agent 的有效分類等級"
                f"（目前有效等級為「{current.to_storage()}」）；"
                f"降級需管理員處理"
            ),
        )


def refuse_bind_above_agent_level(
    db: Session, agent: Agent, collection_ids: list[int]
) -> None:
    """P4.9 — refuse binding any collection above the agent's effective level.

    Caller passes the DELTA (newly added ids) on update, or the full set on
    register — pre-existing over-level bindings are not re-checked so an
    unrelated edit cannot brick the console. Does **not** silently raise the
    agent; the message names the offending collection and both levels and
    points the registrant at the self-service raise path.
    """
    if not collection_ids:
        return
    from app.models.ingestion import IngestionCollection

    agent_level = effective_agent_policy_level(agent)
    rows = (
        db.query(IngestionCollection)
        .filter(IngestionCollection.id.in_(collection_ids))
        .all()
    )
    by_id = {int(c.id): c for c in rows}
    violators: list[tuple[object, ClassificationLevel]] = []
    for cid in collection_ids:
        col = by_id.get(int(cid))
        if col is None:
            # Access validation owns the 404/403 for missing ids; skip here.
            continue
        try:
            col_level = ClassificationLevel.from_storage(
                getattr(col, "classification_level", None) or "無機密"
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"知識庫「{col.name}」的分類等級無效，無法綁定；"
                    "請先修正該知識庫的分類等級"
                ),
            ) from exc
        if agent_level < col_level:
            violators.append((col, col_level))
    if not violators:
        return
    # Name the highest offender so multi-bind failures cite the maximum.
    col, col_level = max(violators, key=lambda pair: pair[1])
    raise HTTPException(
        status_code=403,
        detail=(
            f"無法綁定知識庫「{col.name}」（分類等級「{col_level.to_storage()}」）："
            f"agent 目前有效等級為「{agent_level.to_storage()}」，"
            f"須 ≥ 知識庫等級。"
            f"請先自行將本 agent 的預設分類等級提升至"
            f"「{col_level.to_storage()}」以上後再綁定"
            f"（擁有者／註冊者可直接調整，無需管理員）。"
        ),
    )


def apply_default_classification_level(
    agent: Agent,
    level: ClassificationLevel,
    *,
    db: Session | None = None,
    allow_downgrade: bool = False,
) -> tuple[
    tuple[ClassificationLevel, ClassificationLevel] | None,
    bool,
    tuple[ClassificationLevel, ClassificationLevel],
]:
    """Write ``default_classification_level`` and derive ``requires_encryption``.

    Returns ``(effective_transition, changed, stored_transition)``:
    - ``effective_transition`` is ``(from_effective, to_effective)`` when the
      effective policy level changed (caller must audit); ``None`` when
      effective is unchanged — including a boolean-only repair that keeps
      the same floor, or a pure no-op.
    - ``changed`` is True when either the stored level or the derived
      boolean was written — callers must commit in that case even if audit
      is skipped.
    - ``stored_transition`` is always ``(from_stored, to_stored)`` for the
      requested write (useful as audit metadata alongside the effective
      transition).

    When ``db`` is supplied and ``allow_downgrade`` is False (non-admin
    path), the write is a single conditional UPDATE that only succeeds
    when the committed effective rank is ≤ the requested level — no row
    lock, and a concurrent raise cannot be overwritten into a downgrade.
    Admin explicit downgrades pass ``allow_downgrade=True`` and use the
    plain ORM assignment.
    """
    previous_stored = parse_stored_classification_level(
        getattr(agent, "default_classification_level", None)
    )
    previous_effective = effective_agent_policy_level(agent)
    stored_transition = (previous_stored, level)
    new_bool = requires_controlled_access(level)
    level_changed = previous_stored != level
    bool_changed = bool(getattr(agent, "requires_encryption", False)) != new_bool
    if not level_changed and not bool_changed:
        return None, False, stored_transition

    if db is not None and not allow_downgrade:
        _classification_write_barrier()
        result = db.execute(
            text(
                f"""
                UPDATE agents
                SET default_classification_level = :lvl,
                    requires_encryption = :req
                WHERE id = :id
                  AND ({_EFFECTIVE_LEVEL_RANK_SQL}) <= :new_rank
                """
            ),
            {
                "lvl": level.to_storage(),
                "req": new_bool,
                "id": agent.id,
                "new_rank": level.rank,
            },
        )
        if result.rowcount == 0:
            # Concurrent raise (or stale read): do not persist a downgrade.
            db.expire(agent)
            db.refresh(agent)
            current = effective_agent_policy_level(agent)
            if level < current:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"開發者不得降低 agent 的有效分類等級"
                        f"（目前有效等級為「{current.to_storage()}」）；"
                        f"降級需管理員處理"
                    ),
                )
            # Already at/above the requested effective level with matching
            # stored columns — treat as no-op after the lost race.
            new_stored = parse_stored_classification_level(
                getattr(agent, "default_classification_level", None)
            )
            return None, False, (new_stored, level)
        # Keep the identity-map row aligned with what we just wrote.
        agent.default_classification_level = level.to_storage()
        agent.requires_encryption = new_bool
    else:
        agent.default_classification_level = level.to_storage()
        agent.requires_encryption = new_bool

    new_effective = effective_agent_policy_level(agent)
    if previous_effective != new_effective:
        return (previous_effective, new_effective), True, stored_transition
    return None, True, stored_transition
