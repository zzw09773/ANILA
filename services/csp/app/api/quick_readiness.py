"""管理員可讀的本機文件解析與離線 OCR 診斷。"""

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from anila_core.ingestion.readiness import diagnose_local_ingestion
from app.models.user import User
from app.schemas.quick_builder import QuickBuilderRoute
from app.services.auth_service import require_admin

router = APIRouter(
    prefix="/api/quick-builder",
    tags=["助手快製"],
    route_class=QuickBuilderRoute,
)


@router.get("/readiness")
async def readiness(request: Request, user: User = Depends(require_admin)):
    def invalid_input():
        raise HTTPException(
            422,
            detail={
                "type": "READINESS_INPUT_UNSUPPORTED",
                "message": "本機診斷只檢查固定項目，不接受工具、指令或路徑參數。",
            },
        )

    if request.query_params or request.headers.get("transfer-encoding"):
        invalid_input()
    if request.headers.get("content-length", "0") != "0":
        invalid_input()
    async for chunk in request.stream():
        if chunk:
            invalid_input()
    report = await run_in_threadpool(diagnose_local_ingestion)
    return {
        **report,
        "service": "csp",
        "message": "僅診斷目前 CSP 處理程序的本機依賴；未檢查 ingestion worker、遠端 Docling、正式映像或實際文件品質。",
    }
