"""快製平台 HTTP 輸入契約。"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi import Depends, HTTPException, Request
from app.database import get_db


def ensure_operation_identity(request: Request, db=Depends(get_db)):
    expected = request.headers.get("X-ANILA-Actor-Id")
    if request.method in ("GET", "HEAD", "OPTIONS") or expected is None:
        return
    from app.middleware.caller import get_caller

    user = get_caller(request, db).user
    if not expected.isdecimal() or int(expected) != user.id:
        raise HTTPException(
            status_code=409,
            detail={
                "type": "IDENTITY_CHANGED",
                "message": "登入身分已變更，舊草稿尚未送出；請重新載入並確認身分",
            },
        )


class QuickBuilderRoute(APIRoute):
    def __init__(self, *args, **kwargs):
        kwargs["dependencies"] = [
            *(kwargs.get("dependencies") or []),
            Depends(ensure_operation_identity),
        ]
        super().__init__(*args, **kwargs)

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def localized(request):
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": {
                            "type": "INVALID_INPUT",
                            "message": "欄位格式不正確，請確認必填內容與選項後重試",
                            "fields": [
                                {
                                    "field": ".".join(map(str, error["loc"])),
                                    "type": error["type"],
                                }
                                for error in exc.errors()
                            ],
                        }
                    },
                )

        return localized


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantWrite(StrictInput):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=200)
    graph: dict
    model_id: int | None = Field(default=None, gt=0)
    classification_level: Literal["無機密", "營業秘密", "密", "機密"] = "無機密"
    collection_ids: list[int] = Field(default_factory=list, max_length=30)
    scope: Literal["unit", "public", "departments"] = "unit"
    target_department_ids: list[int] = Field(default_factory=list, max_length=100)


class VersionWrite(AssistantWrite):
    expected_revision: int = Field(ge=1)


class VersionRef(StrictInput):
    version_id: int = Field(gt=0)


class Restore(VersionRef):
    expected_revision: int = Field(ge=1)


class Review(VersionRef):
    approve: bool
    reason: str = Field(default="", max_length=1000)


class TestInput(VersionRef):
    message: str = Field(min_length=1, max_length=16000)
    request_id: UUID | None = None


class TemplateWrite(StrictInput):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    graph: dict
