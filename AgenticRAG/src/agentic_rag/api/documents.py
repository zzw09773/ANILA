"""Document management REST API endpoints.

Endpoints:
    POST   /documents/upload         Upload a file (multipart/form-data)
    POST   /documents/ingest         Trigger ingestion of an uploaded file
    GET    /documents                List indexed documents
    GET    /documents/{document_id}  Document detail + chunk list
    DELETE /documents/{document_id}  Delete document and its vectors
    GET    /documents/{document_id}/status  Ingestion status
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(prefix="/documents", tags=["documents"])

# In-memory ingestion status store (document_id → status dict)
_STATUS: dict[str, dict[str, Any]] = {}


# ------------------------------------------------------------------
# Request / Response models
# ------------------------------------------------------------------

class IngestRequest(BaseModel):
    file_path: str
    document_id: Optional[str] = None
    user_id: str = "default"
    project_id: str = "default"
    metadata: dict = {}


class IngestResponse(BaseModel):
    document_id: str
    status: str
    message: str


class DocumentListItem(BaseModel):
    document_id: str
    title: str
    format: str
    chunk_count: int
    source_path: str


class DocumentDetail(BaseModel):
    document_id: str
    title: str
    format: str
    source_path: str
    chunks: list[dict]


# ------------------------------------------------------------------
# Dependency: ingestion service (injected at app startup)
# ------------------------------------------------------------------

_ingestion_service: Any = None
_document_store: Any = None
_upload_dir: str = "/tmp/agentic_rag_uploads"


def set_ingestion_service(service: Any, doc_store: Any, upload_dir: str) -> None:
    """Called from app factory to inject dependencies."""
    global _ingestion_service, _document_store, _upload_dir
    _ingestion_service = service
    _document_store = doc_store
    _upload_dir = upload_dir


# ------------------------------------------------------------------
# Endpoints
# ------------------------------------------------------------------

@router.post("/upload", summary="Upload a document file")
async def upload_document(
    file: UploadFile = File(...),
    user_id: str = "default",
    project_id: str = "default",
) -> JSONResponse:
    """Accept a multipart file upload and save it to the upload directory."""
    ext = Path(file.filename or "unknown").suffix.lower()
    supported = {
        ".txt", ".md", ".rtf", ".pdf", ".docx", ".doc", ".odt",
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    }
    if ext not in supported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Supported: {sorted(supported)}",
        )

    os.makedirs(_upload_dir, exist_ok=True)
    doc_id = str(uuid.uuid4())
    dest = Path(_upload_dir) / f"{doc_id}{ext}"

    content = await file.read()
    dest.write_bytes(content)

    return JSONResponse(
        status_code=200,
        content={
            "document_id": doc_id,
            "file_path": str(dest),
            "filename": file.filename,
            "size_bytes": len(content),
        },
    )


@router.post("/ingest", response_model=IngestResponse, summary="Ingest a document")
async def ingest_document(
    req: IngestRequest,
    background_tasks: BackgroundTasks,
) -> IngestResponse:
    """Trigger asynchronous ingestion of a document file."""
    if _ingestion_service is None:
        raise HTTPException(status_code=503, detail="Ingestion service not available")

    doc_id = req.document_id or str(uuid.uuid4())
    _STATUS[doc_id] = {
        "status": "pending",
        "progress": 0,
        "stage": "queued",
        "_user_id": req.user_id,
        "_project_id": req.project_id,
    }

    background_tasks.add_task(
        _run_ingestion,
        doc_id=doc_id,
        file_path=req.file_path,
        user_id=req.user_id,
        project_id=req.project_id,
        metadata=req.metadata,
    )

    return IngestResponse(
        document_id=doc_id,
        status="accepted",
        message="Ingestion started in background",
    )


async def _run_ingestion(
    doc_id: str,
    file_path: str,
    user_id: str,
    project_id: str,
    metadata: dict,
) -> None:
    """Background ingestion task."""
    _scope = {"_user_id": user_id, "_project_id": project_id}
    _STATUS[doc_id] = {"status": "running", "progress": 0, "stage": "starting", **_scope}

    def on_progress(current: int, total: int, stage: str) -> None:
        _STATUS[doc_id] = {
            "status": "running",
            "progress": round(current / max(total, 1) * 100),
            "stage": stage,
            **_scope,
        }

    try:
        await _ingestion_service.ingest(
            file_path=file_path,
            user_id=user_id,
            project_id=project_id,
            document_id=doc_id,
            metadata=metadata,
            on_progress=on_progress,
        )
        _STATUS[doc_id] = {"status": "completed", "progress": 100, "stage": "done", **_scope}
    except Exception as exc:
        _STATUS[doc_id] = {
            "status": "failed",
            "progress": 0,
            "stage": "error",
            "error": str(exc),
            **_scope,
        }


@router.get("", summary="List indexed documents")
async def list_documents(
    user_id: str = "default",
    project_id: str = "default",
) -> JSONResponse:
    """Return documents scoped to user_id + project_id (survives container restart)."""
    if _document_store is None:
        raise HTTPException(status_code=503, detail="Document store not available")

    rows = await _document_store.list_all_documents(user_id=user_id, project_id=project_id)
    docs = []
    for r in rows:
        source = r.get("source_path") or ""
        filename = r.get("filename") or ""
        # 顯示用名稱：優先用 filename，其次取 source_path 最後一段
        display_name = filename or (source.rsplit("/", 1)[-1] if source else r["document_id"])
        docs.append({
            "document_id": r["document_id"],
            "title":       display_name,
            "format":      "unknown",
            "chunk_count": r["chunk_count"],
            "source_path": source,
            "filename":    filename,
            "last_indexed": r["last_indexed"].isoformat() if r.get("last_indexed") else None,
        })

    return JSONResponse(content={"documents": docs, "total": len(docs)})


@router.get("/{document_id}/status", summary="Get ingestion status")
async def get_document_status(
    document_id: str,
    user_id: str = "default",
    project_id: str = "default",
) -> JSONResponse:
    """Return the current ingestion status, scoped to user_id + project_id."""
    status = _STATUS.get(document_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Document not found")

    # Verify the caller owns this document's ingestion
    if status.get("_user_id") != user_id or status.get("_project_id") != project_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Strip internal scope fields before returning
    public = {k: v for k, v in status.items() if not k.startswith("_")}
    return JSONResponse(content={"document_id": document_id, **public})


@router.get("/{document_id}", summary="Get document details")
async def get_document(
    document_id: str,
    user_id: str = "default",
    project_id: str = "default",
) -> JSONResponse:
    """Return document metadata and its chunk list, scoped to user_id + project_id."""
    if _document_store is None:
        raise HTTPException(status_code=503, detail="Document store not available")

    chunks = await _document_store.list_by_document(
        document_id, user_id=user_id, project_id=project_id
    )
    if not chunks:
        raise HTTPException(status_code=404, detail="Document not found")

    meta = chunks[0].metadata
    return JSONResponse(content={
        "document_id": document_id,
        "title": meta.get("title", document_id),
        "format": meta.get("format", "unknown"),
        "source_path": meta.get("source_path", ""),
        "chunk_count": len(chunks),
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "content_preview": c.content[:200],
                "metadata": {k: v for k, v in c.metadata.items() if k != "embedding"},
            }
            for c in chunks
        ],
    })


@router.delete("/{document_id}", summary="Delete a document")
async def delete_document(
    document_id: str,
    user_id: str = "default",
    project_id: str = "default",
) -> JSONResponse:
    """Delete a document's chunks scoped to user_id + project_id."""
    if _ingestion_service is None:
        raise HTTPException(status_code=503, detail="Ingestion service not available")

    await _ingestion_service.delete(document_id, user_id=user_id, project_id=project_id)
    _STATUS.pop(document_id, None)

    return JSONResponse(content={"document_id": document_id, "deleted": True})
