"""對話選單授權的值域檢查。管理 API 與設定匯入共用。"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.model_access_group import ModelAccessGroup
from app.models.user import User
from app.schemas.router_model import RouterGrantIn


def _validate_grant(db: Session, grant: RouterGrantIn) -> None:
    if grant.scope_type == "all":
        if grant.department_id or grant.group_id or grant.user_id:
            raise HTTPException(status_code=400, detail="全院授權不可帶部門／群組／使用者")
        return
    if grant.scope_type == "department":
        if grant.department_id is None or grant.group_id or grant.user_id:
            raise HTTPException(status_code=400, detail="部門授權必須且只能指定部門")
        if db.get(Department, grant.department_id) is None:
            raise HTTPException(status_code=404, detail="部門不存在")
        return
    if grant.scope_type == "group":
        if grant.group_id is None or grant.department_id or grant.user_id:
            raise HTTPException(status_code=400, detail="群組授權必須且只能指定群組")
        if db.get(ModelAccessGroup, grant.group_id) is None:
            raise HTTPException(status_code=404, detail="群組不存在")
        return
    if grant.user_id is None or grant.department_id or grant.group_id:
        raise HTTPException(status_code=400, detail="個別授權必須且只能指定使用者")
    if db.get(User, grant.user_id) is None:
        raise HTTPException(status_code=404, detail="使用者不存在")
