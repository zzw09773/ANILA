import secrets
import hashlib
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.agent import ApiKeyAgentPermission, UserAgentPermission
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.time_utils import is_expired
from app.services.auth_service import is_admin_tier


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
    """Validate an API key and return the ApiKey object if valid.

    Defense-in-depth: also rejects when the owning user is inactive.
    Without this gate, deactivating a user via admin UI would invalidate
    their JWTs (via token_version bump) but leave any ``sk-*`` API keys
    fully working — meaning they could keep calling ``/v1/*`` until an
    admin manually disabled every key. Tying validity to ``user.is_active``
    closes that gap even if a future deactivate path forgets to disable
    the keys explicitly.
    """
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()

    if not api_key:
        return None
    if not api_key.is_active:
        return None
    # ⚠ 這裡**不能**直接寫 `api_key.expires_at < datetime.now(timezone.utc)`。
    #
    # `expires_at` 是 naive(`models/api_key.py:28`,由 migration r1_0017 建成
    # `sa.DateTime()`,而同表的 `created_at`/`last_used_at` 在 0001 就是
    # `DateTime(timezone=True)`),拿 naive 跟 aware 比較會直接
    # `TypeError: can't compare offset-naive and offset-aware datetimes`
    # —— 也就是**任何設了到期日的 API key,每一次驗證都是 500**,既不是正常
    # 運作也不是正常過期。而這條路徑先前有 0 個測試。
    #
    # `is_expired()` 內部負責正規化,呼叫端不需要記得補 tzinfo —— 那正是這個
    # bug 的成因(codebase 有 30 個各自手刻的 _as_utc,補丁漏掉的地方就是 bug)。
    if is_expired(api_key.expires_at):
        return None
    if api_key.user is None or not api_key.user.is_active:
        return None

    # Update last_used_at
    api_key.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return api_key


def check_model_permission(
    db: Session,
    *,
    user: User,
    api_key_id: int | None,
    model_id: int,
) -> bool:
    """Check caller permission to use a model.

    Admin-designated "router primary" models are open to every active user,
    so both auth paths (JWT and API key) accept them without a per-row
    permission entry. For all other models:

    - API key path (``api_key_id`` given): require an ``ApiKeyModelPermission``
      row for that key. This preserves the existing per-key scoping.
    - JWT / cookie path (``api_key_id is None``): fall back to the user's
      ``allowed_models`` relationship — same set an admin granted for the
      user account. The SPA uses this path exclusively.

    Admin users pass any model. The SPA never impersonates API keys, so we
    never silently widen an API key's scope via the user fallback.
    """
    model = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.id == model_id, ModelRegistry.is_active.is_(True))
        .first()
    )
    if model is None:
        return False
    if getattr(model, "is_router_primary", False):
        return True
    if is_admin_tier(user):
        return True

    if api_key_id is not None:
        perm = (
            db.query(ApiKeyModelPermission)
            .filter(
                ApiKeyModelPermission.api_key_id == api_key_id,
                ApiKeyModelPermission.model_id == model_id,
            )
            .first()
        )
        return perm is not None

    return any(m.id == model_id for m in user.allowed_models)


def check_agent_permission(
    db: Session,
    *,
    user: User,
    api_key_id: int | None,
    agent_id: int,
) -> bool:
    """Check caller permission to invoke an agent.

    API key path checks ``ApiKeyAgentPermission`` then falls back to
    ``UserAgentPermission`` (preserves existing behavior). JWT / cookie path
    checks ``UserAgentPermission`` directly. admin / owner always pass.
    """
    if is_admin_tier(user):
        return True

    if api_key_id is not None:
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
