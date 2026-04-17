from fastapi import Depends, HTTPException, status, Header
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.api_key import ApiKey
from app.services.api_key_service import validate_api_key


def get_api_key(
    authorization: str = Header(...),
    db: Session = Depends(get_db),
) -> ApiKey:
    """Extract and validate API key from Authorization header (Bearer sk-...)."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效的授權標頭格式，請使用 Bearer <API_KEY>",
        )
    raw_key = authorization[7:]
    api_key = validate_api_key(db, raw_key)
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="無效或已過期的 API Key",
        )
    return api_key
