"""Auto-register models, agents, and dev credentials from environment variables."""
import json
import logging
import os
import re
import hashlib
from app.config import settings
from app.database import SessionLocal
from app.models.agent import Agent, UserAgentPermission
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.model_registry import ModelRegistry
from app.models.platform_link import PlatformLink
from app.models.user import User
from app.utils.security import hash_password

logger = logging.getLogger(__name__)


def _parse_model_env_vars() -> list[dict]:
    """Parse per-model env vars with pattern MODEL_<NAME>_<FIELD>.

    Supports:
      MODEL_LLAMA3_70B_HOST=vllm-llm
      MODEL_LLAMA3_70B_PORT=8000
      MODEL_LLAMA3_70B_TYPE=llm
      MODEL_LLAMA3_70B_DISPLAY_NAME=Llama 3 70B Instruct
      MODEL_LLAMA3_70B_API_VERSION=v1
      MODEL_LLAMA3_70B_DESCRIPTION=vLLM deployed model
      MODEL_LLAMA3_70B_CONTEXT_WINDOW=8192
      MODEL_LLAMA3_70B_BASE_MODEL=llama3-70b  (for agents)

    Model name: underscores converted to hyphens, lowercased.
    e.g., MODEL_LLAMA3_70B_HOST -> model name "llama3-70b"
    """
    pattern = re.compile(
        r"^MODEL_(.+?)_(HOST|PORT|TYPE|DISPLAY_NAME|API_VERSION|DESCRIPTION|CONTEXT_WINDOW|BASE_MODEL)$"
    )

    raw: dict[str, dict[str, str]] = {}
    for key, value in os.environ.items():
        m = pattern.match(key)
        if m:
            model_key = m.group(1)  # e.g., LLAMA3_70B
            field = m.group(2)      # e.g., HOST
            if model_key not in raw:
                raw[model_key] = {}
            raw[model_key][field] = value

    models = []
    for model_key, fields in raw.items():
        host = fields.get("HOST")
        if not host:
            continue

        port = fields.get("PORT", "8000")
        name = model_key.lower().replace("_", "-")
        endpoint_url = f"http://{host}:{port}" if not host.startswith("http") else host

        model = {
            "name": name,
            "display_name": fields.get("DISPLAY_NAME", name),
            "model_type": fields.get("TYPE", "llm"),
            "endpoint_url": endpoint_url,
            "api_version": fields.get("API_VERSION", "v1"),
            "description": fields.get("DESCRIPTION", ""),
        }
        if "CONTEXT_WINDOW" in fields:
            try:
                model["context_window"] = int(fields["CONTEXT_WINDOW"])
            except ValueError:
                pass
        if "BASE_MODEL" in fields:
            model["base_model"] = fields["BASE_MODEL"]

        models.append(model)
        logger.info(f"從環境變數解析模型: MODEL_{model_key}_* -> {name}")

    return models


def auto_seed():
    """Run on startup: create admin, auto-register models/agents, seed dev keys."""
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
        admin = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if not admin:
            admin = User(
                username=settings.ADMIN_USERNAME,
                hashed_password=hash_password(settings.ADMIN_PASSWORD),
                role="owner",
                is_active=True,
            )
            db.add(admin)
            db.flush()
            logger.info(f"已建立 owner 帳號: {settings.ADMIN_USERNAME}")

        # 2. Auto-register models
        # Sources: AUTO_REGISTER_MODELS (JSON) + MODEL_*_HOST env vars
        # Two passes: first register base models, then agents (which may reference base models)
        models_config = []
        if settings.AUTO_REGISTER_MODELS:
            try:
                models_config = json.loads(settings.AUTO_REGISTER_MODELS)
            except json.JSONDecodeError as e:
                logger.error(f"AUTO_REGISTER_MODELS JSON 解析失敗: {e}")

        # Merge per-model env vars (MODEL_*_HOST pattern)
        env_models = _parse_model_env_vars()
        existing_names = {m["name"] for m in models_config}
        for em in env_models:
            if em["name"] not in existing_names:
                models_config.append(em)
            else:
                logger.debug(f"模型 {em['name']} 已在 JSON 配置中，跳過 env var 版本")

        if models_config:
            try:

                # Pass 1: register non-agent models first
                for m in models_config:
                    if m.get("model_type") == "agent":
                        continue
                    existing = db.query(ModelRegistry).filter(
                        ModelRegistry.name == m["name"]
                    ).first()
                    if not existing:
                        model = ModelRegistry(
                            name=m["name"],
                            display_name=m.get("display_name", m["name"]),
                            model_type=m.get("model_type", "llm"),
                            endpoint_url=m["endpoint_url"],
                            api_version=m.get("api_version", "v1"),
                            description=m.get("description", ""),
                            context_window=m.get("context_window"),
                        )
                        db.add(model)
                        logger.info(f"自動註冊模型: {m['name']} -> {m['endpoint_url']}")
                    else:
                        if existing.endpoint_url != m["endpoint_url"]:
                            existing.endpoint_url = m["endpoint_url"]
                            logger.info(f"更新模型端點: {m['name']} -> {m['endpoint_url']}")

                db.flush()  # Ensure base models have IDs

                # Pass 2: register agent models (may reference base_model by name)
                for m in models_config:
                    if m.get("model_type") != "agent":
                        continue
                    existing = db.query(ModelRegistry).filter(
                        ModelRegistry.name == m["name"]
                    ).first()

                    # Resolve base_model by name
                    base_model_id = None
                    base_model_name = m.get("base_model")
                    if base_model_name:
                        base = db.query(ModelRegistry).filter(
                            ModelRegistry.name == base_model_name
                        ).first()
                        if base:
                            base_model_id = base.id
                        else:
                            logger.warning(
                                f"Agent {m['name']} 的底層模型 '{base_model_name}' 未找到"
                            )

                    if not existing:
                        model = ModelRegistry(
                            name=m["name"],
                            display_name=m.get("display_name", m["name"]),
                            model_type="agent",
                            endpoint_url=m["endpoint_url"],
                            api_version=m.get("api_version", "v1"),
                            description=m.get("description", ""),
                            context_window=m.get("context_window"),
                            base_model_id=base_model_id,
                        )
                        db.add(model)
                        logger.info(
                            f"自動註冊 Agent: {m['name']} -> {m['endpoint_url']}"
                            f" (底層: {base_model_name or '無'})"
                        )
                    else:
                        if existing.endpoint_url != m["endpoint_url"]:
                            existing.endpoint_url = m["endpoint_url"]
                        if base_model_id and existing.base_model_id != base_model_id:
                            existing.base_model_id = base_model_id
                            logger.info(f"更新 Agent 底層模型: {m['name']} -> {base_model_name}")

            except Exception as e:
                logger.error(f"模型自動註冊失敗: {e}")

        # 3. Auto-register agents from AUTO_REGISTER_AGENTS env
        if settings.AUTO_REGISTER_AGENTS:
            try:
                agents_config = json.loads(settings.AUTO_REGISTER_AGENTS)
                for item in agents_config:
                    existing = db.query(Agent).filter(Agent.name == item["name"]).first()

                    owner_username = item.get("owner_username", settings.ADMIN_USERNAME)
                    owner = db.query(User).filter(User.username == owner_username).first()
                    if owner is None:
                        logger.warning(f"Agent {item['name']} 的 owner '{owner_username}' 不存在，跳過")
                        continue

                    base_model_id = None
                    base_model_name = item.get("base_model")
                    if base_model_name:
                        base_model = db.query(ModelRegistry).filter(
                            ModelRegistry.name == base_model_name
                        ).first()
                        if base_model:
                            base_model_id = base_model.id
                        else:
                            logger.warning(
                                f"Agent {item['name']} 的 base model '{base_model_name}' 未找到"
                            )

                    if not existing:
                        existing = Agent(
                            name=item["name"],
                            owner_user_id=owner.id,
                            endpoint_url=item["endpoint_url"],
                            api_version=item.get("api_version", "v1"),
                            description_for_router=item.get("description_for_router", ""),
                            base_model_id=base_model_id,
                            capabilities=item.get("capabilities"),
                            input_schema=item.get("input_schema"),
                            health_status=item.get("health_status", "unknown"),
                            approval_status=item.get("approval_status", "approved"),
                        )
                        db.add(existing)
                        logger.info(f"自動註冊 agent: {item['name']} -> {item['endpoint_url']}")
                    else:
                        existing.owner_user_id = owner.id
                        existing.endpoint_url = item["endpoint_url"]
                        existing.api_version = item.get("api_version", existing.api_version)
                        existing.description_for_router = item.get(
                            "description_for_router",
                            existing.description_for_router,
                        )
                        existing.base_model_id = base_model_id
                        existing.capabilities = item.get("capabilities", existing.capabilities)
                        existing.input_schema = item.get("input_schema", existing.input_schema)
                        existing.health_status = item.get("health_status", existing.health_status)
                        existing.approval_status = item.get(
                            "approval_status",
                            existing.approval_status,
                        )

                    if existing.approval_status == "approved":
                        existing.approved_by = admin.id

                db.flush()
            except json.JSONDecodeError as e:
                logger.error(f"AUTO_REGISTER_AGENTS JSON 解析失敗: {e}")
            except Exception as e:
                logger.error(f"Agent 自動註冊失敗: {e}")

        # 4. Auto-seed users + API keys from AUTO_SEED_API_KEYS env
        if settings.AUTO_SEED_API_KEYS:
            try:
                keys_config = json.loads(settings.AUTO_SEED_API_KEYS)
                model_id_by_name = {
                    model.name: model.id
                    for model in db.query(ModelRegistry).all()
                }
                agent_id_by_name = {
                    agent.name: agent.id
                    for agent in db.query(Agent).all()
                }

                for item in keys_config:
                    username = item["username"]
                    user = db.query(User).filter(User.username == username).first()
                    if user is None:
                        user = User(
                            username=username,
                            email=item.get("email"),
                            hashed_password=hash_password(item.get("password", "changeme")),
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
                            logger.warning(f"Seed API key {username}: model '{model_name}' 未註冊")
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

        # 5. Auto-register platform links from AUTO_REGISTER_LINKS env
        # Idempotent upsert — also syncs is_public + required_roles on
        # existing rows so that flipping a flag in env survives a restart
        # without manual DB editing. Migrations 0012 (required_roles) and
        # 0013 (is_public) added these fields; this seed honours them.
        if settings.AUTO_REGISTER_LINKS:
            try:
                links_config = json.loads(settings.AUTO_REGISTER_LINKS)
                for idx, link_data in enumerate(links_config):
                    name = link_data["name"]
                    # Coerce nullable required_roles → [] (schema is NOT NULL
                    # JSONB DEFAULT '[]'). Keeps the env var copy-pastable
                    # from older v0.4 design doc that wrote `null`.
                    required_roles = link_data.get("required_roles") or []
                    is_public = bool(link_data.get("is_public", False))

                    existing = db.query(PlatformLink).filter(
                        PlatformLink.name == name
                    ).first()
                    if existing is None:
                        db.add(PlatformLink(
                            name=name,
                            url=link_data["url"],
                            icon=link_data.get("icon", ""),
                            description=link_data.get("description", ""),
                            sort_order=link_data.get("sort_order", idx + 1),
                            is_public=is_public,
                            required_roles=required_roles,
                        ))
                        logger.info(f"自動註冊平台連結: {name}")
                    else:
                        # Sync mutable fields. Don't touch is_active so an
                        # admin's manual deactivation isn't reverted on
                        # restart (admin > env var here).
                        changed = False
                        for field, new_value in (
                            ("url", link_data["url"]),
                            ("icon", link_data.get("icon", existing.icon or "")),
                            ("description", link_data.get(
                                "description", existing.description or "")),
                            ("sort_order", link_data.get(
                                "sort_order", existing.sort_order)),
                            ("is_public", is_public),
                            ("required_roles", required_roles),
                        ):
                            if getattr(existing, field) != new_value:
                                setattr(existing, field, new_value)
                                changed = True
                        if changed:
                            logger.info(f"同步平台連結: {name}")
            except json.JSONDecodeError as e:
                logger.error(f"AUTO_REGISTER_LINKS JSON 解析失敗: {e}")

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"自動初始化失敗: {e}")
    finally:
        db.close()
