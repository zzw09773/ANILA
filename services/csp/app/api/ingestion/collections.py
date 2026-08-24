"""Ingestion collections CRUD (`/api/ingestion/collections`).

Sprint 4 refactor: collections are first-class user-owned resources.
Sprint 1–3 scoped them to ``agent_id``; that coupling was over-design
for the platform's "infra not multi-tenant SaaS" posture. CSP UI no
longer asks for an agent. Any agent backend points at a collection
via its own deploy config (``RAG_COLLECTION_ID`` env).

Authorisation:
- ``admin`` users: list / manage every collection (cross-org admin).
- non-admin: list / manage only collections they own (``created_by``).
  Sharing-with-other-users is a Sprint-5 ``collection_access_grants``
  concern; not in scope here.

Mutations write an ``audit_log`` row so misuse / accidental delete is
traceable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import NoReturn, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.agents._common import effective_agent_policy_level
from app.database import get_db
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.modules.policy import apply_classification
from app.schemas.contracts.classification import ClassificationLevel
from app.schemas.ingestion import (
    CollectionClassificationRaise,
    CollectionCreate,
    CollectionResponse,
    CollectionUpdate,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier
from app.services.ingestion_classification import (
    agents_bound_below_level,
    cascade_raise_documents,
    unreadable_classification_rows,
)

router = APIRouter(tags=["Ingestion / Collections"])
logger = logging.getLogger(__name__)

# Allowed product-surface tags (migration r1_0029 CHECK). Same vocabulary
# as conversations' ANILALM tag; CSP governance uses ``csp``.
_COLLECTION_ORIGINS = frozenset({"csp", "anilalm"})


def _normalized_caption_model(db: Session, name: str | None) -> str | None:
    """Validate a caption-model *intent* against the registry.

    No vision-capability filter exists (model_type='vlm' is a label, not
    a guarantee). Any registered name is accepted. Unknown names 422 —
    validation lives here, not a DB CHECK, so a new model does not need
    a migration. Empty / omitted → NULL = follow VISION_MODEL.
    """
    if name is None:
        return None
    cleaned = name.strip()
    if not cleaned:
        return None
    exists = (
        db.query(ModelRegistry.id).filter(ModelRegistry.name == cleaned).first()
    )
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"caption_model「{cleaned}」不在模型清單。"
                "請先到「模型」頁登錄，或留空以跟隨平台 VISION_MODEL。"
            ),
        )
    return cleaned

# 唯一「可以被 ANILA 檢索」的密等。取自 enum,不是抄一份字串常數——
# 四級的儲存拼法只有契約層說了算(SYSTEM-MAP §8)。
_UNCLASSIFIED = ClassificationLevel.UNCLASSIFIED.to_storage()


def _refuse_classification_for_anilalm(origin: str | None, level: ClassificationLevel) -> None:
    """ANILALM 不用密等設計（擁有者 2026-08-21 裁決，Q59）——個人知識庫資源不取得任何密等分類。

    「不用密等的設計」不是「不會到達某個等級」，是「這個介面裡沒有密等這個概念」
    （擁有者兩度重申；幕僚長 2026-08-22 就此裁：**全擋，含營業秘密**）。
    所以 營業秘密／密／機密 全數拒絕，只有 無機密（地板 rank 0、所有資源的預設
    起始值）放行。「取得密等分類」＝高於無機密。

    擋在寫入端入口而不是 ``apply_classification`` 核心——核心是四級單向閂鎖的
    **唯一**入口，也是 ANILA 靠它級聯 document／落閂密等的那一半；把 anilalm
    例外塞進核心會讓 ANILA 每一次升密都多走一條 origin 分支，而且降密流程／
    稽核都吃不到這條鮮為人知的例外。分區例外屬於「產品面」層，不屬於「閂鎖」層。
    擋點只有「把 collection 推到有密等」的寫入端：建立、升密。
    （conversation 那條路在 conversation_service.classify_conversation，見該處。）
    """
    if origin == "anilalm" and level > ClassificationLevel.UNCLASSIFIED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "個人知識庫（ANILALM）不使用密等，不能設定任何密等分類"
                "（含營業秘密）。ANILALM 是個人筆記的空間，不會有受控內容；"
                "若這批資料確實是院級受控資料，請在治理中心（CSP）另建一個"
                "知識庫，再於該庫設定密等。"
            ),
        )

# Task 1 那道 CHECK 的名字(ORM ``__table_args__`` ＋ migration r1_0033 同名
# 雙宣告)。撞到它時要把驅動層訊息翻成人話,所以這裡認名字;改名會讓
# ``test_raising_classification_while_marked_says_what_to_do`` 立刻紅,
# 不會靜默退回 500。
_ANILA_SEARCHABLE_CHECK = "ck_ingestion_collections_anila_searchable_unclassified"

# 拒絕標記的訊息:措辭固定、不必帶當下資料的放這裡;要帶密等或模型名的,
# 就地寫在 ``_guard_anila_searchable``。
# ⚠ 每一則都要帶「怎麼拿到你要的東西」,而且那條路要真的走得通——這一包的
# 驗收標準不是擋住了,是被擋的人照著做之後真的拿得到他要的東西。指一條不
# 存在的路比不給路更糟:照做的人會以為是自己弄錯了。
# (刻意不寫「共 N 則」:那個數字每多一道檢查就過期一次,這個檔案已經為此
# 錯過兩輪。要知道有幾則,去數 ``_guard_anila_searchable`` 的 raise。)
_MARK_ERRORS = {
    "not_admin": (
        "只有管理員可以設定 ANILA 檢索標記。這個標記等同於把整個庫公開給"
        "全院的聊天檢索，屬於密等相鄰的決定。請把庫的網址交給管理員代為標記。"
    ),
    "anilalm": (
        "個人知識庫（origin=anilalm）不可標記為 ANILA 可檢索。"
        "個人筆記變成全院可搜不是這個功能的本意。若這批資料確實是院級法規，"
        "請在治理中心（CSP）另建一個知識庫並重新上傳，再標記那一個。"
    ),
}


# ── Authorisation helper ────────────────────────────────────────────────────


def _require_collection_access(
    db: Session, user: User, collection_id: int
) -> IngestionCollection:
    """Resolve the collection + confirm caller can manage it.

    Returns the row (callers usually need other fields anyway).
    Admin bypasses; non-admin must be the ``created_by`` owner. Future
    Sprint may add a ``collection_access_grants`` table for sharing
    across users; this helper is the single point that needs to grow
    when that lands.
    """
    coll = (
        db.query(IngestionCollection)
        .filter(IngestionCollection.id == collection_id)
        .first()
    )
    if coll is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection {collection_id} not found",
        )
    if is_admin_tier(user):
        return coll
    if coll.created_by != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"No access to collection {collection_id}",
        )
    return coll


# Back-compat alias so other endpoint files (documents.py / eval_runs.py /
# jobs.py) that still call ``_require_agent_access`` keep working until
# their Chunk Q sub-passes update them.
def _require_agent_access(db: Session, user: User, agent_id: int):  # noqa: ARG001
    """Sprint 4 deprecated — agent-scope access checks are gone.

    Existing callers pass ``coll.agent_id`` which was renamed away. To
    avoid breaking them mid-refactor, accept any int and grant access
    if the user is admin. Sub-passes in Chunk Q rewrite each caller to
    use ``_require_collection_access`` directly.
    """
    if is_admin_tier(user):
        return None
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="legacy _require_agent_access called; refactor to use _require_collection_access",
    )


# ── ANILA 檢索標記／升密失敗收尾 ───────────────────────────────────────────


def _guard_anila_searchable(db: Session, coll: IngestionCollection) -> None:
    """開標記前的四道檢查。⚠ 全部要給做法,不能只說不行。

    只在「開啟」時跑。關閉永遠放行:每一則拒絕訊息都叫人去關標記,把關閉
    也擋起來等於把自己寫的出口封死。
    """
    # (1) 密等。DB CHECK 已經擋死了,這裡先攔一次是為了把
    # 「conflicts with an existing collection」那種 409 換成看得懂的話。
    stored_level = getattr(coll, "classification_level", None) or _UNCLASSIFIED
    if stored_level != _UNCLASSIFIED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"此庫密等是「{stored_level}」，只有「{_UNCLASSIFIED}」的庫可以"
                f"標記為 ANILA 可檢索（資料庫層也擋著，改不進去）。"
                f"若這批資料實際上不需要密等，請先走降密申請流程"
                f"（POST /api/classification/declassification-requests）"
                f"降到「{_UNCLASSIFIED}」，再回來標記。"
            ),
        )

    # (2) 產品面。
    if coll.origin == "anilalm":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_MARK_ERRORS["anilalm"],
        )

    # (3) 嵌入空間。已標記集必須是同一個嵌入模型,否則 ANILA 那邊把兩組
    # 分數排在一起比大小,而那兩組分數根本不在同一個空間裡。
    marked_model = (
        db.query(IngestionCollection.embedding_model)
        .filter(
            IngestionCollection.anila_searchable.is_(True),
            IngestionCollection.id != coll.id,
        )
        .first()
    )
    if marked_model and marked_model[0] != coll.embedding_model:
        # ⚠ 出路只能寫「另建一個庫、重新上傳」,不能寫「把這個庫重新嵌入」:
        # 本平台沒有 reindex,``search.py:639`` 已經裁定過,並且明文寫著
        # 「do not write one into a user-facing message」。PATCH 也不收
        # embedding_model(送了會回 200 但什麼都沒發生)。指一條不存在的路,
        # 跟不給路是同一件事,而且更難察覺——照做的人會以為是自己弄錯了。
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"此庫用 {coll.embedding_model}，已標記集用 {marked_model[0]}；"
                f"跨嵌入空間的分數不能互相比較。既有知識庫沒有辦法換嵌入模型"
                f"（平台沒有 reindex，改 embedding_model 也不會生效），"
                f"請用 {marked_model[0]} 另建一個知識庫、重新上傳這批文件，"
                f"再標記那一個。"
            ),
        )

    # (4) 庫內文件。CHECK 只鎖 collection 那一列,管不到文件——標記時先把
    # 話講明,別讓管理員以為「庫是無機密」就代表裡面每一份都是。
    classified = (
        db.query(IngestionDocument.classification_level)
        .filter(
            IngestionDocument.collection_id == coll.id,
            IngestionDocument.classification_level != _UNCLASSIFIED,
        )
        .first()
    )
    if classified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"此庫內含密等「{classified[0]}」的文件。標記後這些文件不會被檢索，"
                "但請先確認它們是否應該留在這個庫裡：若不該留，請移到另一個庫再標記；"
                "若該留且其實不需要密等，請走降密申請流程。"
            ),
        )


def _abort_raise_rolled_back(
    exc: Exception,
    collection_id: int,
    previous: ClassificationLevel,
    target: ClassificationLevel,
) -> NoReturn:
    """升密整批失敗的收尾（呼叫端已經 rollback 過）。

    說清楚「什麼都沒動」是重點——沒有訊息的 500 會讓操作者直覺重試，
    而重試在半套狀態下會成功並看起來正常。錯誤內文只給例外類型，
    細節留在伺服器日誌（不把內部訊息回給呼叫端）。
    """
    logger.exception(
        "collection classification raise failed collection_id=%s %s→%s",
        collection_id,
        previous.to_storage(),
        target.to_storage(),
    )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=(
            f"升密失敗，已整批回復：知識庫仍為「{previous.to_storage()}」，"
            f"庫內文件密等一律未變更，重試是安全的。"
            f"錯誤類型 {type(exc).__name__}，細節見伺服器日誌。"
        ),
    ) from exc


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post(
    "/api/ingestion/collections",
    response_model=CollectionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_collection(
    payload: CollectionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Create a new (empty) collection owned by the calling user."""
    from app.services.platform_embedding import (
        LAST_RESORT_EMBEDDING_MODEL,
        canonical_embedding_model_name,
        resolve_platform_embedding,
    )

    # FAKE-CONTROLS #56: this column is compared against model_registry
    # names, and migration r1_0018 copied it onto chunk provenance. A
    # caller-supplied spelling that differs only in case is therefore a
    # corpus that silently retrieves nothing, so store the registry's
    # own spelling rather than whatever arrived.
    embedding_model = canonical_embedding_model_name(db, payload.embedding_model)
    if not embedding_model:
        resolved = resolve_platform_embedding(db)
        if resolved is not None:
            # Already a model_registry name — canonical by construction.
            embedding_model = resolved.name
        else:
            # Last-resort default so collection create never becomes a new
            # gate before an admin designates a platform embedding.
            embedding_model = LAST_RESORT_EMBEDDING_MODEL

    # Schema validator already normalised the label; re-parse so a future
    # schema drift cannot store a string the latch / bind rule reject.
    try:
        level = ClassificationLevel.from_storage(payload.classification_level)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="classification_level 必須是四級之一：無機密、營業秘密、密、機密",
        ) from exc

    origin = payload.origin or None
    if origin is not None and origin not in _COLLECTION_ORIGINS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="origin 必須是 'csp' 或 'anilalm'",
        )

    # ANILALM 不用密等（Q59）：個人知識庫不取得密等。擋在寫入前。
    # ⚠ 正確性命門，非早閘：建庫直接寫 ORM 列、不經 apply_classification 核心，
    #   正確性不由核心補——刪掉會紅（Reviewer 實測 2 failed），不可刪。
    _refuse_classification_for_anilalm(origin, level)

    caption_model = _normalized_caption_model(db, payload.caption_model)

    coll = IngestionCollection(
        name=payload.name,
        description=payload.description,
        chunking_config=payload.chunking_config.model_dump(),
        embedding_model=embedding_model,
        embedding_dim=payload.embedding_dim,
        status="active",
        document_count=0,
        chunk_count=0,
        bytes_stored=0,
        created_by=current_user.id,
        origin=origin,
        classification_level=level.to_storage(),
        caption_enabled=payload.caption_enabled,
        caption_model=caption_model,
    )
    db.add(coll)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        # Log the raw driver error server-side; never leak schema / constraint
        # details (e.orig) to the API client.
        logger.warning("collection create IntegrityError: %s", e.orig)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Collection creation failed: a collection with these attributes may already exist.",
        ) from e
    db.refresh(coll)

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_collection_create",
        resource_type="ingestion_collection",
        resource_id=coll.id,
        metadata={
            "name": payload.name,
            "created_by": current_user.id,
            "origin": origin,
            "classification_level": level.to_storage(),
            "caption_enabled": payload.caption_enabled,
            "caption_model": caption_model,
        },
    )
    return CollectionResponse.model_validate(coll)


@router.get(
    "/api/ingestion/collections",
    response_model=list[CollectionResponse],
)
def list_collections(
    include_archived: bool = Query(
        False, description="預設只列 active；True 連 archived 一起回"
    ),
    owned_only: bool = Query(
        True,
        description=(
            "預設只列自己的 collections；admin 設 False 可看全部"
        ),
    ),
    origin: Optional[str] = Query(
        None,
        description=(
            "只列此產品面建立的知識庫（csp / anilalm）。"
            "NULL origin 的舊列仍會一併回傳，避免既有語料從貨架消失。"
        ),
    ),
    exclude_origin: Optional[str] = Query(
        None,
        description=(
            "排除此產品面。NULL origin 舊列保留。"
            "與 origin 互斥；語意比照 /api/conversations。"
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[CollectionResponse]:
    """List collections accessible to the current user.

    Sprint 4: no ``agent_id`` filter. Default behaviour:
    - non-admin: only own collections (admin-bypass when ``owned_only=False``
      is rejected for non-admins).
    - admin: own collections by default; pass ``owned_only=false`` to
      see every collection on the platform.

    Origin filter (r1_0029): same shape as conversations — CSP governance
    passes ``origin=csp``, ANILALM passes ``origin=anilalm``. Pre-origin
    rows (``origin IS NULL``) stay visible under every surface so an
    existing corpus is never orphaned.
    """
    if not owned_only and not is_admin_tier(current_user):
        raise HTTPException(
            status_code=403,
            detail="owned_only=false requires admin role",
        )
    if origin is not None and exclude_origin is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="origin and exclude_origin are mutually exclusive",
        )
    if origin is not None and origin not in _COLLECTION_ORIGINS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="origin 必須是 'csp' 或 'anilalm'",
        )
    if exclude_origin is not None and exclude_origin not in _COLLECTION_ORIGINS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="exclude_origin 必須是 'csp' 或 'anilalm'",
        )

    q = db.query(IngestionCollection)
    if owned_only:
        q = q.filter(IngestionCollection.created_by == current_user.id)
    if not include_archived:
        q = q.filter(IngestionCollection.status == "active")
    if origin is not None:
        q = q.filter(
            or_(
                IngestionCollection.origin == origin,
                IngestionCollection.origin.is_(None),
            )
        )
    elif exclude_origin is not None:
        q = q.filter(
            or_(
                IngestionCollection.origin.is_(None),
                IngestionCollection.origin != exclude_origin,
            )
        )
    rows = q.order_by(IngestionCollection.id).all()
    return [CollectionResponse.model_validate(r) for r in rows]


@router.get(
    "/api/ingestion/collections/{collection_id}",
    response_model=CollectionResponse,
)
def get_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    coll = _require_collection_access(db, current_user, collection_id)
    return CollectionResponse.model_validate(coll)


@router.post(
    "/api/ingestion/collections/{collection_id}/classification",
    response_model=CollectionResponse,
)
def raise_collection_classification(
    collection_id: int,
    payload: CollectionClassificationRaise,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    """Raise a collection's classification level (one-way latch).

    Goes through ``apply_classification`` — never writes the column
    directly — so ClassificationEvent + memory-purge side effects stay
    on the single latch path. Lowering is refused with a pointer to the
    declassification flow (``apply_classification`` would no-op; we turn
    that into an explicit error instead of a misleading 200).

    Before writing: refuse if any bound agent is below the new level
    (name the agents; do not silently raise them). Documents below the
    new level are cascaded via ``apply_classification`` in the **same
    transaction** as the collection's own latch — all-or-nothing.

    Atomicity (this is the whole point of the ``commit=False`` plumbing):
    the latch is one-way, so a half-applied raise is not a retryable
    blip — it strands documents at a level only the three-party
    declassification flow can undo, one document at a time, while the
    caller sees a failure and retries into what looks like success. So
    every write here lives in one transaction and there is exactly one
    ``db.commit()``. Any failure rolls the whole set back.

    Atomicity does **not** depend on row locking: ``apply_classification``
    does take ``SELECT … FOR UPDATE`` (a no-op under SQLite), but what
    makes this all-or-nothing is the single enclosing transaction.
    Locking only narrows the concurrent-raise window, and that case is
    handled explicitly by the ``event is None`` branch below.

    Known cost of that choice: the transaction (and, under Postgres, the
    row locks on every cascaded document) lives for the whole cascade,
    so a raise on a very large collection is one long write transaction.
    That is the price of not stranding documents, and this is a rare,
    admin-triggered, non-streaming operation. If collections ever get
    large enough for it to matter, the answer is batching with a
    resumable record of what advanced — not going back to per-document
    commits, which is the bug this replaced.

    Auth matches other collection mutations (owner or admin-tier). Not
    looser than ``create_collection`` (any authenticated user may create
    at any level today).
    """
    coll = _require_collection_access(db, current_user, collection_id)
    # The Pydantic validator on ``CollectionClassificationRaise`` already
    # rejects the four-value violation with 422 before the route body runs,
    # so no defensive re-parse of ``payload`` is needed here.
    target = ClassificationLevel.from_storage(payload.classification_level)
    # ANILALM 不用密等（Q59）：這支路由是「把庫推到密以上」的第二條路。
    # origin 是列值不是 payload，所以不是拼錯 product surface——任何人
    # （含 admin）用這支把 anilalm 庫升到「密」以上都必須明確失敗。
    # ⚠ 這裡是效能／訊息用的早閘：正確性由 apply_classification 核心保證，
    #   刪掉不紅（Reviewer 實測仍綠）。好處＝更早 403、也省掉「綁定 agent 檢查」
    #   與文件級聯的掃描——**未量測，「省一次級聯掃描」是理論，不是量到的數字。**
    _refuse_classification_for_anilalm(getattr(coll, "origin", None), target)

    stored_previous = getattr(coll, "classification_level", None) or "無機密"
    try:
        previous = ClassificationLevel.from_storage(stored_previous)
    except ValueError as exc:
        # Only reachable if something wrote the column outside the app.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"知識庫 #{collection_id} 的密等儲存值「{stored_previous}」不是"
                f"四級之一（無機密／營業秘密／密／機密），無法判斷是否為升密，"
                f"因此整批拒絕、未做任何變更。請先修正該筆資料再重試。"
            ),
        ) from exc

    if target < previous:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"知識庫密等只能往上調（目前「{previous.to_storage()}」，"
                f"請求「{target.to_storage()}」）。"
                f"降級請走降密申請流程"
                f"（POST /api/classification/declassification-requests），"
                f"須主管核准，不可由此路由自行降級。"
            ),
        )
    if target == previous:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"知識庫已是「{previous.to_storage()}」，無須升密。"
                f"若要降級，請走降密申請流程。"
            ),
        )

    under = agents_bound_below_level(db, collection_id, target)
    if under:
        named = "、".join(
            f"「{a.name}」(id={a.id}，有效密等「"
            f"{effective_agent_policy_level(a).to_storage()}」)"
            for a in under
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"無法將知識庫升至「{target.to_storage()}」：下列已綁定的 "
                f"agent 有效密等低於目標等級：{named}。"
                f"請先透過 POST /api/agents/{{id}}/classification "
                f"將那些 agent 升至「{target.to_storage()}」以上，"
                f"再重試本知識庫升密。系統不會自動提升 agent。"
            ),
        )

    # Pre-flight: a document row whose stored level is not one of the four
    # values would blow up mid-cascade. Refuse the whole raise and name the
    # rows rather than half-applying — "silently skipping" would leave a
    # document below its collection, which is invariant (a) inverted.
    unreadable = unreadable_classification_rows(db, collection_id)
    if unreadable:
        named = "、".join(
            f"文件 #{doc_id}（儲存值「{stored}」）" for doc_id, stored in unreadable
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"無法升密：下列文件的密等儲存值不是四級之一"
                f"（無機密／營業秘密／密／機密）：{named}。"
                f"整批拒絕、未做任何變更——修正這些資料列後再重試。"
            ),
        )

    # ── single transaction: documents + collection + audit ──────────────
    try:
        raised_doc_ids = cascade_raise_documents(
            db,
            collection_id=collection_id,
            new_level=target,
            actor_user_id=current_user.id,
            commit=False,
        )

        event = apply_classification(
            db,
            resource_type="collection",
            resource_id=str(collection_id),
            new_level=target.to_storage(),
            actor_type="user",
            actor_id=str(current_user.id),
            reason="manual_admin",
            source="manual_admin",
            commit=False,
        )
        if event is None:
            # Concurrent raise or stale session. Roll the cascade back too —
            # otherwise the caller gets 409 while documents stayed raised.
            db.rollback()
            db.refresh(coll)
            current = ClassificationLevel.from_storage(
                getattr(coll, "classification_level", None) or "無機密"
            )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"知識庫密等未變更（目前「{current.to_storage()}」；"
                    f"請求「{target.to_storage()}」）。若目標未高於現行等級，"
                    f"升密不會生效；降級請走降密申請流程。"
                    f"本次未變更任何文件密等。"
                ),
            )

        log_audit_event(
            db,
            commit=False,
            actor=current_user,
            action="ingestion_collection_classification_raise",
            resource_type="ingestion_collection",
            resource_id=coll.id,
            metadata={
                "name": coll.name,
                "from_level": previous.to_storage(),
                "to_level": target.to_storage(),
                "classification_event_id": event.id,
                "cascaded_document_ids": raised_doc_ids,
            },
        )
        db.commit()
    except HTTPException:
        raise
    except IntegrityError as exc:
        # 這條路是 collection 密等的唯一寫入點,所以 Task 1 那道
        # 「標記了就不准升密」的 CHECK 只可能在這裡撞到。不翻譯的話它會
        # 掉進下面那個 500——操作者只看得到「錯誤類型 IntegrityError」,
        # 而他其實只差一個「先取消標記」的動作。
        db.rollback()
        if _ANILA_SEARCHABLE_CHECK in str(exc.orig):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"此庫已標記為 ANILA 可檢索，不能升密——已整批回復，"
                    f"知識庫與庫內文件的密等都沒有變更。"
                    f"請先取消 ANILA 檢索標記"
                    f"（PATCH /api/ingestion/collections/{collection_id} "
                    f"帶 anila_searchable=false），再調整密等。"
                ),
            ) from exc
        _abort_raise_rolled_back(exc, collection_id, previous, target)
    except Exception as exc:
        db.rollback()
        _abort_raise_rolled_back(exc, collection_id, previous, target)

    db.refresh(coll)
    return CollectionResponse.model_validate(coll)


@router.patch(
    "/api/ingestion/collections/{collection_id}",
    response_model=CollectionResponse,
)
def update_collection(
    collection_id: int,
    payload: CollectionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CollectionResponse:
    coll = _require_collection_access(db, current_user, collection_id)

    changed: dict[str, object] = {}
    # ── ANILA 檢索標記 ──────────────────────────────────────────────────
    # 擁有者本人也不行:``_require_collection_access`` 放行的是「管理自己的庫」,
    # 而標記的影響範圍是全院的聊天檢索,不是這一個庫。所以在這裡多一道 admin
    # 閘,而不是去改那個 helper——它有 19 個 call site、散在 9 個檔案
    # (agents/registration、conversations、本檔、documents、eval_runs、
    # image_blob、jobs、relations、search),動它會一併放寬文件上傳與刪除。
    mark_flip: Optional[tuple[bool, bool]] = None
    if payload.anila_searchable is not None:
        if not is_admin_tier(current_user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=_MARK_ERRORS["not_admin"],
            )
        was = bool(coll.anila_searchable)
        wants = bool(payload.anila_searchable)
        if wants != was:
            if wants:
                _guard_anila_searchable(db, coll)
            coll.anila_searchable = wants
            changed["anila_searchable"] = wants
            mark_flip = (was, wants)

    if payload.name is not None:
        coll.name = payload.name
        changed["name"] = payload.name
    if payload.description is not None:
        coll.description = payload.description
        changed["description"] = payload.description
    if payload.chunking_config is not None:
        coll.chunking_config = payload.chunking_config.model_dump()
        changed["chunking_config"] = coll.chunking_config
    if payload.status is not None:
        coll.status = payload.status
        changed["status"] = payload.status
    if "caption_enabled" in payload.model_fields_set:
        coll.caption_enabled = payload.caption_enabled
        changed["caption_enabled"] = payload.caption_enabled
    if "caption_model" in payload.model_fields_set:
        coll.caption_model = _normalized_caption_model(db, payload.caption_model)
        changed["caption_model"] = coll.caption_model

    if not changed:
        return CollectionResponse.model_validate(coll)

    coll.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        logger.warning("collection update IntegrityError: %s", e.orig)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Collection update failed: the change conflicts with an existing collection.",
        ) from e
    db.refresh(coll)

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_collection_update",
        resource_type="ingestion_collection",
        resource_id=coll.id,
        metadata={"changed": list(changed.keys())},
    )
    if mark_flip is not None:
        # 標記翻面自己一列:全院檢索範圍的變動要能單獨查,不必從一堆
        # 「changed: [...]」裡撈。
        log_audit_event(
            db,
            commit=True,
            actor=current_user,
            action="ingestion_collection_anila_searchable_set",
            resource_type="ingestion_collection",
            resource_id=coll.id,
            metadata={
                "name": coll.name,
                "from": mark_flip[0],
                "to": mark_flip[1],
                "classification_level": coll.classification_level,
                "embedding_model": coll.embedding_model,
            },
        )
    return CollectionResponse.model_validate(coll)


@router.delete(
    "/api/ingestion/collections/{collection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_collection(
    collection_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Hard delete a collection.

    CASCADE drops every document and chunk in pgvector. There is no
    soft-delete here — admin-or-owner only operation, and there's no
    audit benefit to keeping orphan rows because the audit_log has a
    timestamped record of the delete itself.
    """
    coll = _require_collection_access(db, current_user, collection_id)
    snapshot = {"name": coll.name, "created_by": coll.created_by}
    db.delete(coll)
    db.commit()
    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_collection_delete",
        resource_type="ingestion_collection",
        resource_id=collection_id,
        metadata=snapshot,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
