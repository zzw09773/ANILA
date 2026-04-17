"""Auto-register models and platform links from environment variables on startup."""
import json
import logging
import os
import re
from app.config import settings
from app.database import SessionLocal
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
    """Run on startup: create admin, auto-register models & links from env vars."""
    db = SessionLocal()
    try:
        # 1. Ensure admin user exists
        admin = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if not admin:
            admin = User(
                username=settings.ADMIN_USERNAME,
                hashed_password=hash_password(settings.ADMIN_PASSWORD),
                role="admin",
                is_active=True,
            )
            db.add(admin)
            db.flush()
            logger.info(f"已建立管理員帳號: {settings.ADMIN_USERNAME}")

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

        # 3. Auto-register platform links from AUTO_REGISTER_LINKS env
        if settings.AUTO_REGISTER_LINKS:
            try:
                links_config = json.loads(settings.AUTO_REGISTER_LINKS)
                for idx, link_data in enumerate(links_config):
                    existing = db.query(PlatformLink).filter(
                        PlatformLink.name == link_data["name"]
                    ).first()
                    if not existing:
                        link = PlatformLink(
                            name=link_data["name"],
                            url=link_data["url"],
                            icon=link_data.get("icon", ""),
                            description=link_data.get("description", ""),
                            sort_order=link_data.get("sort_order", idx + 1),
                        )
                        db.add(link)
                        logger.info(f"自動註冊平台連結: {link_data['name']}")
            except json.JSONDecodeError as e:
                logger.error(f"AUTO_REGISTER_LINKS JSON 解析失敗: {e}")

        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"自動初始化失敗: {e}")
    finally:
        db.close()
