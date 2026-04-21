import secrets
import hashlib
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.agent import ApiKeyAgentPermission, UserAgentPermission
from app.models.model_registry import ModelRegistry


def generate_api_key() -> tuple[str, str, str, str]:
    """Generate API key. Returns (full_key, prefix, suffix, hash)."""
    raw_key = "sk-" + secrets.token_urlsafe(48)
    prefix = raw_key[:8]
    suffix = raw_key[-4:]
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    return raw_key, prefix, suffix, key_hash


def create_api_key(
    db: Session,
    user_id: int,
    name: str,
    model_ids: list[int],
    expires_at: datetime | None = None,
) -> tuple[ApiKey, str]:
    """Create a new API key. Returns (api_key_obj, full_key)."""
    full_key, prefix, suffix, key_hash = generate_api_key()

    api_key = ApiKey(
        user_id=user_id,
        name=name,
        key_prefix=prefix,
        key_suffix=suffix,
        key_hash=key_hash,
        expires_at=expires_at,
    )
    db.add(api_key)
    db.flush()

    # Add model permissions
    for model_id in model_ids:
        perm = ApiKeyModelPermission(api_key_id=api_key.id, model_id=model_id)
        db.add(perm)

    db.commit()
    db.refresh(api_key)
    return api_key, full_key


def validate_api_key(db: Session, raw_key: str) -> ApiKey | None:
    """Validate an API key and return the ApiKey object if valid."""
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()

    if not api_key:
        return None
    if not api_key.is_active:
        return None
    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
        return None

    # Update last_used_at
    api_key.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return api_key


def check_model_permission(db: Session, api_key_id: int, model_id: int) -> bool:
    """Check if an API key has permission to use a model."""
    perm = (
        db.query(ApiKeyModelPermission)
        .filter(
            ApiKeyModelPermission.api_key_id == api_key_id,
            ApiKeyModelPermission.model_id == model_id,
        )
        .first()
    )
    return perm is not None


def check_agent_permission(db: Session, api_key_id: int, agent_id: int) -> bool:
    """Check if an API key's owner has permission to call an agent.

    Permission is granted via api_key_agent_permissions or inherited from
    user_agent_permissions. Admin users always pass.
    """
    api_key = db.query(ApiKey).filter(ApiKey.id == api_key_id).first()
    if not api_key:
        return False
    user = api_key.user
    if not user:
        return False
    if user.role == "admin":
        return True
    direct_perm = (
        db.query(ApiKeyAgentPermission)
        .filter(
            ApiKeyAgentPermission.api_key_id == api_key_id,
            ApiKeyAgentPermission.agent_id == agent_id,
        )
        .first()
    )
    if direct_perm is not None:
        return True
    perm = (
        db.query(UserAgentPermission)
        .filter(
            UserAgentPermission.user_id == user.id,
            UserAgentPermission.agent_id == agent_id,
        )
        .first()
    )
    return perm is not None
