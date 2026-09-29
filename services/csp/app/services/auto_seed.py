"""建立管理員、平台入口模型，以及開發用的 seed 帳號。

模型與 agent 只在治理中心登錄。開機不讀 AUTO_REGISTER_MODELS /
AUTO_REGISTER_AGENTS，也不再從 MODEL_* 環境變數組端點。
"""
import json
import logging
import os
import hashlib

from app.config import settings
from app.database import SessionLocal
from app.models.agent import Agent, UserAgentPermission
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.utils.security import hash_password

logger = logging.getLogger(__name__)
ADMIN_USERNAME = "admin"
# Shell default target. Not a GPU model — CSP proxies this name to the
# router service. Seeded on every boot so enable never depends on a
# manual Models page row (the 404 is ``模型 'anila-router' 未註冊``).
PLATFORM_ROUTER_NAME = "anila-router"
PLATFORM_ROUTER_ENDPOINT = "http://router:9000"


def _platform_router_endpoint() -> str:
    return (os.environ.get("ANILA_ROUTER_INTERNAL_URL") or "").strip() or PLATFORM_ROUTER_ENDPOINT


def ensure_platform_router_model(db) -> ModelRegistry:
    """Create or reactivate the ``anila-router`` catalog row.

    Env creates the row once. A later admin edit of ``endpoint_url`` is
    kept (OE-2 B3). Deactivating or clearing ``is_internal`` is not kept
    — this name is the platform chat entry, not an optional LLM.
    """
    existing = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == PLATFORM_ROUTER_NAME)
        .first()
    )
    if existing is None:
        row = ModelRegistry(
            name=PLATFORM_ROUTER_NAME,
            display_name="ANILA 自動選助手",
            model_type="llm",
            endpoint_url=_platform_router_endpoint(),
            api_version="v1",
            description="Platform router alias. Requests go to the router service.",
            is_active=True,
            is_internal=True,
            is_router_primary=False,
        )
        db.add(row)
        db.flush()
        logger.info(
            "自動註冊平台入口模型: %s -> %s",
            PLATFORM_ROUTER_NAME,
            row.endpoint_url,
        )
        return row
    restored = False
    if not existing.is_active:
        existing.is_active = True
        restored = True
    if not existing.is_internal:
        existing.is_internal = True
        restored = True
    if restored:
        logger.info("平台入口模型 %s 已恢復為啟用", PLATFORM_ROUTER_NAME)
    db.flush()
    return existing


def seed_model_skip_reason(model_name: str, inactive_names: set[str]) -> str:
    """Why a seeded API key could not be granted ``model_name``.

    Seeding only selects active models, so a deactivated one lands in the same
    "cannot grant" branch as one that was never registered. Saying "未註冊" for
    both would send the next reader looking for a registration problem that does
    not exist — this filter is what made the branch reachable, so telling the two
    apart is part of that change, not an extra.
    """
    return "已停用" if model_name in inactive_names else "未註冊"


def auto_seed():
    """Run on startup: create admin, the platform router row, and dev keys."""
    db = SessionLocal()
    try:
        # 1. Ensure admin user exists.
        #
        # First-time bootstrap: seeded ADMIN_USERNAME is the platform
        # operator → role="owner" so they can use require_owner-gated
        # endpoints (purge user, edit raw audit fields, etc.) without
        # needing a second admin to promote them. Without this, the
        # whole owner tier is unreachable on a fresh deploy.
        #
        # Existing installs are NOT touched: the `if not admin` guard
        # means deployments where admin already exists keep their
        # current role; live stack admins stay at admin tier and can
        # be promoted manually if/when needed.
        admin = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if not admin:
            admin = User(
                username=ADMIN_USERNAME,
                hashed_password=hash_password(settings.ADMIN_PASSWORD),
                role="owner",
                is_active=True,
            )
            db.add(admin)
            db.flush()
            logger.info(f"已建立 owner 帳號: {ADMIN_USERNAME}")

        # 模型與 agent 只在治理中心登錄。這裡不讀環境變數。

        # 4. Auto-seed users + API keys from AUTO_SEED_API_KEYS env
        if settings.AUTO_SEED_API_KEYS:
            try:
                keys_config = json.loads(settings.AUTO_SEED_API_KEYS)
                # Selection is active-only: seeding must not wire a key to a
                # model an operator switched off. The inactive names are kept
                # solely so the warning below can say *which* of the two
                # reasons applied — before this filter existed a deactivated
                # model resolved fine and never reached that branch, so the
                # message only has to tell them apart because we changed this.
                model_rows = db.query(ModelRegistry).all()
                model_id_by_name = {
                    model.name: model.id for model in model_rows if model.is_active
                }
                inactive_model_names = {
                    model.name for model in model_rows if not model.is_active
                }
                agent_id_by_name = {
                    agent.name: agent.id
                    for agent in db.query(Agent).all()
                }

                for item in keys_config:
                    username = item["username"]
                    user = db.query(User).filter(User.username == username).first()
                    requested_role = item.get(
                        "role", user.role if user is not None else "user"
                    )
                    deputy_locked = requested_role == "deputy" or (
                        user is not None and user.role == "deputy"
                    )
                    if user is None and requested_role == "deputy":
                        logger.error(
                            "seed 不建立代理管理員 %s：略過角色指派，沒有帳號所以其餘設定也不套用",
                            username,
                        )
                        continue
                    if user is None:
                        seed_pw = item.get("password")
                        if not seed_pw:
                            # Fail closed: never silently default to a known
                            # password ("changeme"). Mint a random one the
                            # operator must reset out-of-band — log that it
                            # happened, never the value.
                            import secrets as _secrets

                            seed_pw = _secrets.token_urlsafe(24)
                            logger.warning(
                                "seed 使用者 %s 未提供密碼，已產生隨機密碼 — "
                                "請於使用前重設",
                                username,
                            )
                        user = User(
                            username=username,
                            email=item.get("email"),
                            hashed_password=hash_password(seed_pw),
                            role=item.get("role", "user"),
                            is_active=True,
                            is_approved=True,
                        )
                        db.add(user)
                        db.flush()
                        logger.info(f"已建立 seed 使用者: {username}")
                    else:
                        if item.get("email"):
                            user.email = item["email"]
                        if deputy_locked:
                            logger.error(
                                "seed 略過 %s 的代理角色指派與重新啟用，其餘設定仍套用",
                                username,
                            )
                        else:
                            user.role = item.get("role", user.role)
                            user.is_active = True
                            user.is_approved = True

                    raw_key = item["key"]
                    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
                    api_key = db.query(ApiKey).filter(ApiKey.key_hash == key_hash).first()
                    if api_key is None:
                        api_key = ApiKey(
                            user_id=user.id,
                            name=item.get("key_name", f"{username}-seed-key"),
                            key_prefix=raw_key[:8],
                            key_suffix=raw_key[-4:],
                            key_hash=key_hash,
                            is_active=True,
                        )
                        db.add(api_key)
                        db.flush()
                        logger.info(f"已建立 seed API key: {api_key.name}")
                    else:
                        api_key.user_id = user.id
                        api_key.name = item.get("key_name", api_key.name)
                        api_key.is_active = True

                    for model_name in item.get("models", []):
                        model_id = model_id_by_name.get(model_name)
                        if model_id is None:
                            reason = seed_model_skip_reason(
                                model_name, inactive_model_names
                            )
                            logger.warning(
                                f"Seed API key {username}: model '{model_name}' {reason}"
                            )
                            continue
                        exists = db.query(ApiKeyModelPermission).filter(
                            ApiKeyModelPermission.api_key_id == api_key.id,
                            ApiKeyModelPermission.model_id == model_id,
                        ).first()
                        if exists is None:
                            db.add(ApiKeyModelPermission(api_key_id=api_key.id, model_id=model_id))

                    for agent_name in item.get("agents", []):
                        agent_id = agent_id_by_name.get(agent_name)
                        if agent_id is None:
                            logger.warning(f"Seed API key {username}: agent '{agent_name}' 未註冊")
                            continue
                        exists = db.query(UserAgentPermission).filter(
                            UserAgentPermission.user_id == user.id,
                            UserAgentPermission.agent_id == agent_id,
                        ).first()
                        if exists is None:
                            db.add(UserAgentPermission(user_id=user.id, agent_id=agent_id))
            except json.JSONDecodeError as e:
                logger.error(f"AUTO_SEED_API_KEYS JSON 解析失敗: {e}")
            except Exception as e:
                logger.error(f"API key 自動初始化失敗: {e}")

        # 平台對話入口。缺這列時第一次聊天會 404「模型 'anila-router' 未註冊」。
        ensure_platform_router_model(db)

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"自動初始化失敗: {e}")
    finally:
        db.close()
