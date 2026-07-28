"""Ingestion documents endpoint group.

Sprint 1 ships:

- ``POST /api/ingestion/collections/{id}/documents`` — multipart upload,
  writes blob to UPLOAD_DIR, then atomically INSERTs the document, job, and
  durable dispatch intent. A background relay publishes the Arq job.
- ``GET  /api/ingestion/collections/{id}/documents`` — paginated list of
  documents in a collection.
- ``GET  /api/ingestion/documents/{id}`` — detail row + last job row.

Document upload is the API end of the pipeline; the worker takes over after
the durable relay publishes the intent. The dev sees the document in 'pending'
status immediately, then 'parsing' / 'chunking' / 'embedding' / 'indexed'
as the worker advances.
"""

from __future__ import annotations

from app.schemas.base import ApiResponseModel

import asyncio
import hashlib
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anila_contracts import Classification
from anila_core.ingestion.citation_extractor import normalize_title
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore

from app.api.ingestion.collections import _require_collection_access
from app.api.ingestion.surface import CollectionOrigin, surface_origin_dep
from app.modules.clearance.service import (
    ClearancePolicyDataError,
    resolve_and_evaluate_data_access,
)
from app.modules.policy import apply_classification
from app.schemas.contracts.policy import PolicyActorType
from app.database import get_db
from app.config import settings
from app.models.ingestion import (
    IngestionCollection,
    IngestionDocument,
    IngestionJob,
)
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user
from app.services.ingestion_outbox import create_ingestion_dispatch
from app.services.content_sniffing import (
    ContentValidationError,
    validate_content,
    validate_zip_archive,
)
from app.services.retention_reaper import RetentionSafetyError, safe_ingestion_path

router = APIRouter(tags=["Ingestion / Documents"])


_UPLOAD_DIR = os.environ.get("INGESTION_UPLOAD_DIR", "/var/anila/ingestion-uploads")

# Sprint 1 hard cap. Larger files are a Sprint 2 concern (chunked upload,
# resumable, progress) — for now hard-fail with 413.
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# Sprint 5 X / M2: zip 解壓總量上限。每檔 50MB × 200 檔 = 10GB 太寬鬆，
# 真正想處理的是「一次塞 200 個小檔」而不是「200 個極限大檔」；用
# total cap 1GB 限制磁碟寫入量，超過時直接停止後續解壓。
_ZIP_MAX_TOTAL_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB

# 控制 zip 內檔名能用的字元 — 阻擋 NUL / CR / LF（Content-Disposition
# header 注入）以及任何 ``..`` segment（路徑遍歷顯示偽裝）。儲存路徑用
# sha256 不會受影響，但使用者下載時的 ``filename`` 一定要乾淨。
_FILENAME_BAD_CHARS = ("\x00", "\r", "\n")


def _zip_member_name(member: zipfile.ZipInfo) -> str:
    """還原 zip 內檔名的正確編碼。

    ``zipfile`` 對「沒設 UTF-8 旗標(general-purpose bit 11 / 0x800)」的 entry
    一律用 CP437 解檔名;但 Windows 內建壓縮存的中文檔名其實是 CP950/Big5(或
    GBK)→ 被 CP437 解成亂碼。偵測到無 UTF-8 旗標時,把字串還原成原始 bytes 再用
    台灣常見編碼重解。單檔上傳沒這問題(檔名來自 multipart,本來就 UTF-8)。
    """
    import os

    name = member.filename
    if member.flag_bits & 0x800:
        return name  # entry 已標 UTF-8,zipfile 解對了
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name  # 不是 CP437 能表示的,維持原樣
    # 純 ASCII 檔名在 CP437/CP950/UTF-8 下位元組相同,zipfile 本來就解對 → 別動,
    # 避免把合法 ASCII 名誤判成需要轉碼。
    if all(b < 0x80 for b in raw):
        return name
    # 非 UTF-8 旗標 + 含非 ASCII bytes → 多半是本地碼頁存的 CJK 檔名被 zipfile 用
    # CP437 誤解。台灣內網優先 CP950(Big5 是其子集);可由 ANILA_ZIP_FILENAME_ENC
    # 覆寫(例如 gbk)。⚠ 啟發式:各 CJK 碼頁 byte 範圍重疊,"decode 成功" 不保證
    # 100% 正確,但對單一語系內網是合理預設。
    encs = [e for e in (os.getenv("ANILA_ZIP_FILENAME_ENC"), "cp950", "gbk") if e]
    for enc in encs:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return name


def _sanitize_archive_filename(raw: str, *, preserve_folder_structure: bool) -> str:
    """Return a safe ``filename`` for documents pulled from an uploaded zip.

    - ``preserve_folder_structure=False``: keep basename only.
    - ``preserve_folder_structure=True``: keep relative path BUT collapse
      any ``..`` segments and reject control chars. We never use this name
      to build a filesystem path (that's ``storage_path`` derived from
      sha256), but it is echoed back in JSON responses and as the download
      ``Content-Disposition``, so we still need to keep it free of CRLF.
    """
    name = raw or "upload"
    if preserve_folder_structure:
        # Drop leading slashes / drive letters; collapse "..".
        parts = [p for p in name.replace("\\", "/").split("/") if p and p != "."]
        parts = [p for p in parts if p != ".."]
        name = "/".join(parts) or "upload"
    else:
        name = os.path.basename(name) or "upload"

    for ch in _FILENAME_BAD_CHARS:
        name = name.replace(ch, "_")
    # Cap the displayable length so a malicious 65k-char filename can't
    # be persisted (DB column is 1000 wide; trim safely below that).
    return name[:512]


def _derive_title(filename: str, explicit: str | None = None) -> tuple[str | None, str | None]:
    """Pick a document ``title`` + its ``normalized_title`` for citation resolution.

    Phase 1 of document-relations: citation targets resolve by matching a
    cited regulation name against ``ingestion_documents.normalized_title``, so
    every document needs a best-effort title. Source priority:

    1. ``explicit`` — the uploader knows the real regulation name (the most
       reliable source per design §13; ROC regs are often named by 字號 inside
       the file but the human knows the canonical name).
    2. filename stem — strip the extension and any path; a deterministic
       fallback so the column is never null.

    The worker (task #40) may later refine ``title`` from a parsed first
    heading. Returns ``(title, normalized_title)``; ``normalized_title`` is the
    durable join key (NFKC + brackets/whitespace stripped).
    """
    raw = (explicit or "").strip()
    if not raw:
        base = os.path.basename(filename or "")
        raw = os.path.splitext(base)[0].strip()
    if not raw:
        return None, None
    title = raw[:500]
    return title, (normalize_title(title) or None)


class DocumentResponse(ApiResponseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    collection_id: int
    filename: str
    title: str | None = None
    normalized_title: str | None = None
    sha256: str
    mime_type: str | None
    bytes: int | None
    status: str
    availability_status: str
    processing_stage: str
    active_generation_id: int | None
    chunk_count: int
    error_message: str | None
    uploaded_by: int | None
    uploaded_at: datetime
    indexed_at: datetime | None
    # W2-11 lets an uploader declare a level above the collection floor, but
    # without these two fields nobody can see the result: the uploader gets no
    # confirmation and a reviewer browsing the collection cannot spot a
    # mislabel without going through the sampling report. Declaring something
    # you can never read back is not a correctness control.
    classification_level: str | None = None
    classification_source: str | None = None


class DocumentDetailResponse(DocumentResponse):
    """Document row + last job row for status display."""

    latest_job_id: int | None = None
    latest_job_status: str | None = None
    latest_job_error_code: str | None = None
    arq_job_id: str | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_collection(
    db: Session,
    user: User,
    collection_id: int,
    *,
    origin: "OriginArg | None" = None,
) -> IngestionCollection:
    """Sprint 4: collection access keyed on ownership, not agent_id.

    ``origin`` is required at the resolver boundary. Callers under an
    HTTP surface mount may omit it and we supply ``require_surface_origin()``;
    direct unit-test invocations of endpoint functions must pass
    ``origin=`` explicitly (no ambient ContextVar).
    """
    from app.api.ingestion.surface import OriginArg, require_surface_origin

    effective = origin if origin is not None else require_surface_origin()
    return _require_collection_access(
        db, user, collection_id, origin=effective
    )


def _require_document_data_clearance(
    db: Session, *, user: User, document: IngestionDocument
) -> None:
    """Require canonical data authority in addition to management ACL.

    Inspector endpoints expose document bytes (or direct derivatives of those
    bytes), so collection ownership/admin status is never sufficient.  The
    shared evaluator checks classification, every required compartment,
    need-to-know, and collection membership under one active grant.
    """

    try:
        decision = resolve_and_evaluate_data_access(
            db,
            user_id=user.id,
            collection_id=document.collection_id,
            document_id=document.id,
        )
    except (LookupError, ClearancePolicyDataError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="clearance policy data invalid; document access denied",
        ) from exc
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="clearance/compartment/need-to-know/collection grant insufficient",
        )


def _locked_collection_classification(db: Session, collection_id: int) -> str:
    """Read the canonical collection floor under a writer-conflicting lock.

    PostgreSQL emits ``FOR SHARE``; SQLite test fixtures safely ignore the lock
    clause. Holding it until the document INSERT commits prevents a concurrent
    collection upgrade from racing a lower child into existence.
    """
    row = (
        db.query(IngestionCollection.classification_level)
        .filter(IngestionCollection.id == collection_id)
        .with_for_update(read=True)
        .populate_existing()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Collection not found")
    raw = row[0]
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=409,
            detail="Collection classification state is invalid",
        )
    try:
        return Classification.from_storage(raw).to_storage()
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail="Collection classification state is invalid",
        ) from exc


def _resolve_document_upload_classification(
    db: Session,
    *,
    collection_id: int,
    declared_raw: str | None,
    actor: User,
) -> tuple[str, str]:
    """W2-11 per-document classification declaration (upward-only).

    Returns ``(effective_level, classification_source)``.

    - Omitted / blank → inherit the locked collection floor.
    - Below the collection floor → 400 (must not lower via upload).
    - Above the collection floor → latch the collection via the existing
      ``apply_classification`` path (writes ClassificationEvent); upload
      is accepted, never rejected for an upward declaration.
    - Unknown literal → 422 fail-closed.
    """
    floor_storage = _locked_collection_classification(db, collection_id)
    floor = Classification.from_storage(floor_storage)

    if (
        declared_raw is None
        or not isinstance(declared_raw, str)
        or not declared_raw.strip()
    ):
        return floor_storage, "collection_inherited"

    try:
        declared = Classification.from_storage(str(declared_raw).strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "classification_level 必須是五級之一："
                "無機密 / 營業秘密 / 機密 / 極機密 / 絕對機密"
            ),
        ) from exc

    if declared.rank < floor.rank:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"文件密等「{declared.to_storage()}」低於知識庫下限"
                f"「{floor.to_storage()}」(僅允許上調，不可下調)"
            ),
        )

    if declared.rank > floor.rank:
        # Reuse the platform one-way latch — never invent a parallel path.
        apply_classification(
            db,
            resource_type="collection",
            resource_id=str(collection_id),
            new_level=declared.to_storage(),
            actor_type=PolicyActorType.USER.value,
            actor_id=str(actor.id),
            reason="manual_admin",
            source="uploader_declared",
            commit=False,
        )
        return declared.to_storage(), "uploader_declared"

    return floor_storage, "collection_inherited"


def _persist_blob(content: bytes, sha256: str) -> str:
    """Write the upload to disk under a content-addressable path.

    Path = ``UPLOAD_DIR/<sha256[:2]>/<sha256>`` so we get a flat 2-deep
    directory structure (~256 entries per top-level dir even at scale).
    Same sha256 → same path → re-uploads are no-ops at the FS layer.
    The DB layer separately enforces the (collection_id, sha256) unique.
    """
    sub = os.path.join(_UPLOAD_DIR, sha256[:2])
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, sha256)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)
    return path


@dataclass(frozen=True)
class _ZipMemberStage:
    """CPU/IO staging outcome for one zip member (no DB touches).

    ``cumulative_delta`` mirrors the original loop: empty / too_large /
    unzip failure contribute 0; once a non-empty payload under the per-file
    cap is inflated, its ``size`` counts toward the archive total even if
    the total cap then trips before persist or sniffing later rejects it.
    """

    outcome: str
    result_filename: str
    detail: str | None = None
    size: int = 0
    cumulative_delta: int = 0
    sha256: str | None = None
    storage_path: str | None = None
    media_type: str | None = None


def _stage_zip_member(
    zf: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    *,
    out_name: str,
    in_zip_path: str,
    remaining_budget: int,
) -> _ZipMemberStage:
    """Inflate / sniff / hash / optionally persist one member (sync, off-loop).

    ``remaining_budget`` is ``_ZIP_MAX_TOTAL_BYTES - cumulative_bytes`` *before*
    this member. Persist runs only when ``size <= remaining_budget``, so a
    member that would push the archive over the 1 GB cap is never written —
    matching the original check-before-``_persist_blob`` order.

    Called via ``asyncio.to_thread`` so zlib / sniffing / sha256 / disk I/O
    cannot freeze the single uvicorn event loop (see proxy service's
    ``to_thread`` rationale: doing sync heavy work on the MainThread freezes
    every concurrent coroutine).
    """
    try:
        content = zf.read(member)
    except Exception as e:
        return _ZipMemberStage(
            outcome="unzip_error",
            result_filename=in_zip_path,
            detail=f"unzip failed: {type(e).__name__}",
        )

    size = len(content)
    if size == 0:
        return _ZipMemberStage(
            outcome="empty",
            result_filename=out_name,
            detail="empty file",
        )
    if size > _MAX_BYTES:
        return _ZipMemberStage(
            outcome="too_large",
            result_filename=out_name,
            detail=f"{size:,} bytes exceeds {_MAX_BYTES:,} limit",
        )

    # Count toward the archive total first (same order as the pre-to_thread
    # loop), then refuse persist when this member alone pushes over the cap.
    if size > remaining_budget:
        return _ZipMemberStage(
            outcome="over_total",
            result_filename=out_name,
            detail="archive total exceeds 1 GB cap (this file pushed over)",
            size=size,
            cumulative_delta=size,
        )

    try:
        sniffed = validate_content(content, filename=out_name)
    except ContentValidationError as exc:
        return _ZipMemberStage(
            outcome="content_error",
            result_filename=out_name,
            detail=f"content rejected: {exc}",
            size=size,
            cumulative_delta=size,
        )

    sha256 = hashlib.sha256(content).hexdigest()
    storage_path = _persist_blob(content, sha256)
    return _ZipMemberStage(
        outcome="ready",
        result_filename=out_name,
        size=size,
        cumulative_delta=size,
        sha256=sha256,
        storage_path=storage_path,
        media_type=sniffed.media_type,
    )


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post(
    "/collections/{collection_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    collection_id: int,
    file: UploadFile = File(...),
    title: Annotated[
        str | None,
        Form(
            description=(
                "Optional canonical regulation/document name used to resolve "
                "cross-document citation targets. Falls back to the filename stem."
            ),
        ),
    ] = None,
    classification_level: Annotated[
        str | None,
        Form(
            description=(
                "W2-11:optional per-document classification. Defaults to the "
                "collection floor; upward-only. Declaring above the floor latches "
                "the whole collection via apply_classification."
            ),
        ),
    ] = None,
    origin: CollectionOrigin = Depends(surface_origin_dep),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentResponse:
    """Accept one file and persist its durable ingestion intent.

    Returns 202 (Accepted) — the document row is written but indexing
    happens async. Caller polls ``GET /api/ingestion/documents/{id}``
    to watch status transitions.
    """
    _resolve_collection(db, current_user, collection_id, origin=origin)

    # Read fully into memory — Sprint 1 caps uploads at 50 MB so this is
    # fine; Sprint 2 streaming upload will spool to disk in chunks.
    content = await file.read()
    size = len(content)
    if size == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if size > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size:,} > {_MAX_BYTES:,} bytes)",
        )
    try:
        sniffed = validate_content(
            content,
            filename=file.filename or "",
            declared_mime=file.content_type,
        )
    except ContentValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    sha256 = hashlib.sha256(content).hexdigest()
    storage_path = _persist_blob(content, sha256)

    doc_title, doc_norm_title = _derive_title(file.filename or "", title)
    classification_level_value, classification_source = (
        _resolve_document_upload_classification(
            db,
            collection_id=collection_id,
            declared_raw=classification_level,
            actor=current_user,
        )
    )
    classified_at = datetime.now(timezone.utc)

    # Insert the document row. Uniqueness on (collection_id, sha256) gives
    # us cheap content-level dedup — re-uploading the same file just
    # returns the existing row.
    doc = IngestionDocument(
        collection_id=collection_id,
        filename=file.filename or sha256,
        title=doc_title,
        normalized_title=doc_norm_title,
        sha256=sha256,
        mime_type=sniffed.media_type,
        bytes=size,
        storage_path=storage_path,
        status="pending",
        chunk_count=0,
        uploaded_by=current_user.id,
        classification_level=classification_level_value,
        classification_latched_at=classified_at,
        classification_source=classification_source,
        archive_due_at=datetime.now(timezone.utc) + timedelta(
            days=settings.RETENTION_INGESTION_ACTIVE_DAYS
        ),
    )
    db.add(doc)
    try:
        job = create_ingestion_dispatch(
            db,
            document=doc,
            enqueued_by=current_user.id,
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        # Same content already uploaded to this collection — return the
        # existing row instead of erroring. Idempotent uploads matter
        # for retry-prone clients.
        existing = (
            db.query(IngestionDocument)
            .filter(
                IngestionDocument.collection_id == collection_id,
                IngestionDocument.sha256 == sha256,
            )
            .first()
        )
        if existing is None:
            raise HTTPException(status_code=500, detail="Upload conflict")
        return DocumentResponse.model_validate(existing)
    db.refresh(doc)

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_document_upload",
        resource_type="ingestion_document",
        resource_id=doc.id,
        metadata={
            "collection_id": collection_id,
            "filename": doc.filename,
            "size": size,
            "arq_job_id": job.arq_job_id,
            "classification_level": classification_level_value,
            "classification_source": classification_source,
        },
    )
    return DocumentResponse.model_validate(doc)


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=DocumentResponse,
)
async def reprocess_document(
    document_id: int,
    origin: CollectionOrigin = Depends(surface_origin_dep),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentResponse:
    """重新嵌入既有 document(通常是 parse/embedding 失敗、卡在 status='failed' 的)。

    失敗檔原本沒有重試入口,而 (collection_id, sha256) unique 也擋住重傳同一個檔
    → 資料卡死。這個端點讓 owner/admin 對既有 row 直接 re-enqueue ingest job,不必
    重傳;重設 status='pending' 並清掉上次的 error_message。進行中的檔拒絕重塞,
    避免同一檔並行兩個 job。
    """
    doc = db.get(IngestionDocument, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    # 透過所屬 collection 做存取控管(與上傳走同一條授權路徑)。
    _resolve_collection(db, current_user, doc.collection_id, origin=origin)
    # 只允許重試「失敗」的檔 — 其餘狀態各有正常流程,避免重複塞 job。
    if doc.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Only failed documents can be reprocessed (current: {doc.status})",
        )
    # 原子狀態轉移:conditional UPDATE failed→pending,只有真的搶到(rowcount==1)
    # 才往下 enqueue,擋住兩個並行請求同時 re-enqueue 同一份文件(競態 + 重複 job)。
    claimed = (
        db.query(IngestionDocument)
        .filter(
            IngestionDocument.id == document_id,
            IngestionDocument.status == "failed",
        )
        .update(
            {"status": "pending", "error_message": None},
            synchronize_session=False,
        )
    )
    if claimed != 1:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Document is no longer in a failed state"
        )
    doc.status = "pending"
    doc.error_message = None
    job = create_ingestion_dispatch(
        db,
        document=doc,
        enqueued_by=current_user.id,
    )
    db.commit()
    db.refresh(doc)

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_document_reprocess",
        resource_type="ingestion_document",
        resource_id=doc.id,
        metadata={
            "collection_id": doc.collection_id,
            "filename": doc.filename,
            "arq_job_id": job.arq_job_id,
        },
    )
    return DocumentResponse.model_validate(doc)


class ZipUploadResult(BaseModel):
    """Per-file outcome of a multi-file zip upload."""

    filename: str
    document_id: int | None = None
    arq_job_id: str | None = None
    status: str  # 'enqueued' | 'duplicate' | 'skipped' | 'too_large' | 'error'
    detail: str | None = None


class ZipUploadResponse(BaseModel):
    """Aggregated outcome of a single zip upload."""

    files_in_archive: int
    enqueued: int
    duplicates: int
    skipped: int
    errors: int
    results: list[ZipUploadResult]


def _declared_zip_member_error(
    member,
    out_name: str,
    *,
    cumulative_bytes: int,
) -> ZipUploadResult | None:
    """Preflight zip metadata before inflating a member.

    ``ZipInfo.file_size`` is the declared uncompressed size. We still keep
    the post-read checks below as defense in depth, but this avoids inflating
    members that are already known to exceed per-file or archive-total caps.
    """
    declared_size = max(int(getattr(member, "file_size", 0) or 0), 0)
    if cumulative_bytes >= _ZIP_MAX_TOTAL_BYTES:
        return ZipUploadResult(
            filename=out_name,
            status="skipped",
            detail="archive total exceeds 1 GB cap",
        )
    if declared_size == 0:
        return ZipUploadResult(
            filename=out_name,
            status="skipped",
            detail="empty file",
        )
    if declared_size > _MAX_BYTES:
        return ZipUploadResult(
            filename=out_name,
            status="too_large",
            detail=f"{declared_size:,} bytes exceeds {_MAX_BYTES:,} limit",
        )
    if cumulative_bytes + declared_size > _ZIP_MAX_TOTAL_BYTES:
        return ZipUploadResult(
            filename=out_name,
            status="skipped",
            detail="archive total exceeds 1 GB cap (this file pushed over)",
        )
    return None


@router.post(
    "/collections/{collection_id}/documents/zip",
    response_model=ZipUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_zip(
    collection_id: int,
    file: UploadFile = File(...),
    preserve_folder_structure: bool = False,
    classification_level: Annotated[
        str | None,
        Form(
            description=(
                "W2-11:optional per-archive classification applied to every "
                "member document. Defaults to the collection floor; upward-only. "
                "Declaring above the floor latches the whole collection via "
                "apply_classification."
            ),
        ),
    ] = None,
    origin: CollectionOrigin = Depends(surface_origin_dep),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ZipUploadResponse:
    """Bulk upload via zip archive.

    Each file in the zip becomes a document in the collection. Files
    that hit the per-file 50 MB cap are reported as ``too_large`` but
    don't fail the whole upload; same-sha256 duplicates report
    ``duplicate`` and skip enqueue. Folders / dotfiles (``__MACOSX/``,
    ``.DS_Store``) are filtered out.

    ``preserve_folder_structure``: when True, prepends the in-zip path
    to ``filename`` so the inspector can group by folder. When False
    (default), only the basename is kept — useful when the zip was
    created with a "compress everything in this folder" UI that
    introduces a useless top-level wrapper.

    Hard limit: 200 files per zip. Bigger archives should be split or
    use the future Sprint 4 streaming API.
    """
    from io import BytesIO

    _resolve_collection(db, current_user, collection_id, origin=origin)

    archive_bytes = await file.read()
    if len(archive_bytes) > 500 * 1024 * 1024:  # 500 MB cap on archive
        raise HTTPException(status_code=413, detail="Zip archive > 500 MB")
    try:
        validate_zip_archive(
            archive_bytes,
            filename=file.filename or "",
            declared_mime=file.content_type,
        )
    except ContentValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        zf = zipfile.ZipFile(BytesIO(archive_bytes))
    except zipfile.BadZipFile as e:
        raise HTTPException(status_code=400, detail=f"Not a valid zip: {e}") from e

    # Filter to actual file entries; reject anything that smells dodgy.
    members = [
        m for m in zf.infolist()
        if not m.is_dir()
        and not m.filename.startswith("__MACOSX/")
        and not os.path.basename(m.filename).startswith(".")
    ]
    if len(members) > 200:
        raise HTTPException(
            status_code=413,
            detail=f"{len(members)} files in archive; limit is 200 per zip",
        )

    # Resolve after archive validation so a bad zip cannot latch the floor,
    # and a below-floor declaration 400s before any member documents exist.
    classification_level_value, classification_source = (
        _resolve_document_upload_classification(
            db,
            collection_id=collection_id,
            declared_raw=classification_level,
            actor=current_user,
        )
    )
    classified_at = datetime.now(timezone.utc)
    # Persist an upward latch before per-member commit/rollback cycles can
    # undo ClassificationEvent rows written with commit=False.
    if classification_source == "uploader_declared":
        db.commit()

    results: list[ZipUploadResult] = []
    enqueued = duplicates = skipped = errors = 0
    # 累積解壓資料量；超過 _ZIP_MAX_TOTAL_BYTES 後續成員一律 skipped。
    cumulative_bytes = 0

    for member in members:
        # Choose the document filename based on preserve_folder_structure.
        # 先還原檔名編碼(zip 內非 UTF-8 旗標的中文檔名會被 CP437 解成亂碼)。
        in_zip_path = _zip_member_name(member)
        out_name = _sanitize_archive_filename(
            in_zip_path,
            preserve_folder_structure=preserve_folder_structure,
        )

        declared_error = _declared_zip_member_error(
            member,
            out_name,
            cumulative_bytes=cumulative_bytes,
        )
        if declared_error is not None:
            skipped += 1
            results.append(declared_error)
            continue

        # zlib / sniff / sha256 / disk write off the event loop. SQLAlchemy
        # Session is not thread-safe — DB work stays below on the loop thread.
        staged = await asyncio.to_thread(
            _stage_zip_member,
            zf,
            member,
            out_name=out_name,
            in_zip_path=in_zip_path,
            remaining_budget=_ZIP_MAX_TOTAL_BYTES - cumulative_bytes,
        )
        cumulative_bytes += staged.cumulative_delta

        if staged.outcome == "unzip_error":
            errors += 1
            results.append(ZipUploadResult(
                filename=staged.result_filename, status="error",
                detail=staged.detail,
            ))
            continue
        if staged.outcome == "empty":
            skipped += 1
            results.append(ZipUploadResult(
                filename=staged.result_filename, status="skipped",
                detail=staged.detail,
            ))
            continue
        if staged.outcome == "too_large":
            skipped += 1
            results.append(ZipUploadResult(
                filename=staged.result_filename, status="too_large",
                detail=staged.detail,
            ))
            continue
        if staged.outcome == "over_total":
            skipped += 1
            results.append(ZipUploadResult(
                filename=staged.result_filename, status="skipped",
                detail=staged.detail,
            ))
            continue
        if staged.outcome == "content_error":
            errors += 1
            results.append(ZipUploadResult(
                filename=staged.result_filename, status="error",
                detail=staged.detail,
            ))
            continue

        # staged.outcome == "ready"
        sha256 = staged.sha256
        storage_path = staged.storage_path
        size = staged.size
        assert sha256 is not None and storage_path is not None

        # Check for duplicate (same sha within collection).
        existing = (
            db.query(IngestionDocument)
            .filter(
                IngestionDocument.collection_id == collection_id,
                IngestionDocument.sha256 == sha256,
            )
            .first()
        )
        if existing is not None:
            duplicates += 1
            results.append(ZipUploadResult(
                filename=staged.result_filename, status="duplicate",
                document_id=existing.id,
                detail="same sha already in collection",
            ))
            continue

        member_title, member_norm_title = _derive_title(staged.result_filename)
        doc = IngestionDocument(
            collection_id=collection_id,
            filename=staged.result_filename,
            title=member_title,
            normalized_title=member_norm_title,
            sha256=sha256,
            mime_type=staged.media_type,
            bytes=size,
            storage_path=storage_path,
            status="pending",
            chunk_count=0,
            uploaded_by=current_user.id,
            classification_level=classification_level_value,
            classification_latched_at=classified_at,
            classification_source=classification_source,
            archive_due_at=datetime.now(timezone.utc) + timedelta(
                days=settings.RETENTION_INGESTION_ACTIVE_DAYS
            ),
        )
        db.add(doc)
        try:
            job = create_ingestion_dispatch(
                db,
                document=doc,
                enqueued_by=current_user.id,
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            duplicates += 1
            results.append(ZipUploadResult(
                filename=staged.result_filename, status="duplicate",
                detail="raced with concurrent upload",
            ))
            continue
        db.refresh(doc)

        enqueued += 1
        results.append(ZipUploadResult(
            filename=staged.result_filename, status="enqueued",
            document_id=doc.id, arq_job_id=job.arq_job_id,
        ))

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_document_upload_zip",
        resource_type="ingestion_collection",
        resource_id=collection_id,
        metadata={
            "files_in_archive": len(members),
            "enqueued": enqueued, "duplicates": duplicates,
            "skipped": skipped, "errors": errors,
            "classification_level": classification_level_value,
            "classification_source": classification_source,
        },
    )
    return ZipUploadResponse(
        files_in_archive=len(members),
        enqueued=enqueued,
        duplicates=duplicates,
        skipped=skipped,
        errors=errors,
        results=results,
    )


@router.get(
    "/collections/{collection_id}/documents",
    response_model=list[DocumentResponse],
)
def list_documents(
    collection_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DocumentResponse]:
    _resolve_collection(db, current_user, collection_id)
    rows = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.collection_id == collection_id)
        .order_by(IngestionDocument.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [DocumentResponse.model_validate(r) for r in rows]


@router.get(
    "/documents/{document_id}",
    response_model=DocumentDetailResponse,
)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentDetailResponse:
    """Document row + most recent job row joined.

    The inspector polls this endpoint to render the parse → chunk →
    embed → indexed timeline. We always fetch the *latest* job because
    a re-ingest creates a new row; the older ones stay for audit but
    aren't UI-relevant.
    """
    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    coll = _resolve_collection(db, current_user, doc.collection_id)  # auth + 404
    _ = coll  # only invoked for its side-effect (auth check).

    latest_job = (
        db.query(IngestionJob)
        .filter(IngestionJob.document_id == document_id)
        .order_by(IngestionJob.id.desc())
        .first()
    )

    payload = DocumentDetailResponse.model_validate(doc)
    if latest_job is not None:
        payload.latest_job_id = latest_job.id
        payload.latest_job_status = latest_job.status
        payload.latest_job_error_code = latest_job.error_code
        payload.arq_job_id = latest_job.arq_job_id
    return payload


@router.delete(
    "/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Archive a document; the leased retention reaper performs erasure."""
    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    # Auth: must be admin or own the parent collection.
    _resolve_collection(db, current_user, doc.collection_id)

    active_job = db.query(IngestionJob.id).filter(
        IngestionJob.document_id == doc.id,
        IngestionJob.status.notin_(("succeeded", "failed", "cancelled", "dead_letter")),
    ).first()
    if active_job is not None:
        raise HTTPException(status_code=409, detail="Document has nonterminal ingestion work")

    snapshot = {
        "filename": doc.filename,
        "sha256": doc.sha256,
        "collection_id": doc.collection_id,
        "bytes": doc.bytes,
    }
    if doc.lifecycle_state == "erased":
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    now = datetime.now(timezone.utc)
    doc.lifecycle_state = "archived"
    doc.archived_at = doc.archived_at or now
    doc.erase_due_at = now + timedelta(days=settings.RETENTION_INGESTION_ARCHIVE_DAYS)
    doc.availability_status = "unavailable"
    db.commit()

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_document_archive",
        resource_type="ingestion_document",
        resource_id=document_id,
        metadata=snapshot,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Inspector endpoints (Sprint 2 Chunk H) ──────────────────────────────────


class ChunkRow(ApiResponseModel):
    """Inspector-facing chunk row.

    Embedding is omitted by default because the inspector list view
    doesn't render the 4000-d vector. Vector debug info goes through
    the dedicated ``/embedding-debug`` endpoint behind a UI toggle.
    """

    id: int
    chunk_key: str
    content: str
    metadata: dict
    token_count: int | None
    created_at: datetime
    # Sprint 9 X / parent-child RAG (migration 0028). All optional;
    # legacy rows that predate the new chunker default to
    # ``chunk_type='leaf'`` / ``parent_chunk_id=None`` so existing UIs
    # see no behaviour change unless they look at the new fields.
    parent_chunk_id: int | None = None
    chunk_type: str = "leaf"
    chunk_level: int = 0


class ChunkEmbeddingDebug(BaseModel):
    """Vector-debug summary for a single chunk.

    Only ``dim`` and ``norm`` are returned — never the full vector.
    Bandwidth: 30 bytes per chunk vs ~16 KB if the raw embedding shipped.
    Useful to confirm chunks were actually embedded (norm ≈ 1 means
    L2-normalised; embedding pipelines that drop normalisation surface
    here as norm ≠ 1).
    """

    chunk_id: int
    dim: int
    norm: float


@router.get(
    "/documents/{document_id}/chunks",
    response_model=list[ChunkRow],
)
async def list_document_chunks(
    document_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ChunkRow]:
    """Inspector chunk-list — agent-scoped via AgentScopedPgVectorStore.

    Goes through the central SDK so RLS auto-filters even if the API
    layer's own auth check (``_resolve_collection``) is buggy. Belt-
    and-suspenders security.
    """
    from app.services.ingestion_pool import get_pool

    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    coll = _resolve_collection(db, current_user, doc.collection_id)
    _require_document_data_clearance(db, user=current_user, document=doc)

    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    store = CollectionScopedPgVectorStore(pool, collection_id=coll.id)
    chunks = await store.list_by_document(
        document_id=document_id,
        limit=limit,
        offset=offset,
        include_embedding=False,
    )
    return [
        ChunkRow(
            id=c.id,
            chunk_key=c.chunk_key,
            content=c.content,
            metadata=c.metadata or {},
            token_count=c.token_count,
            created_at=c.created_at,
            parent_chunk_id=getattr(c, "parent_chunk_id", None),
            chunk_type=getattr(c, "chunk_type", "leaf") or "leaf",
            chunk_level=getattr(c, "chunk_level", 0) or 0,
        )
        for c in chunks
    ]


@router.get(
    "/documents/{document_id}/chunks/{chunk_id}/embedding-debug",
    response_model=ChunkEmbeddingDebug,
)
async def get_chunk_embedding_debug(
    document_id: int,
    chunk_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChunkEmbeddingDebug:
    """Return ``(dim, L2 norm)`` for one chunk — the "Show vector debug"
    payload behind the Inspector toggle.

    The route is keyed on ``(document_id, chunk_id)`` rather than
    ``chunk_id`` alone because the Layer 2 RLS policy default-denies
    every ``document_chunks`` row when no GUC is set, which means we
    can't look up the owning agent_id directly from the chunks table.
    Resolving via ``ingestion_documents`` → ``ingestion_collections``
    (regular non-RLS tables) is the clean way through. The frontend
    always has both ids in hand from the chunks-list endpoint anyway.

    Wire payload is 2 scalars (~30 bytes); the full halfvec stays
    server-side. Used behind the inspector's "Show vector debug"
    toggle so the page render isn't paying for it by default.
    """
    import math

    from app.services.ingestion_pool import get_pool

    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    coll = _resolve_collection(db, current_user, doc.collection_id)
    _require_document_data_clearance(db, user=current_user, document=doc)

    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    store = CollectionScopedPgVectorStore(pool, collection_id=coll.id)
    async with store._acquire() as conn:  # noqa: SLF001
        row = await conn.fetchrow(
            """
            SELECT id, embedding
             FROM document_chunks
             WHERE id = $1 AND document_id = $2
               AND is_active_generation = true
            """,
            chunk_id,
            document_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    emb = row["embedding"]
    # ``HalfVector`` from pgvector exposes ``.to_list()``; raw lists
    # iterate directly. Both shapes appear depending on codec version.
    components = list(emb.to_list()) if hasattr(emb, "to_list") else list(emb)
    norm = math.sqrt(sum(c * c for c in components)) if components else 0.0
    return ChunkEmbeddingDebug(
        chunk_id=int(row["id"]),
        dim=len(components),
        norm=norm,
    )


@router.get(
    "/documents/{document_id}/blob",
    response_class=FileResponse,
    responses={
        200: {
            "description": "Raw uploaded file bytes",
            "content": {"application/octet-stream": {}},
        }
    },
)
def download_document_blob(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    """Stream the raw uploaded file back to the inspector.

    Auth: standard collection-scope check. The blob lives at
    ``UPLOAD_DIR/<sha[:2]>/<sha>`` — we don't do path traversal because
    we read storage_path from the DB row, not from a query string.
    """
    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    _resolve_collection(db, current_user, doc.collection_id)  # auth check
    _require_document_data_clearance(db, user=current_user, document=doc)
    if doc.lifecycle_state != "active":
        raise HTTPException(status_code=410, detail="Document 已封存或清除")
    try:
        blob_path = safe_ingestion_path(_UPLOAD_DIR, doc.storage_path or "")
    except RetentionSafetyError:
        raise HTTPException(status_code=410, detail="Blob storage path 無效") from None
    if not blob_path.is_file():
        raise HTTPException(status_code=410, detail="Blob no longer on disk")
    return FileResponse(
        path=blob_path,
        media_type=doc.mime_type or "application/octet-stream",
        filename=doc.filename,
    )
